import os
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


# ----------------------------
# Helpers: IO / state
# ----------------------------
STATE_FILE = "state.json"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def state_key_for_mode(mode: str) -> str:
    return f"last_post_{mode}"

def already_posted_today(state: dict, mode: str) -> bool:
    key = state_key_for_mode(mode)
    today_str = datetime.now(KYIV_TZ).date().isoformat()
    return state.get(key) == today_str

def mark_posted_today(state: dict, mode: str):
    key = state_key_for_mode(mode)
    state[key] = datetime.now(KYIV_TZ).date().isoformat()


# ----------------------------
# Telegram
# ----------------------------
def tg_send_message(text: str, thread_id: int | None = None, disable_preview: bool = True):
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError("Missing TG_BOT_TOKEN or TG_CHAT_ID in environment.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True if disable_preview else False,
    }

    # IMPORTANT: for forum topics/threads
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)

    data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return body


# ----------------------------
# Weather (Dnipro) via Open-Meteo (no key)
# ----------------------------
def fetch_weather(day: date) -> dict | None:
    # Dnipro approx coords
    lat, lon = 48.4647, 35.0462
    # Open-Meteo daily forecast
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        daily = data.get("daily", {})
        times = daily.get("time", [])
        if not times:
            return None

        day_str = day.isoformat()
        if day_str not in times:
            return None

        i = times.index(day_str)
        wcode = daily.get("weathercode", [None])[i]
        tmax = daily.get("temperature_2m_max", [None])[i]
        tmin = daily.get("temperature_2m_min", [None])[i]
        pop = daily.get("precipitation_probability_max", [None])[i]

        return {
            "weathercode": wcode,
            "tmax": tmax,
            "tmin": tmin,
            "pop": pop,
        }
    except Exception:
        return None

def weather_emoji(wcode: int | None) -> str:
    # Very rough mapping
    if wcode is None:
        return "🌡️"
    if wcode in (0, 1):
        return "☀️"
    if wcode in (2, 3):
        return "⛅"
    if wcode in (45, 48):
        return "🌫️"
    if wcode in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        return "🌧️"
    if wcode in (66, 67):
        return "🌧️❄️"
    if wcode in (71, 73, 75, 77, 85, 86):
        return "❄️"
    if wcode in (95, 96, 99):
        return "⛈️"
    return "🌦️"

def weather_text(mode: str) -> str:
    # mode: today / tomorrow
    now = datetime.now(KYIV_TZ)
    day = now.date() if mode == "today" else (now.date() + timedelta(days=1))

    w = fetch_weather(day)
    if not w:
        return ""

    em = weather_emoji(w["weathercode"])
    pop = w.get("pop")
    tmin = w.get("tmin")
    tmax = w.get("tmax")

    day_label = "сьогодні" if mode == "today" else "завтра"
    parts = []
    parts.append(f"{em} Погода в Дніпрі на {day_label}:")
    if tmin is not None and tmax is not None:
        parts.append(f"🌡️ Мін/Макс: {int(round(tmin))}°C / {int(round(tmax))}°C")
    if pop is not None:
        parts.append(f"☔ Ймовірність опадів: {int(round(pop))}%")
    return "\n".join(parts)


# ----------------------------
# ICS parsing
# ----------------------------
def fetch_ics_text() -> str:
    ics_url = os.environ.get("GCAL_ICS_URL", "").strip()
    if not ics_url:
        raise RuntimeError("Missing GCAL_ICS_URL in environment.")
    with urllib.request.urlopen(ics_url, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return raw

def unfold_ics_lines(ics: str) -> list[str]:
    # ICS line folding: lines that start with space are continuation
    lines = ics.splitlines()
    out = []
    for line in lines:
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line.strip())
        else:
            out.append(line.rstrip("\r\n"))
    return out

def parse_ics_datetime(value: str) -> datetime | None:
    # Supports:
    #  - 20260218T090000Z
    #  - 20260218T090000
    #  - 20260218 (all-day)
    value = value.strip()
    if not value:
        return None
    try:
        if value.endswith("Z") and "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(KYIV_TZ)
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=KYIV_TZ)
            return dt
        # all-day
        d = datetime.strptime(value, "%Y%m%d").date()
        return datetime(d.year, d.month, d.day, 0, 0, tzinfo=KYIV_TZ)
    except Exception:
        return None

def parse_event_block(block: list[str]) -> dict:
    ev = {
        "summary": "",
        "description": "",
        "location": "",
        "dtstart": None,
        "dtend": None,
    }
    for line in block:
        if line.startswith("SUMMARY"):
            ev["summary"] = line.split(":", 1)[-1].strip()
        elif line.startswith("DESCRIPTION"):
            ev["description"] = line.split(":", 1)[-1].replace("\\n", "\n").strip()
        elif line.startswith("LOCATION"):
            ev["location"] = line.split(":", 1)[-1].replace("\\n", "\n").strip()
        elif line.startswith("DTSTART"):
            val = line.split(":", 1)[-1].strip()
            ev["dtstart"] = parse_ics_datetime(val)
        elif line.startswith("DTEND"):
            val = line.split(":", 1)[-1].strip()
            ev["dtend"] = parse_ics_datetime(val)
    return ev

def parse_ics_events(ics: str) -> list[dict]:
    lines = unfold_ics_lines(ics)
    events = []
    cur = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = []
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(parse_event_block(cur))
            cur = None
        else:
            if cur is not None:
                cur.append(line)
    # filter invalid
    out = []
    for e in events:
        if e["dtstart"] is None:
            continue
        out.append(e)
    return out


# ----------------------------
# Extract structured fields from description
# ----------------------------
TEACHER_RE = re.compile(r"(?im)^(?:викл\.|доц\.|проф\.|ас\.\s*проф\.|ст\.\s*викл\.)\s*(.+)$")
ACCESS_CODE_RE = re.compile(r"(?im)код\s*доступу\s*[:\-]\s*(.+)$")
URL_RE = re.compile(r"https?://\S+")

def extract_teacher(description: str) -> str | None:
    if not description:
        return None
    m = TEACHER_RE.search(description)
    if m:
        return m.group(1).strip()
    # Sometimes teacher is like "Доц. Плещан Х.В." (capitalized)
    m2 = re.search(r"(?im)^(Доц\.|Викл\.)\s*(.+)$", description)
    if m2:
        return m2.group(0).strip()
    return None

def extract_access_code(description: str) -> str | None:
    if not description:
        return None
    m = ACCESS_CODE_RE.search(description)
    if m:
        return m.group(1).strip()
    return None

def extract_links(description: str) -> list[str]:
    if not description:
        return []
    links = URL_RE.findall(description)
    # de-dup, keep order
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def normalize_lesson_type(summary: str, description: str) -> str:
    text = f"{summary}\n{description}".lower()
    if "практ" in text:
        return "Практичне"
    if "лекц" in text:
        return "Лекція"
    if "сем" in text:
        return "Семінар"
    return "Заняття"

def format_location(description: str, location: str) -> str:
    # If location already has Online / Zoom / ауд.
    text = f"{location}\n{description}".lower()
    if "online" in text or "zoom" in text:
        # try to include "(Zoom)" and/or "ауд."
        # Keep short:
        if "ауд" in text:
            # find "ауд. 207" etc
            m = re.search(r"(ауд\.?\s*\d+)", f"{location}\n{description}", flags=re.IGNORECASE)
            if m:
                return f"Online (Zoom, {m.group(1)})"
        return "Online (Zoom)"
    if location:
        return location.strip()
    return "Аудиторія (не вказано)"


# ----------------------------
# Formatting
# ----------------------------
WEEKDAY_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def fmt_date_short(d: date) -> str:
    return d.strftime("%d.%m")

def greeting_for_mode(mode: str) -> str:
    if mode == "today":
        return "Доброго ранку шановні студенти! ☀️"
    if mode == "tomorrow":
        return "Добрий вечір шановні студенти! 🌙"
    if mode == "week":
        return "🗓️ Розклад на тиждень"
    return "Розклад"

def header_icon_for_mode(mode: str) -> str:
    if mode == "today":
        return "📅"
    if mode == "tomorrow":
        return "📅"
    if mode == "week":
        return "🗓️"
    return "📅"

def format_day_schedule(events: list[dict], day: date, title_prefix: str) -> str:
    # Sort events by start time
    day_events = [e for e in events if e["dtstart"].date() == day]
    day_events.sort(key=lambda x: x["dtstart"])

    title = f"{header_icon_for_mode('today')} {title_prefix} ({day.strftime('%d.%m')})"
    lines = [title]

    if not day_events:
        lines.append("— Пар немає ✅")
        return "\n".join(lines)

    for idx, e in enumerate(day_events, start=1):
        summary = (e.get("summary") or "").strip()
        desc = (e.get("description") or "").strip()
        loc = (e.get("location") or "").strip()

        dt1 = e["dtstart"]
        dt2 = e.get("dtend") or (dt1 + timedelta(minutes=80))

        lesson_type = normalize_lesson_type(summary, desc)
        teacher = extract_teacher(desc)
        access = extract_access_code(desc)
        links = extract_links(desc)

        place = format_location(desc, loc)

        # Subject line
        lines.append("")
        lines.append(f"📌 {idx}. {summary.upper() if summary else 'ЗАНЯТТЯ'}")
        lines.append(f"🕒 {fmt_time(dt1)}–{fmt_time(dt2)}")
        lines.append(f"📚 {lesson_type}")
        if teacher:
            lines.append(f"👨‍🏫 {teacher}")
        lines.append(f"🌐 {place}")

        # link: keep only the first link if many
        if links:
            lines.append(f"🔗 {links[0]}")
        if access:
            lines.append(f"🔐 Код доступу: {access}")

    lines.append("")
    lines.append(f"🕘 Оновлено: {datetime.now(KYIV_TZ).strftime('%H:%M')}")
    return "\n".join(lines)

def format_week_schedule(events: list[dict], start_day: date) -> str:
    # Week: Mon..Sun of the week containing start_day
    monday = start_day - timedelta(days=start_day.weekday())
    sunday = monday + timedelta(days=6)

    lines = [greeting_for_mode("week")]
    lines.append(f"{fmt_date_short(monday)} – {fmt_date_short(sunday)}")
    lines.append("")

    # group per day
    for i in range(7):
        d = monday + timedelta(days=i)
        day_events = [e for e in events if e["dtstart"].date() == d]
        day_events.sort(key=lambda x: x["dtstart"])

        wd = WEEKDAY_UA[i]
        lines.append(f"🟦 {wd} • {fmt_date_short(d)}")

        if not day_events:
            lines.append("— (пар немає)")
            lines.append("")
            continue

        for e in day_events:
            summary = (e.get("summary") or "").strip()
            desc = (e.get("description") or "").strip()
            loc = (e.get("location") or "").strip()

            dt1 = e["dtstart"]
            dt2 = e.get("dtend") or (dt1 + timedelta(minutes=80))

            lesson_type = normalize_lesson_type(summary, desc)
            teacher = extract_teacher(desc)
            place = format_location(desc, loc)
            access = extract_access_code(desc)
            links = extract_links(desc)

            # компактно, але структурно (як ти просив)
            lines.append(f"🕒 {fmt_time(dt1)}–{fmt_time(dt2)}")
            lines.append(f"📌 {summary}")
            lines.append(f"📚 {lesson_type}")
            if teacher:
                lines.append(f"👨‍🏫 {teacher}")
            lines.append(f"🌐 {place}")
            if links:
                lines.append(f"🔗 {links[0]}")
            if access:
                lines.append(f"🔐 Код доступу: {access}")
            lines.append("")

        lines.append("")

    lines.append(f"🕘 Оновлено: {datetime.now(KYIV_TZ).strftime('%H:%M')}")
    return "\n".join(lines)


# ----------------------------
# Main
# ----------------------------
def main():
    mode = (os.environ.get("MODE") or "").strip().lower()
    if mode not in ("today", "tomorrow", "week"):
        # allow CLI: python calendar_bot.py today
        import sys
        if len(sys.argv) >= 2:
            mode = sys.argv[1].strip().lower()
    if mode not in ("today", "tomorrow", "week"):
        raise RuntimeError("MODE must be one of: today, tomorrow, week")

    state = load_state()

    # Prevent duplicates (but allow manual FORCE=1)
    force = os.environ.get("FORCE", "0").strip() == "1"
    if not force and already_posted_today(state, mode):
        print(f"Already posted for mode={mode} today. Exiting.")
        return

    ics = fetch_ics_text()
    events = parse_ics_events(ics)

    now = datetime.now(KYIV_TZ)
    if mode == "today":
        day = now.date()
        msg_parts = [greeting_for_mode("today")]
        w = weather_text("today")
        if w:
            msg_parts.append("")
            msg_parts.append(w)
            msg_parts.append("")
        msg_parts.append(format_day_schedule(events, day, "Розклад на сьогодні"))
        message = "\n".join(msg_parts)
        # Post to general (no thread_id)
        tg_send_message(message, thread_id=None, disable_preview=True)

    elif mode == "tomorrow":
        day = now.date() + timedelta(days=1)
        msg_parts = [greeting_for_mode("tomorrow")]
        w = weather_text("tomorrow")
        if w:
            msg_parts.append("")
            msg_parts.append(w)
            msg_parts.append("")
        msg_parts.append(format_day_schedule(events, day, "Розклад на завтра"))
        message = "\n".join(msg_parts)
        tg_send_message(message, thread_id=None, disable_preview=True)

    elif mode == "week":
        # IMPORTANT: post to Schedule thread
        thread_id_raw = os.environ.get("TG_SCHEDULE_THREAD_ID", "").strip()
        if not thread_id_raw:
            raise RuntimeError("Missing TG_SCHEDULE_THREAD_ID for weekly posts.")
        thread_id = int(thread_id_raw)

        message = format_week_schedule(events, now.date())
        tg_send_message(message, thread_id=thread_id, disable_preview=True)

    mark_posted_today(state, mode)
    save_state(state)
    print("Posted OK.")

if __name__ == "__main__":
    main()
