#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests


API_BASE = "https://api.alerts.in.ua"


def env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # alerts.in.ua повертає ISO, зазвичай з Z
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fmt_ua_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "невідомо"
    # показуємо локально, але без бібліотек — просто як UTC+0 (можна замінити пізніше)
    # Для Дніпра краще зробити нормальний timezone (Europe/Kyiv) через zoneinfo.
    # Тут лишаю максимально безболісно.
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%d.%m %H:%M (UTC)")


def get_active_alert_for_uid(token: str, location_uid: str) -> Optional[Dict[str, Any]]:
    """
    Беремо /v1/alerts/active.json і шукаємо alert з location_uid == потрібному uid.
    Це найуніверсальніше для громади/району/області. :contentReference[oaicite:1]{index=1}
    """
    url = f"{API_BASE}/v1/alerts/active.json"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=20)

    # нормальні коди помилок описані в доках :contentReference[oaicite:2]{index=2}
    if resp.status_code == 401:
        raise RuntimeError("401 Unauthorized: токен alerts.in.ua відсутній/неправильний/прострочений.")
    if resp.status_code == 403:
        raise RuntimeError("403 Forbidden: API заблоковане по IP/країні або інша заборона.")
    if resp.status_code == 429:
        raise RuntimeError("429 Too Many Requests: перевищено ліміти. Зменш частоту опитування.")
    if resp.status_code != 200:
        # часто тут і трапляється твій 404, коли url неправильний
        raise RuntimeError(f"alerts.in.ua request failed: {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    alerts = data.get("alerts", []) if isinstance(data, dict) else []
    for a in alerts:
        if str(a.get("location_uid")) == str(location_uid):
            return a
    return None


def tg_send_message(bot_token: str, chat_id: str, text: str, thread_id: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text[:300]}")


def build_start_message(alert: Dict[str, Any]) -> str:
    title = alert.get("location_title", "Дніпро")
    a_type = alert.get("alert_type", "air_raid")
    started = fmt_ua_time(iso_to_dt(alert.get("started_at")))
    notes = (alert.get("notes") or "").strip()

    # мінімально читабельно, без “роману”, але по суті
    lines = [
        "🚨 ТРИВОГА",
        f"📍 Локація: {title}",
        f"🏷️ Тип: {a_type}",
        f"⏱️ Початок: {started}",
    ]
    if notes:
        lines.append(f"📝 Примітка: {notes}")
    lines.append("🛡️ Бережіть себе.")
    return "\n".join(lines)


def build_end_message(last_alert: Dict[str, Any]) -> str:
    title = last_alert.get("location_title", "Дніпро")
    started = fmt_ua_time(iso_to_dt(last_alert.get("started_at")))
    finished = fmt_ua_time(iso_to_dt(last_alert.get("finished_at")))  # інколи null
    return "\n".join([
        "✅ ВІДБІЙ",
        f"📍 Локація: {title}",
        f"⏱️ Початок: {started}",
        f"🏁 Завершення: {finished}",
        "🙂 Дякуємо, що тримаєтесь.",
    ])


def main() -> int:
    # Telegram
    tg_bot_token = env_required("TG_BOT_TOKEN")
    tg_chat_id = env_required("TG_CHAT_ID")
    tg_thread_id = os.getenv("TG_ALERTS_THREAD_ID", "").strip() or None  # topic id

    # alerts.in.ua
    alerts_token = env_required("ALERTS_TOKEN")
    alerts_uid = os.getenv("ALERTS_UID", "332").strip()  # твій UID = 332

    state_path = os.getenv("ALERTS_STATE_FILE", "alerts_state.json")
    state = load_state(state_path)

    prev_active = bool(state.get("active", False))
    prev_alert = state.get("last_alert") if isinstance(state.get("last_alert"), dict) else None

    # 1) перевіряємо активну тривогу саме для uid
    current_alert = get_active_alert_for_uid(alerts_token, alerts_uid)
    now_active = current_alert is not None

    # 2) якщо змінився стан — постимо
    if (not prev_active) and now_active:
        msg = build_start_message(current_alert)
        tg_send_message(tg_bot_token, tg_chat_id, msg, tg_thread_id)
        state["active"] = True
        state["last_alert"] = current_alert

    elif prev_active and (not now_active):
        # в active.json її вже нема => відбій
        if prev_alert:
            msg = build_end_message(prev_alert)
        else:
            msg = "✅ ВІДБІЙ\n📍 Локація: Дніпро\n🙂"
        tg_send_message(tg_bot_token, tg_chat_id, msg, tg_thread_id)
        state["active"] = False

    else:
        # без змін — нічого не робимо
        pass

    state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state_path, state)
    print(f"OK. active={now_active}, prev_active={prev_active}, uid={alerts_uid}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
