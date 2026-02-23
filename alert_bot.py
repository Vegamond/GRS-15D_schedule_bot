#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests


ALERTS_ACTIVE_URL = os.getenv(
    "ALERTS_ACTIVE_URL",
    # типово в alerts.in.ua часто використовують iot endpoint з активними тривогами
    "https://alerts.in.ua/api/v1/iot/active_air_raid_alerts.json",
)

STATE_FILE = os.getenv("ALERTS_STATE_FILE", "alerts_state.json")


def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_active": None, "last_change_ts": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_active": None, "last_change_ts": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def now_local_str() -> str:
    # GitHub runner UTC; але для тексту досить “UTC+2/+3” не вираховуємо —
    # якщо хочеш, можна додати pytz/zoneinfo. Зараз — універсально.
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def extract_any_id(item: dict) -> set:
    """
    Повертає множину можливих ID зі структури елемента активної тривоги.
    Робимо максимально толерантно до різних форматів.
    """
    ids = set()

    # прямі поля
    for k in ("uid", "id", "regionId", "region_id", "territoryId", "territory_id", "locationUid", "location_uid"):
        v = item.get(k)
        if v is not None:
            ids.add(str(v))

    # вкладені структури, які часто зустрічаються
    for path in (
        ("region", "uid"),
        ("region", "id"),
        ("territory", "uid"),
        ("territory", "id"),
        ("location", "uid"),
        ("location", "id"),
    ):
        cur = item
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and cur is not None:
            ids.add(str(cur))

    return ids


def is_uid_active(active_payload, target_uid: str) -> bool:
    """
    active_payload може бути list/dict.
    Ціль: визначити чи є target_uid у списку активних тривог.
    Якщо UID не зустрівся — це означає “нема тривоги”, а не помилка.
    """
    target_uid = str(target_uid)

    # інколи API повертає {"data":[...]} або {"alerts":[...]}
    if isinstance(active_payload, dict):
        for key in ("data", "alerts", "result", "active", "items"):
            if key in active_payload and isinstance(active_payload[key], list):
                active_payload = active_payload[key]
                break

    if not isinstance(active_payload, list):
        # якщо формат взагалі неочікуваний — не падаємо, просто вважаємо “нема”
        return False

    for item in active_payload:
        if not isinstance(item, dict):
            continue
        ids = extract_any_id(item)
        if target_uid in ids:
            return True

    return False


def tg_send_message(token: str, chat_id: str, text: str, thread_id: str | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    # Для форумних груп (topics) треба message_thread_id
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    r = requests.post(url, json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text}")


def main() -> int:
    tg_token = env_required("TG_BOT_TOKEN")
    tg_chat_id = env_required("TG_CHAT_ID")

    # topic/thread для тривог
    tg_alerts_thread_id = os.getenv("TG_ALERTS_THREAD_ID")  # може бути None (тоді в загальний)
    alerts_token = env_required("ALERTS_TOKEN")
    alerts_uid = env_required("ALERTS_UID")  # у тебе: 332

    headers = {
        # alerts.in.ua зазвичай приймає X-API-Key, але лишаємо також Bearer як fallback
        "X-API-Key": alerts_token,
        "Authorization": f"Bearer {alerts_token}",
        "Accept": "application/json",
        "User-Agent": "GRS-15D-alert-bot/1.0",
    }

    # 1) Забираємо активні тривоги
    resp = requests.get(ALERTS_ACTIVE_URL, headers=headers, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"alerts.in.ua request failed: {resp.status_code} {resp.text}")

    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError("alerts.in.ua returned non-JSON response")

    active_now = is_uid_active(payload, alerts_uid)

    # 2) Стан + антиспам
    state = load_state()
    last_active = state.get("last_active", None)

    # first run — просто запам’ятати, не постити (щоб не “лякати” на запуску)
    if last_active is None:
        state["last_active"] = active_now
        state["last_change_ts"] = int(time.time())
        save_state(state)
        print(f"[init] active_now={active_now} (saved, no post)")
        return 0

    if bool(last_active) == bool(active_now):
        print(f"[nochange] active_now={active_now} (no post)")
        return 0

    # 3) Є зміна: постимо
    ts = now_local_str()
    if active_now:
        text = (
            "🚨 **ПОВІТРЯНА ТРИВОГА — Дніпро**\n"
            f"🕒 {ts}\n\n"
            "Будь ласка, пройдіть в укриття / дотримуйтесь правил безпеки."
        )
    else:
        text = (
            "✅ **ВІДБІЙ ТРИВОГИ — Дніпро**\n"
            f"🕒 {ts}\n\n"
            "Можна повертатися до справ (але тримаємо голову холодною)."
        )

    # Telegram MarkdownV2 дуже вибагливий — тому без parse_mode.
    # Якщо хочеш жирний текст — зробимо окремо, з екрануванням.
    # Зараз лишимо “звичайний” текст (найнадійніше).
    text = text.replace("**", "")  # прибрали “markdown” маркери

    tg_send_message(tg_token, tg_chat_id, text, tg_alerts_thread_id)

    state["last_active"] = active_now
    state["last_change_ts"] = int(time.time())
    save_state(state)

    print(f"[posted] active_now={active_now}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
