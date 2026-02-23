#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ", "Europe/Kyiv"))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_ALERTS_THREAD_ID = os.getenv("TG_ALERTS_THREAD_ID", "").strip()

ALERTS_API_KEY = os.getenv("ALERTS_API_KEY", "").strip()
ALERTS_UID = int(os.getenv("ALERTS_UID", "332").strip())  # м. Дніпро та Дніпровська ТГ

STATE_FILE = os.getenv("ALERTS_STATE_FILE", "alerts_state.json").strip()

# Найчастіше використовується такий endpoint у прикладах клієнтів:
ALERTS_BASE_URL = os.getenv("ALERTS_BASE_URL", "https://alerts.com.ua").strip().rstrip("/")
ALERTS_ENDPOINT = os.getenv("ALERTS_ENDPOINT", "/api/states").strip()

def _http_get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "GRS-15D-alert-bot/1.0",
            "X-API-Key": ALERTS_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)

def _http_post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "GRS-15D-alert-bot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TG_ALERTS_THREAD_ID:
        payload["message_thread_id"] = int(TG_ALERTS_THREAD_ID)
    resp = _http_post_json(url, payload)
    if not resp.get("ok"):
        raise RuntimeError(resp)

def main():
    if not (TG_BOT_TOKEN and TG_CHAT_ID and ALERTS_API_KEY):
        raise RuntimeError("Потрібні TG_BOT_TOKEN, TG_CHAT_ID, ALERTS_API_KEY")

    url = f"{ALERTS_BASE_URL}{ALERTS_ENDPOINT}"
    data = _http_get_json(url)

    # У різних реалізаціях структура може бути:
    # - список об'єктів
    # - або {"states":[...]}
    states = data.get("states") if isinstance(data, dict) else None
    if states is None and isinstance(data, list):
        states = data
    if states is None:
        raise RuntimeError(f"Unexpected alerts response: {data}")

    target = None
    for s in states:
        # пробуємо типові ключі
        sid = s.get("id") or s.get("uid") or s.get("regionId") or s.get("locationId")
        if sid == ALERTS_UID:
            target = s
            break

    if not target:
        raise RuntimeError(f"Не знайшов UID={ALERTS_UID} у відповіді API")

    # Нормалізуємо “чи є тривога”
    # типові поля: alert / alarm / isAlarm / active
    active = (
        bool(target.get("alert"))
        or bool(target.get("alarm"))
        or bool(target.get("isAlarm"))
        or bool(target.get("active"))
    )

    # тип загрози (якщо є)
    kind = target.get("type") or target.get("alertType") or target.get("kind") or ""
    kind = str(kind).strip()

    now = datetime.now(TZ).strftime("%H:%M")
    place = "м. Дніпро та Дніпровська територіальна громада"

    state = load_state()
    prev = state.get("active")

    # антиспам: пишемо тільки при зміні
    if prev is None or bool(prev) != bool(active):
        if active:
            title = "🚨 <b>Тривога!</b>"
            extra = f"\nТип: <b>{kind}</b>" if kind else ""
            msg = f"{title}\n📍 {place}{extra}\n🕒 {now}"
        else:
            msg = f"✅ <b>Відбій</b>\n📍 {place}\n🕒 {now}"

        tg_send(msg)

        state["active"] = bool(active)
        state["last_change"] = datetime.now(TZ).isoformat()
        save_state(state)

    print("OK")

if __name__ == "__main__":
    main()
