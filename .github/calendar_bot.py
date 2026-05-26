#!/usr/bin/env python3
# -*- coding: utf-8 -*-
 
import os
import re
import json
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from zoneinfo import ZoneInfo
 
import requests
 
KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
 
 
# ----------------------------
# Models
# ----------------------------
@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    summary: str
    description: str
    location: str
 
 
# ----------------------------
# Utils
# ----------------------------
def now_kyiv() -> dt.datetime:
    return dt.datetime.now(tz=KYIV_TZ)
 
 
def iso_date(d: dt.date) -> str:
    return d.isoformat()
 
 
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
 
 
def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
 
 
def should_post(state: Dict, key: str, stamp: str) -> bool:
    """
    Simple dedupe: post only if last_stamp != stamp
    """
    last = state.get(key)
    return last != stamp
 
 
def mark_posted(state: Dict, key: str, stamp: str) -> None:
    state[key] = stamp
 
 
def env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val
 
 
def env_optional_int(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None
 
 
# ----------------------------
# ICS parsing (minimal, robust-enough for Google Calendar ICS)
# ----------------------------
def fetch_ics(url: str, timeout_s: int = 30) -> str:
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text
 
 
def _unfold_ics_lines(ics_text: str) -> List[str]:
    """
    RFC5545 line folding: lines starting with space/tab continue previous line.
    """
    raw = ics_text.splitlines()
    out = []
    for line in raw:
        if not line:
            out.append(line)
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line.lstrip())
        else:
            out.append(line)
    return out
 
 
def _parse_dt(value: str, tzid: Optional[str]) -> dt.datetime:
    """
    Handles:
      - YYYYMMDDTHHMMSSZ (UTC)
      - YYYYMMDDTHHMMSS (local, interpret as tzid if provided, else Kyiv)
      - YYYYMMDD (all-day) -> treat as 00:00 in tzid/Kyiv
    """
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value, "%Y%m%d").date()
        return dt.datetime(d.year, d.month, d.day, 0, 0, tzinfo=ZoneInfo(tzid) if tzid else KYIV_TZ)
 
    if value.endswith("Z"):
        base = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        return base.astimezone(KYIV_TZ)
 
    # no Z
    naive = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(tzid) if tzid else KYIV_TZ
    return naive.replace(tzinfo=tz).astimezone(KYIV_TZ)
 
 
def parse_ics_events(ics_text: str) -> List[Event]:
    lines = _unfold_ics_lines(ics_text)
    events: List[Event] = []
 
    in_event = False
    cur: Dict[str, Tuple[Optional[str], str]] = {}
 
    def flush():
        nonlocal cur
        if not cur:
            return
        dtstart_tz, dtstart_val = cur.get("DTSTART", (None, ""))
        dtend_tz, dtend_val = cur.get("DTEND", (None, ""))
        summary = cur.get("SUMMARY", (None, ""))[1]
        description = cur.get("DESCRIPTION", (None, ""))[1]
        location = cur.get("LOCATION", (None, ""))[1]
 
        if not dtstart_val or not dtend_val:
            cur = {}
            return
 
        start = _parse_dt(dtstart_val, dtstart_tz)
        end = _parse_dt(dtend_val, dtend_tz)
 
        events.append(Event(
            start=start,
            end=end,
            summary=summary.strip(),
            description=description.strip(),
            location=location.strip(),
        ))
        cur = {}
 
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            if in_event:
                flush()
            in_event = False
            continue
        if not in_event:
            continue
 
        # key(;params)?:value
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        key = left
        tzid = None
        if ";" in left:
            key, params = left.split(";", 1)
            # e.g. DTSTART;TZID=Europe/Kyiv:...
            m = re.search(r"TZID=([^;]+)", params)
            if m:
                tzid = m.group(1)
 
        key = key.strip().upper()
        value = value.strip()
 
        if key in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION"}:
            cur[key] = (tzid, value)
 
    events.sort(key=lambda e: e.start)
    return events
 
 
def events_in_range(events: List[Event], start_date: dt.date, end_date: dt.date) -> List[Event]:
    """
    inclusive date-range by start date in Kyiv time.
    """
    out = []
    for ev in events:
        d = ev.start.astimezone(KYIV_TZ).date()
        if start_date <= d <= end_date:
            out.append(ev)
    return out
 
 
# ----------------------------
# Extractors (teacher, type, zoom link, passcode)
# ----------------------------
UA_DOW = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "Пʼятниця",
    5: "Субота",
    6: "Неділя",
}
 
TYPE_WORDS = {
    "лекція": "Лекція",
    "практич": "Практичне",
    "лаб": "Лабораторна",
    "семінар": "Семінар",
    "залік": "Залік",
    "іспит": "Іспит",
    "екзамен": "Екзамен",
}
 
EXAM_TYPES = {"Залік", "Іспит", "Екзамен"}
 
 
def split_summary(summary: str) -> Tuple[str, Optional[str]]:
    """
    Google summary often: "Дисципліна — Лекція" / "Дисципліна — Практичне"
    We return: (discipline, type)
    """
    s = summary.strip()
    parts = [p.strip() for p in s.split("—")]
    if len(parts) >= 2:
        tail = parts[-1].lower()
        for k, v in TYPE_WORDS.items():
            if k in tail:
                return ("—".join(parts[:-1]).strip(), v)
    # also try '-' dash
    parts2 = [p.strip() for p in s.split("-")]
    if len(parts2) >= 2:
        tail = parts2[-1].lower()
        for k, v in TYPE_WORDS.items():
            if k in tail:
                return ("-".join(parts2[:-1]).strip(), v)
 
    return (s, None)
 
 
def extract_zoom_links(text: str) -> List[str]:
    # keep zoom links first, but allow any https
    links = re.findall(r"https?://\S+", text)
    # strip trailing punctuation
    cleaned = []
    for l in links:
        cleaned.append(l.rstrip(").,;:!\"'"))
    # prefer zoom links
    zoom = [l for l in cleaned if "zoom.us" in l]
    rest = [l for l in cleaned if l not in zoom]
    return zoom + rest
 
 
def extract_teacher(description: str) -> Optional[str]:
    """
    Looks for lines like:
      "Доц.: Плещан Х.В."
      "доц. Применко В.Г."
      "Викл. Руденко А.В."
    """
    if not description:
        return None
    lines = [l.strip() for l in description.replace("\\n", "\n").splitlines() if l.strip()]
    patterns = [
        r"^(?:доц\.?|доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:викл\.?|викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:проф\.?|професор)\s*[:\-]?\s*(.+)$",
        r"^(?:асист\.?|асистент)\s*[:\-]?\s*(.+)$",
        r"^(?:Доц\.?|Доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:Викл\.?|Викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:Проф\.?|Професор)\s*[:\-]?\s*(.+)$",
    ]
    for line in lines:
        for pat in patterns:
            m = re.match(pat, line, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None
 
 
def extract_passcode(description: str) -> Optional[str]:
    if not description:
        return None
    # normalize
    t = description.replace("\\n", "\n")
    # common variants
    patterns = [
        r"(?:Код доступу|Код доступа|Passcode|Пароль)\s*[:\-]?\s*([A-Za-zА-Яа-я0-9\-_]+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None
 
 
def classify_place(location: str, description: str) -> str:
    """
    Return a human-friendly place line.
    """
    blob = f"{location}\n{description}".lower()
    # online hints
    if "online" in blob or "zoom" in blob:
        # sometimes includes "(ауд. 207)"
        m = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
        if m:
            return f"🌐 Online (Zoom) • 🏫 {m.group(1).replace('ауд', 'ауд.').strip()}"
        return "🌐 Online (Zoom)"
    # auditorium
    m2 = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
    if m2:
        return f"🏫 {m2.group(1).replace('ауд', 'ауд.').strip()}"
    if location.strip():
        return f"📍 {location.strip()}"
    return "📍 (місце не вказано)"
 
 
# ----------------------------
# Weather (Dnipro) via Open-Meteo (no API key)
# ----------------------------
def get_weather_dnipro(day: dt.date) -> Optional[Dict]:
    """
    Returns dict with: desc, tmin, tmax, precip_prob_max
    """
    # Dnipro coords
    lat, lon = 48.45, 34.98
 
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        dates = data.get("daily", {}).get("time", [])
        if not dates:
            return None
        if day.isoformat() not in dates:
            return None
        idx = dates.index(day.isoformat())
        wcode = data["daily"]["weathercode"][idx]
        tmax = data["daily"]["temperature_2m_max"][idx]
        tmin = data["daily"]["temperature_2m_min"][idx]
        p = data["daily"]["precipitation_probability_max"][idx]
        return {
            "desc": weathercode_ua(wcode),
            "tmin": int(round(tmin)),
            "tmax": int(round(tmax)),
            "p": int(p) if p is not None else None,
        }
    except Exception:
        return None
 
 
def weathercode_ua(code: int) -> str:
    # minimal mapping (enough to be useful)
    mapping = {
        0: "ясно",
        1: "переважно ясно",
        2: "мінлива хмарність",
        3: "хмарно",
        45: "туман",
        48: "паморозь / туман",
        51: "мряка",
        53: "мряка",
        55: "мряка",
        61: "дощ",
        63: "дощ",
        65: "сильний дощ",
        66: "крижаний дощ",
        67: "крижаний дощ",
        71: "сніг",
        73: "сніг",
        75: "сильний сніг",
        77: "снігова крупа",
        80: "зливи",
        81: "зливи",
        82: "сильні зливи",
        85: "снігопад",
        86: "сильний снігопад",
        95: "гроза",
        96: "гроза з градом",
        99: "гроза з градом",
    }
    return mapping.get(code, "погода (код: %s)" % code)
 
 
def format_weather_block(day: dt.date, label: str) -> str:
    w = get_weather_dnipro(day)
    if not w:
        return ""
    lines = []
    lines.append(f"⛅ Погода в Дніпрі на {label}:")
    lines.append(f"• {w['desc']}")
    lines.append(f"• 🌡️ Мін/Макс: {w['tmin']}°C / {w['tmax']}°C")
    if w.get("p") is not None:
        lines.append(f"• ☔ Ймовірність опадів: {w['p']}%")
    return "\n".join(lines) + "\n\n"
 
 
# ----------------------------
# Formatting
# ----------------------------
def hhmm(t: dt.datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime("%H:%M")
 
 
def fmt_date_short(d: dt.date) -> str:
    return d.strftime("%d.%m")
 
 
def day_header(d: dt.date) -> str:
    dow = UA_DOW[d.weekday()]
    return f"📅 <b>{dow}</b> • <b>{fmt_date_short(d)}</b>"
 
 
def separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"
 
 
def format_day(events: List[Event], day: dt.date) -> str:
    lines = []
    lines.append(day_header(day))
    lines.append("")  # empty line
 
    if not events:
        lines.append("— (пар немає)")
        return "\n".join(lines)
 
    # Заліки/іспити завжди першими, потім пари за часом
    exams = [ev for ev in events if split_summary(ev.summary)[1] in EXAM_TYPES]
    pairs = [ev for ev in events if split_summary(ev.summary)[1] not in EXAM_TYPES]
    sorted_events = exams + pairs
 
    for ev in sorted_events:
        discipline, etype = split_summary(ev.summary)
        teacher = extract_teacher(ev.description)
        passcode = extract_passcode(ev.description)
        place = classify_place(ev.location, ev.description)
        links = extract_zoom_links(ev.description + "\n" + ev.location)
        link = links[0] if links else None
        is_exam = etype in EXAM_TYPES
 
        if is_exam:
            lines.append(separator())
            lines.append(f"📝 <b>{etype.upper()}!</b>")
            lines.append(f"🕒 <b>{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
        else:
            lines.append(f"🕒 <b>{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
 
        lines.append(f"📚 <b>{escape_html(discipline)}</b>")
        if etype and not is_exam:
            lines.append(f"🎓 {etype}")
        if teacher:
            lines.append(f"👩‍🏫 {escape_html(teacher)}")
        lines.append(escape_html(place))
 
        if link:
            lines.append(f"🔗 {escape_html(link)}")
        if passcode:
            lines.append(f"🔑 Код доступу: <b>{escape_html(passcode)}</b>")
 
        if is_exam:
            lines.append(separator())
 
        lines.append("")  # blank line between pairs
 
    # remove last blank line
    while lines and lines[-1] == "":
        lines.pop()
 
    return "\n".join(lines)
 
 
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
 
 
def format_today_message(events: List[Event], day: dt.date) -> str:
    # emoji-indicator in header (sun/mood)
    header = f"<b>Доброго ранку шановні студенти!</b> ☀️\n🗓️ <b>Розклад на сьогодні ({fmt_date_short(day)})</b>\n\n"
    body = format_day(events, day)
    return header + body + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
 
 
def format_tomorrow_message(events: List[Event], day: dt.date) -> str:
    header = f"<b>Добрий вечір шановні студенти!</b> 🌙\n🗓️ <b>Розклад на завтра ({fmt_date_short(day)})</b>\n\n"
    body = format_day(events, day)
    return header + body + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
 
 
def format_week_message(events: List[Event], start_day: dt.date, end_day: dt.date) -> str:
    header = (
        f"🗓️ <b>Розклад на тиждень</b>\n"
        f"<b>{fmt_date_short(start_day)} – {fmt_date_short(end_day)}</b>\n\n"
    )
 
    # group events by day
    by_day: Dict[dt.date, List[Event]] = {start_day + dt.timedelta(days=i): [] for i in range((end_day - start_day).days + 1)}
    for ev in events:
        by_day[ev.start.astimezone(KYIV_TZ).date()].append(ev)
 
    blocks = []
    for i, d in enumerate(by_day.keys()):
        blocks.append(separator())
        blocks.append(format_day(by_day[d], d))
    blocks.append(separator())
 
    return header + "\n".join(blocks) + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
 
 
# ----------------------------
# Telegram
# ----------------------------
def tg_send_message(
    token: str,
    chat_id: str,
    text: str,
    message_thread_id: Optional[int] = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
 
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
 
 
# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["today", "tomorrow", "week"], help="Posting mode")
    args = parser.parse_args()
 
    token = env_required("TG_BOT_TOKEN")
    chat_id = env_required("TG_CHAT_ID")
    ics_url = env_required("GCAL_ICS_URL")
 
    schedule_thread_id = env_optional_int("TG_SCHEDULE_THREAD_ID")  # used for weekly thread posting
 
    # Load state
    state = load_state()
 
    # Fetch & parse calendar
    ics = fetch_ics(ics_url)
    all_events = parse_ics_events(ics)
 
    today = now_kyiv().date()
 
    if args.mode == "today":
        target = today
        stamp = f"today:{iso_date(target)}"
        if not should_post(state, "last_today", stamp):
            print("Already posted today schedule for this date. Exiting.")
            return
 
        day_events = events_in_range(all_events, target, target)
 
        # Weather block (today)
        weather = format_weather_block(target, "сьогодні")
        msg = "<b>Доброго ранку шановні студенти!</b> ☀️\n\n" + weather
        msg += f"🗓️ <b>Розклад на сьогодні ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
 
        tg_send_message(token, chat_id, msg, message_thread_id=None)
 
        mark_posted(state, "last_today", stamp)
        save_state(state)
        print("Posted today schedule.")
 
    elif args.mode == "tomorrow":
        target = today + dt.timedelta(days=1)
        stamp = f"tomorrow:{iso_date(target)}"
        if not should_post(state, "last_tomorrow", stamp):
            print("Already posted tomorrow schedule for this date. Exiting.")
            return
 
        day_events = events_in_range(all_events, target, target)
 
        # Weather block (tomorrow)
        weather = format_weather_block(target, "завтра")
        msg = "<b>Добрий вечір шановні студенти!</b> 🌙\n\n" + weather
        msg += f"🗓️ <b>Розклад на завтра ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
 
        tg_send_message(token, chat_id, msg, message_thread_id=None)
 
        mark_posted(state, "last_tomorrow", stamp)
        save_state(state)
        print("Posted tomorrow schedule.")
 
    elif args.mode == "week":
        # Week is Mon..Sun of the upcoming week (or current week if run on Sunday)
        # Since user wants Sunday post for next week: compute next Monday from 'today'
        # If today is Sunday: next day is Monday => next week start
        days_ahead = (0 - today.weekday()) % 7  # Monday=0
        this_monday = today + dt.timedelta(days=days_ahead)
        # If today is not Sunday and workflow triggers Sunday anyway, this still points to the next Monday
        if today.weekday() != 6:
            # safeguard: if someone manually runs on another day, post current week's remaining Mon..Sun
            this_monday = today - dt.timedelta(days=today.weekday())
        this_sunday = this_monday + dt.timedelta(days=6)
 
        stamp = f"week:{iso_date(this_monday)}:{iso_date(this_sunday)}"
        if not should_post(state, "last_week", stamp):
            print("Already posted weekly schedule for this week-range. Exiting.")
            return
 
        week_events = events_in_range(all_events, this_monday, this_sunday)
        msg = format_week_message(week_events, this_monday, this_sunday)
 
        # IMPORTANT: weekly should go to Schedule thread
        if schedule_thread_id is None:
            print("WARNING: TG_SCHEDULE_THREAD_ID not set. Weekly post will go to general chat.")
        tg_send_message(token, chat_id, msg, message_thread_id=schedule_thread_id)
 
        mark_posted(state, "last_week", stamp)
        save_state(state)
        print("Posted weekly schedule.")
 
 
if __name__ == "__main__":
    main()
