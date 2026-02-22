import os
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta, time, date
from zoneinfo import ZoneInfo

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
GCAL_ICS_URL = os.environ["GCAL_ICS_URL"]

# today | tomorrow | week
MODE = os.environ.get("MODE", "today").strip().lower()

# optional: topic/thread id for "Розклад"
TG_SCHEDULE_THREAD_ID = os.environ.get("TG_SCHEDULE_THREAD_ID")  # e.g. "1234"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"

POST_RULES = {
    "today": (8, 0),      # після 08:00 Kyiv
    "tomorrow": (19, 0),  # після 19:00 Kyiv
}


def kyiv_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(KYIV_TZ)


def tg_send(text: str, thread_id: str | None = None) -> None:
    """
    If thread_id is provided, message goes to that forum topic.
    """
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    data = urllib.parse.urlencode(payload).encode("utf-8")
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()


def unfold_ics(text: str) -> str:
    # RFC5545 line folding fix
    return (
        text.replace("\r\n ", "")
            .replace("\n ", "")
            .replace("\r\n\t", "")
            .replace("\n\t", "")
    )


def fetch_ics() -> str:
    with urllib.request.urlopen(GCAL_ICS_URL, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return unfold_ics(raw)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(st: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def parse_dt(s: str):
    """
    Google iCal:
    - 20260216T090000Z  -> UTC (Z)
    - 20260216T090000   -> local (assume Kyiv)
    - 20260216          -> all-day
    """
    if not s:
        return None
    s = s.strip()

    if len(s) == 8 and s.isdigit():
        d = datetime.strptime(s, "%Y%m%d").date()
        return datetime(d.year, d.month, d.day, 0, 0, tzinfo=KYIV_TZ)

    is_utc = s.endswith("Z")
    if is_utc:
        s = s[:-1]

    try:
        dt_naive = datetime.strptime(s[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None

    if is_utc:
        return dt_naive.replace(tzinfo=timezone.utc).astimezone(KYIV_TZ)
    return dt_naive.replace(tzinfo=KYIV_TZ)


# ---------------------------
# Weather (Dnipro) — Open-Meteo (no key)
# ---------------------------

WEATHER_CODE_UA = {
    0: "ясно",
    1: "переважно ясно",
    2: "мінлива хмарність",
    3: "хмарно",
    45: "туман",
    48: "паморозь / туман",
    51: "мряка (слабка)",
    53: "мряка (помірна)",
    55: "мряка (сильна)",
    56: "крижана мряка (слабка)",
    57: "крижана мряка (сильна)",
    61: "дощ (слабкий)",
    63: "дощ (помірний)",
    65: "дощ (сильний)",
    66: "крижаний дощ (слабкий)",
    67: "крижаний дощ (сильний)",
    71: "сніг (слабкий)",
    73: "сніг (помірний)",
    75: "сніг (сильний)",
    77: "снігова крупа",
    80: "зливи (слабкі)",
    81: "зливи (помірні)",
    82: "зливи (сильні)",
    85: "снігопад (слабкий)",
    86: "снігопад (сильний)",
    95: "гроза",
    96: "гроза з градом (слабка)",
    99: "гроза з градом (сильна)",
}


def _fmt_int(x):
    if x is None:
        return None
    try:
        return int(round(float(x)))
    except Exception:
        return None


def emoji_for_weathercode(code: int | None) -> str:
    if code is None:
        return "🌤️"
    if code == 0:
        return "☀️"
    if code in (1, 2):
        return "🌤️"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if code in (51, 53, 55, 56, 57):
        return "🌦️"
    if code in (61, 63, 65, 80, 81, 82, 66, 67):
        return "🌧️"
    if code in (71, 73, 75, 77, 85, 86):
        return "❄️"
    if code in (95, 96, 99):
        return "⛈️"
    return "🌤️"


def fetch_dnipro_weather(day_offset: int) -> dict:
    """
    day_offset:
      0 = today
      1 = tomorrow
    """
    lat, lon = 48.45, 34.98
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature,weathercode,wind_speed_10m"
        "&daily=weathercode,temperature_2m_min,temperature_2m_max,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (ScheduleBot; GitHub Actions)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}

    try:
        cur = data.get("current", {})
        daily = data.get("daily", {})

        codes = daily.get("weathercode") or []
        tmin = daily.get("temperature_2m_min") or []
        tmax = daily.get("temperature_2m_max") or []
        pmax = daily.get("precipitation_probability_max") or []

        code_daily = codes[day_offset] if len(codes) > day_offset else None
        desc = WEATHER_CODE_UA.get(code_daily, "погода")

        out = {
            "code": code_daily,
            "desc": desc,
            "tmin": _fmt_int(tmin[day_offset]) if len(tmin) > day_offset else None,
            "tmax": _fmt_int(tmax[day_offset]) if len(tmax) > day_offset else None,
            "precip_prob": _fmt_int(pmax[day_offset]) if len(pmax) > day_offset else None,
        }

        if day_offset == 0:
            out["temp"] = _fmt_int(cur.get("temperature_2m"))
            out["feels"] = _fmt_int(cur.get("apparent_temperature"))
            out["wind"] = _fmt_int(cur.get("wind_speed_10m"))

        return out
    except Exception:
        return {}


def format_weather_block_dnipro(day_offset: int) -> list[str]:
    w = fetch_dnipro_weather(day_offset)
    if not w:
        return []

    when = "на сьогодні" if day_offset == 0 else "на завтра"
    lines = [f"🌤 Погода в Дніпрі {when}:"]

    if day_offset == 0 and w.get("temp") is not None:
        lines.append(f"{w['temp']}°C, {w.get('desc', 'погода')}")
        if w.get("feels") is not None:
            lines.append(f"🌡 Відчувається як: {w['feels']}°C")
        if w.get("wind") is not None:
            lines.append(f"💨 Вітер: {w['wind']} м/с")
    else:
        lines.append(w.get("desc", "погода"))

    if w.get("tmin") is not None and w.get("tmax") is not None:
        lines.append(f"📈 Мін/Макс: {w['tmin']}°C / {w['tmax']}°C")

    if w.get("precip_prob") is not None:
        lines.append(f"☔ Ймовірність опадів: {w['precip_prob']}%")

    lines.append("")
    return lines


def weather_emoji_dnipro(day_offset: int) -> str:
    w = fetch_dnipro_weather(day_offset)
    if not w:
        return "🌤️"
    return emoji_for_weathercode(w.get("code"))


# ---------------------------
# Schedule parsing from ICS
# ---------------------------

def parse_teacher_from_text(summary: str, location: str, description: str) -> str:
    blob = "\n".join([summary or "", location or "", description or ""]).strip()
    if not blob:
        return ""

    m = re.search(r"(?im)^\s*(викладач|лектор|teacher)\s*[:\-]\s*(.+)$", blob)
    if m:
        val = m.group(2).strip()
        val = val.split("\n")[0].strip()
        return val

    m = re.search(r"(?i)\b(доц\.|проф\.|викл\.|асист\.|ст\.?\s*викл\.)\s+[^\n,]+", blob)
    if m:
        return m.group(0).strip()

    m = re.search(r"(?i)\b[А-ЯІЇЄҐ][а-яіїєґ’\-]+\s+[А-ЯІЇЄҐ]\.[А-ЯІЇЄҐ]\.?\b", blob)
    if m:
        return m.group(0).strip()

    return ""


def parse_teacher_from_attendee(event_block: str) -> str:
    matches = re.findall(r"ATTENDEE;[^:\n]*CN=(\"[^\"]+\"|[^;:\n]+)", event_block)
    if not matches:
        return ""
    cn = matches[0].strip()
    if cn.startswith('"') and cn.endswith('"'):
        cn = cn[1:-1]
    return cn.strip()


def parse_lesson_type(summary: str, desc: str) -> str:
    blob = f"{summary}\n{desc}".lower()
    if "лекц" in blob:
        return "Лекція"
    if "практ" in blob:
        return "Практика"
    if "семін" in blob:
        return "Семінар"
    if "лаб" in blob:
        return "Лабораторна"
    return ""


def parse_subject(summary: str) -> str:
    if not summary:
        return "Заняття"
    parts = [p.strip() for p in summary.split("—")]
    return parts[0] if parts and parts[0] else summary.strip()


def choose_one_url(description: str) -> str:
    if not description:
        return ""
    urls = re.findall(r"https?://\S+", description)
    if not urls:
        return ""
    zoom_links = [u for u in urls if "zoom.us" in u]
    return zoom_links[-1] if zoom_links else urls[-1]


def detect_format(location: str, description: str) -> tuple[str, str]:
    loc = (location or "").strip()
    blob = f"{loc}\n{description}".lower()

    if "zoom" in blob or "online" in blob or "meet.google" in blob:
        place = "Zoom" if "zoom" in blob else "Online"
        return ("Online", place)

    m = re.search(r"(?i)\b(ауд\.?\s*\d+)\b", loc)
    if m:
        return ("Offline", m.group(1).replace("  ", " ").strip())

    if loc:
        return ("Offline", loc)

    return ("", "")


def parse_events_for_date(ics_text: str, target_date: date):
    events = []
    blocks = ics_text.split("BEGIN:VEVENT")

    for raw in blocks[1:]:
        block = raw.split("END:VEVENT")[0]

        def get(field):
            m = re.search(rf"^{field}[^:]*:(.*)$", block, flags=re.MULTILINE)
            return m.group(1).strip() if m else ""

        dtstart = get("DTSTART")
        dtend = get("DTEND")
        summary = get("SUMMARY") or "Заняття"
        location = get("LOCATION") or ""
        description = (get("DESCRIPTION") or "").replace("\\n", "\n")

        ds = parse_dt(dtstart)
        de = parse_dt(dtend)
        if not ds:
            continue

        if ds.astimezone(KYIV_TZ).date() != target_date:
            continue

        subject = parse_subject(summary)
        lesson_type = parse_lesson_type(summary, description)
        url = choose_one_url(description)
        fmt, place = detect_format(location, description)

        teacher = parse_teacher_from_text(summary, location, description)
        if not teacher:
            teacher = parse_teacher_from_attendee(block)

        events.append({
            "start": ds,
            "end": de,
            "subject": subject,
            "lesson_type": lesson_type,
            "teacher": teacher,
            "format": fmt,
            "place": place,
            "url": url,
        })

    events.sort(key=lambda x: x["start"])
    return events


# ---------------------------
# Formatting
# ---------------------------

def format_day_schedule(events, target_date: date, mode: str) -> str:
    now_k = kyiv_now()
    date_str = target_date.strftime("%d.%m")

    lines = []
    if mode == "tomorrow":
        lines.append("Добрий вечір шановні студенти! 🌙")
        lines.append("")
        lines.extend(format_weather_block_dnipro(1))
        wx = weather_emoji_dnipro(1)
        lines.append(f"📅 {wx} Розклад на завтра ({date_str})")
        lines.append("")
    else:
        lines.append("Доброго ранку шановні студенти! ☀️")
        lines.append("")
        lines.extend(format_weather_block_dnipro(0))
        wx = weather_emoji_dnipro(0)
        lines.append(f"📅 {wx} Розклад на сьогодні ({date_str})")
        lines.append("")

    if not events:
        lines.append("🟦 У календарі порожньо 🫡")
        lines.append(f"⏱ Оновлено: {now_k.strftime('%H:%M')}")
        return "\n".join(lines)

    for i, e in enumerate(events, 1):
        st = e["start"].astimezone(KYIV_TZ).strftime("%H:%M")
        en = e["end"].astimezone(KYIV_TZ).strftime("%H:%M") if e["end"] else ""
        time_str = f"{st}–{en}" if en else st

        lines.append(f"📌 {i}. {e['subject'].upper()}")
        lines.append(f"🕒 {time_str}")

        if e.get("lesson_type"):
            lines.append(f"📚 {e['lesson_type']}")

        if e.get("teacher"):
            lines.append(f"👨‍🏫 {e['teacher']}")

        if e.get("format") == "Online":
            lines.append(f"🌐 Online ({e.get('place') or 'Zoom'})")
        elif e.get("format") == "Offline":
            lines.append(f"🏫 {e.get('place') or 'Аудиторія'}")

        if e.get("url"):
            lines.append(f"🔗 {e['url']}")

        if i != len(events):
            lines.append("──────────────")

    lines.append("")
    lines.append(f"⏱ Оновлено: {now_k.strftime('%H:%M')}")
    return "\n".join(lines)


def next_week_monday_from_sunday(now_k: datetime) -> date:
    """
    If run on Sunday, returns next Monday.
    If run other day, still returns next Monday (upcoming).
    """
    # weekday: Mon=0 ... Sun=6
    days_until_monday = (7 - now_k.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    return now_k.date() + timedelta(days=days_until_monday)


def format_week_schedule(ics: str, monday: date) -> str:
    start = monday
    end = monday + timedelta(days=6)

    lines = []
    lines.append("📚 Розклад на тиждень")
    lines.append(f"🗓 {start.strftime('%d.%m')} – {end.strftime('%d.%m')}")
    lines.append("")

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    any_events = False

    for i in range(7):
        d = start + timedelta(days=i)
        events = parse_events_for_date(ics, d)

        lines.append(f"🟦 {day_names[i]} • {d.strftime('%d.%m')}")
        if not events:
            lines.append("  — (пар немає)")
            lines.append("")
            continue

        any_events = True
        for e in events:
            st = e["start"].astimezone(KYIV_TZ).strftime("%H:%M")
            en = e["end"].astimezone(KYIV_TZ).strftime("%H:%M") if e["end"] else ""
            time_str = f"{st}–{en}" if en else st

            place = ""
            if e.get("format") == "Online":
                place = "🌐 Online"
            elif e.get("format") == "Offline":
                pl = (e.get("place") or "").strip()
                place = f"🏫 {pl}" if pl else "🏫 Offline"

            lines.append(f"  🕒 {time_str} • {e['subject']}" + (f" • {place}" if place else ""))

        lines.append("")

    if not any_events:
        lines.append("🫡 На тиждень подій не знайдено.")

    lines.append(f"⏱ Оновлено: {kyiv_now().strftime('%H:%M')}")
    return "\n".join(lines)


# ---------------------------
# Posting rules
# ---------------------------

def should_post_now(mode: str) -> bool:
    h, m = POST_RULES.get(mode, (8, 0))
    now = kyiv_now()
    return (now.hour > h) or (now.hour == h and now.minute >= m)


def main():
    st = load_state()
    now = kyiv_now()

    # WEEKLY MODE (post to topic)
    if MODE == "week":
        monday = next_week_monday_from_sunday(now)
        stamp_key = "last_post_week_monday"
        stamp_val = monday.strftime("%Y-%m-%d")

        if st.get(stamp_key) == stamp_val:
            return

        ics = fetch_ics()
        msg = format_week_schedule(ics, monday)

        # If no thread id provided, fallback to main chat
        tg_send(msg, thread_id=TG_SCHEDULE_THREAD_ID)

        st[stamp_key] = stamp_val
        st[f"{stamp_key}_kyiv_time"] = kyiv_now().strftime("%Y-%m-%d %H:%M")
        save_state(st)
        return

    # DAILY MODES
    if MODE == "tomorrow":
        target_date = now.date() + timedelta(days=1)
        stamp_key = "last_post_tomorrow_date"
    else:
        target_date = now.date()
        stamp_key = "last_post_today_date"

    target_str = target_date.strftime("%Y-%m-%d")

    if st.get(stamp_key) == target_str:
        return

    if not should_post_now(MODE):
        return

    ics = fetch_ics()
    events = parse_events_for_date(ics, target_date)
    msg = format_day_schedule(events, target_date, MODE)
    tg_send(msg)

    st[stamp_key] = target_str
    st[f"{stamp_key}_kyiv_time"] = kyiv_now().strftime("%Y-%m-%d %H:%M")
    save_state(st)


if __name__ == "__main__":
    main()
