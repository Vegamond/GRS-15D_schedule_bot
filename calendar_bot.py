import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from icalendar import Calendar
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DNIPRO_LAT = 48.4647
DNIPRO_LON = 35.0462

STATE_FILE = "state.json"

EMOJI = {
    "morning": "🌅",
    "evening": "🌙",
    "week": "🗓️",
    "calendar": "📅",
    "weather": "⛅",
    "updated": "🕒",
    "lesson": "📌",
    "time": "🕒",
    "teacher": "👩‍🏫",
    "type": "📚",
    "place": "🌐",
    "link": "🔗",
    "code": "🔑",
    "no_lessons": "— (пар немає)",
    "blue_day": "🟦",
}

# ---------- Models ----------

@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    summary: str
    location: str
    description: str

    def is_online(self) -> bool:
        text = f"{self.location}\n{self.description}".lower()
        return "zoom" in text or "online" in text or "meet.google.com" in text

# ---------- Utils ----------

def load_env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v

def read_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"last_posted": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_posted": {}}

def write_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def now_kyiv() -> dt.datetime:
    return dt.datetime.now(tz=KYIV_TZ)

def in_window(now_local: dt.datetime, hh: int, mm: int, window_minutes: int = 10) -> bool:
    """True if now is between hh:mm and hh:mm+window_minutes (local time)."""
    start = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    end = start + dt.timedelta(minutes=window_minutes)
    return start <= now_local < end

def today_date(now_local: dt.datetime) -> dt.date:
    return now_local.date()

def tomorrow_date(now_local: dt.datetime) -> dt.date:
    return (now_local + dt.timedelta(days=1)).date()

def sunday_week_key(d: dt.date) -> str:
    # ISO week key like 2026-W08
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"

def date_title_ua(d: dt.date) -> str:
    return d.strftime("%d.%m")

def weekday_short_ua(d: dt.date) -> str:
    # Пн, Вт, Ср, Чт, Пт, Сб, Нд
    wd = d.weekday()
    return ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"][wd]

def normalize_time(s: dt.datetime) -> str:
    return s.astimezone(KYIV_TZ).strftime("%H:%M")

def safe_text(s: str) -> str:
    # plain text: keep as-is but strip weird nulls
    return (s or "").replace("\x00", "").strip()

# ---------- Calendar (ICS) ----------

def fetch_ics(url: str, timeout: int = 30) -> bytes:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "GRS-15D-schedule-bot/1.0"})
    r.raise_for_status()
    return r.content

def parse_ics_events(ics_bytes: bytes) -> List[Event]:
    cal = Calendar.from_ical(ics_bytes)
    events: List[Event] = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        summary = safe_text(str(component.get("summary", "")))
        location = safe_text(str(component.get("location", "")))
        description = safe_text(str(component.get("description", "")))

        dtstart = component.get("dtstart")
        dtend = component.get("dtend")

        if not dtstart or not dtend:
            continue

        start = dtstart.dt
        end = dtend.dt

        # Ensure timezone-aware
        if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
            # all-day event -> ignore for schedule bot
            continue
        if isinstance(end, dt.date) and not isinstance(end, dt.datetime):
            continue

        if start.tzinfo is None:
            start = start.replace(tzinfo=KYIV_TZ)
        if end.tzinfo is None:
            end = end.replace(tzinfo=KYIV_TZ)

        events.append(Event(start=start, end=end, summary=summary, location=location, description=description))

    events.sort(key=lambda e: e.start)
    return events

def events_for_date(events: List[Event], day: dt.date) -> List[Event]:
    out = []
    for e in events:
        if e.start.astimezone(KYIV_TZ).date() == day:
            out.append(e)
    out.sort(key=lambda e: e.start)
    return out

def events_for_week(events: List[Event], start_day: dt.date, days: int = 7) -> Dict[dt.date, List[Event]]:
    m: Dict[dt.date, List[Event]] = {}
    for i in range(days):
        d = start_day + dt.timedelta(days=i)
        m[d] = events_for_date(events, d)
    return m

# ---------- Extract structured info from description ----------

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def extract_first_url_from_text(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None

def extract_teacher(desc: str) -> Optional[str]:
    """
    Tries to extract teacher line like:
    - 'доц. Плещан Х.В.'
    - 'доц. Применко В.Г.'
    - 'Викл. Руденко А.В.'
    """
    if not desc:
        return None

    # Common prefixes
    patterns = [
        r"(?im)^\s*(доц\.\s*[^\n]+)$",
        r"(?im)^\s*(викл\.\s*[^\n]+)$",
        r"(?im)^\s*(проф\.\s*[^\n]+)$",
        r"(?im)^\s*(асист\.\s*[^\n]+)$",
    ]
    for p in patterns:
        m = re.search(p, desc)
        if m:
            return safe_text(m.group(1))

    # Sometimes in the middle: "Доц: Плещан Х.В."
    m2 = re.search(r"(?im)\b(доц|викл|проф|асист)\s*[:\-]\s*([^\n]+)", desc)
    if m2:
        return safe_text(f"{m2.group(1)}. {m2.group(2)}".replace("..", "."))

    return None

def extract_access_code(desc: str) -> Optional[str]:
    """
    Looks for:
    - 'Код доступу: Kuk-2022'
    - 'Код: 12345'
    """
    if not desc:
        return None
    m = re.search(r"(?im)\bкод\s+доступу\s*[:\-]\s*([^\n]+)", desc)
    if m:
        return safe_text(m.group(1))
    m = re.search(r"(?im)\bpasscode\s*[:\-]\s*([^\n]+)", desc)
    if m:
        return safe_text(m.group(1))
    return None

def extract_place(event: Event) -> str:
    # Prefer "Online" wording if online, else use location if present
    loc = safe_text(event.location)
    desc = safe_text(event.description)
    text = f"{loc}\n{desc}".lower()

    if "aud." in text or "ауд." in text:
        # Keep whatever has auditorium in it
        # Try to return a short phrase containing "ауд"
        m = re.search(r"(?i)(ауд\.\s*\d+)", f"{loc}\n{desc}")
        if m:
            return f"Online (Zoom) / {m.group(1)}" if event.is_online() else m.group(1)

    if event.is_online():
        return "Online (Zoom)"
    if loc:
        return loc
    return "Очно"

def extract_main_link(event: Event) -> Optional[str]:
    # Requirement: remove the "upper link" and keep only the one in description.
    # So: first URL from description ONLY.
    return extract_first_url_from_text(event.description)

# ---------- Weather (Open-Meteo, no key) ----------

def weather_code_to_emoji(code: int) -> str:
    # Open-Meteo WMO weather codes: https://open-meteo.com/en/docs
    if code in (0,):
        return "☀️"
    if code in (1, 2, 3):
        return "⛅"
    if code in (45, 48):
        return "🌫️"
    if code in (51, 53, 55, 56, 57):
        return "🌦️"
    if code in (61, 63, 65, 66, 67):
        return "🌧️"
    if code in (71, 73, 75, 77):
        return "🌨️"
    if code in (80, 81, 82):
        return "🌧️"
    if code in (85, 86):
        return "🌨️"
    if code in (95, 96, 99):
        return "⛈️"
    return "🌤️"

def fetch_weather_daily(day: dt.date) -> Optional[Dict]:
    """
    Returns weather dict for that date:
    { "desc": "...", "tmin": ..., "tmax": ..., "precip_prob": ..., "emoji": "..." }
    """
    # We'll ask for daily forecast and pick index by date.
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={DNIPRO_LAT}&longitude={DNIPRO_LON}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "GRS-15D-schedule-bot/1.0"})
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        if not dates:
            return None
        if day.isoformat() not in dates:
            return None
        i = dates.index(day.isoformat())

        code = int(daily.get("weathercode", [None])[i])
        tmax = daily.get("temperature_2m_max", [None])[i]
        tmin = daily.get("temperature_2m_min", [None])[i]
        pprob = daily.get("precipitation_probability_max", [None])[i]

        emoji = weather_code_to_emoji(code)

        # Very simple UA-ish description (not perfect, but readable)
        desc = {
            0: "ясно",
            1: "переважно ясно",
            2: "мінлива хмарність",
            3: "хмарно",
            45: "туман",
            48: "туман",
            51: "мряка",
            53: "мряка",
            55: "мряка",
            61: "дощ",
            63: "дощ",
            65: "сильний дощ",
            71: "сніг",
            73: "сніг",
            75: "сильний сніг",
            80: "зливи",
            81: "зливи",
            82: "сильні зливи",
            85: "снігопад",
            86: "сильний снігопад",
            95: "гроза",
            96: "гроза",
            99: "гроза",
        }.get(code, "погода")

        return {
            "emoji": emoji,
            "desc": desc,
            "tmin": tmin,
            "tmax": tmax,
            "precip_prob": pprob,
        }
    except Exception:
        return None

def format_weather_block(day: dt.date, label: str) -> str:
    w = fetch_weather_daily(day)
    if not w:
        return ""
    lines = []
    lines.append(f"{EMOJI['weather']} Погода в Дніпрі {label}:")
    lines.append(f"{w['emoji']} {w['desc']}")
    if w["tmin"] is not None and w["tmax"] is not None:
        lines.append(f"🌡️ Мін/Макс: {w['tmin']}°C / {w['tmax']}°C")
    if w["precip_prob"] is not None:
        lines.append(f"☔ Ймовірність опадів: {w['precip_prob']}%")
    return "\n".join(lines) + "\n\n"

# ---------- Formatting ----------

def format_today(events: List[Event], day: dt.date) -> str:
    header = f"Доброго ранку шановні студенти! {EMOJI['morning']}\n{EMOJI['calendar']} Розклад на сьогодні ({date_title_ua(day)})\n\n"
    weather = format_weather_block(day, "на сьогодні")
    body = format_events_detailed(events) if events else f"{EMOJI['no_lessons']}\n"
    updated = f"\n{EMOJI['updated']} Оновлено: {now_kyiv().strftime('%H:%M')}"
    return header + weather + body + updated

def format_tomorrow(events: List[Event], day: dt.date) -> str:
    header = f"Добрий вечір шановні студенти! {EMOJI['evening']}\n{EMOJI['calendar']} Розклад на завтра ({date_title_ua(day)})\n\n"
    weather = format_weather_block(day, "на завтра")
    body = format_events_detailed(events) if events else f"{EMOJI['no_lessons']}\n"
    updated = f"\n{EMOJI['updated']} Оновлено: {now_kyiv().strftime('%H:%M')}"
    return header + weather + body + updated

def format_events_detailed(events: List[Event]) -> str:
    lines: List[str] = []
    for i, e in enumerate(events, 1):
        st = normalize_time(e.start)
        en = normalize_time(e.end)

        teacher = extract_teacher(e.description) or ""
        access_code = extract_access_code(e.description)
        place = extract_place(e)
        link = extract_main_link(e)

        # Title line
        lines.append(f"{EMOJI['lesson']} {i}. {safe_text(e.summary).upper()}")
        # Time
        lines.append(f"{EMOJI['time']} {st}–{en}")
        # Type (we infer from title sometimes; leave as "Лекція" if present)
        # If summary already includes "Лекція/Практика" we don't duplicate.
        if "лекц" in e.summary.lower():
            lines.append(f"{EMOJI['type']} Лекція")
        elif "практ" in e.summary.lower():
            lines.append(f"{EMOJI['type']} Практика")
        else:
            lines.append(f"{EMOJI['type']} Заняття")

        if teacher:
            # normalize "Викл." -> "Викл."
            lines.append(f"{EMOJI['teacher']} {teacher}")

        lines.append(f"{EMOJI['place']} {place}")

        if link:
            lines.append(f"{EMOJI['link']} {link}")
        if access_code:
            lines.append(f"{EMOJI['code']} Код доступу: {access_code}")

        lines.append("")  # blank line between lessons

    return "\n".join(lines).rstrip() + "\n"

def format_week(week_map: Dict[dt.date, List[Event]], start_day: dt.date) -> str:
    end_day = start_day + dt.timedelta(days=6)
    header = f"{EMOJI['week']} Розклад на тиждень\n{date_title_ua(start_day)} – {date_title_ua(end_day)}\n\n"
    lines: List[str] = [header]

    for d in sorted(week_map.keys()):
        day_events = week_map[d]
        lines.append(f"{EMOJI['blue_day']} {weekday_short_ua(d)} • {date_title_ua(d)}")
        if not day_events:
            lines.append(f"{EMOJI['no_lessons']}\n")
            continue

        for e in day_events:
            st = normalize_time(e.start)
            en = normalize_time(e.end)
            online = "🌐 Online" if e.is_online() else "🏫 Очно"
            # compact line:
            # 🕒 09:00–10:20 • Історія України • 🌐 Online
            lines.append(f"{EMOJI['time']} {st}–{en} • {safe_text(e.summary)} • {online}")

            link = extract_main_link(e)
            if link:
                lines.append(f"{EMOJI['link']} {link}")

        lines.append("")  # blank line after each day

    lines.append(f"{EMOJI['updated']} Оновлено: {now_kyiv().strftime('%H:%M')}")
    return "\n".join(lines).rstrip() + "\n"

# ---------- Telegram ----------

def tg_send(text: str, thread_id: Optional[int]) -> None:
    token = load_env_required("TG_BOT_TOKEN")
    chat_id = load_env_required("TG_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)

    r = requests.post(url, json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text}")

# ---------- Main logic ----------

def should_post(mode: str, force: bool) -> Tuple[bool, str]:
    """
    Returns (should_post, state_key)
    state_key is used to store last posted in state.json
    """
    now = now_kyiv()
    state_key = ""

    if force:
        if mode == "today":
            state_key = today_date(now).isoformat()
        elif mode == "tomorrow":
            state_key = tomorrow_date(now).isoformat()
        elif mode == "week":
            # key for the week starting Monday of current week
            monday = (now.date() - dt.timedelta(days=now.date().weekday()))
            state_key = sunday_week_key(monday)
        return True, state_key

    if mode == "today":
        if not in_window(now, 8, 0, window_minutes=15):
            return False, ""
        state_key = today_date(now).isoformat()
        return True, state_key

    if mode == "tomorrow":
        if not in_window(now, 19, 0, window_minutes=15):
            return False, ""
        state_key = tomorrow_date(now).isoformat()
        return True, state_key

    if mode == "week":
        # Post on Sunday into schedule thread (e.g., at 21:00) — you can adjust
        # Here: Sunday 21:00 Kyiv
        if now.weekday() != 6:
            return False, ""
        if not in_window(now, 21, 0, window_minutes=30):
            return False, ""
        # week starting Monday (tomorrow if today is Sunday? Actually next week starts tomorrow)
        next_monday = (now.date() + dt.timedelta(days=1))
        state_key = sunday_week_key(next_monday)
        return True, state_key

    return False, ""

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["today", "tomorrow", "week"])
    ap.add_argument("--force", action="store_true", help="Post immediately (ignores time window/state)")
    args = ap.parse_args()

    # Required env
    gcal_url = load_env_required("GCAL_ICS_URL")
    # Telegram env checked inside tg_send

    state = read_state()
    last_posted = state.get("last_posted", {})

    ok_to_post, state_key = should_post(args.mode, args.force)
    if not ok_to_post:
        print("Not in posting window. Exiting.")
        return

    # Idempotency: avoid double posts for same date/week
    prev_key = last_posted.get(args.mode)
    if (not args.force) and prev_key == state_key:
        print(f"Already posted for {args.mode}: {state_key}. Exiting.")
        return

    ics = fetch_ics(gcal_url)
    events = parse_ics_events(ics)

    now = now_kyiv()

    if args.mode == "today":
        day = today_date(now)
        day_events = events_for_date(events, day)
        msg = format_today(day_events, day)
        # post to main chat (no thread_id)
        tg_send(msg, thread_id=None)

    elif args.mode == "tomorrow":
        day = tomorrow_date(now)
        day_events = events_for_date(events, day)
        msg = format_tomorrow(day_events, day)
        tg_send(msg, thread_id=None)

    elif args.mode == "week":
        # next week (Mon..Sun) if today is Sunday
        next_monday = now.date() + dt.timedelta(days=1)
        week_map = events_for_week(events, next_monday, days=7)
        msg = format_week(week_map, next_monday)

        # post to Schedule thread
        thread_id_env = os.getenv("TG_SCHEDULE_THREAD_ID", "").strip()
        thread_id = int(thread_id_env) if thread_id_env else None
        if thread_id is None:
            raise RuntimeError("Missing TG_SCHEDULE_THREAD_ID for week mode.")
        tg_send(msg, thread_id=thread_id)

    # update state
    state.setdefault("last_posted", {})
    state["last_posted"][args.mode] = state_key
    write_state(state)

    print(f"Posted {args.mode} successfully. State updated to {state_key}.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
