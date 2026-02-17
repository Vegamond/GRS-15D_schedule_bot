import os
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
GCAL_ICS_URL = os.environ["GCAL_ICS_URL"]

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"

# Пост має вийти зранку після 08:00 (Kyiv)
POST_HOUR_KYIV = 8
POST_MINUTE_KYIV = 0


def kyiv_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(KYIV_TZ)


def tg_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()


def unfold_ics(text: str) -> str:
    """
    RFC5545 line folding: довгі рядки розбиваються, а продовження починається з пробілу/таб.
    Це критично для довгих Zoom-лінків.
    """
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
    Підтримка форматів Google iCal:
    - 20260216T090000Z  -> UTC (Z) -> конвертуємо в Europe/Kyiv
    - 20260216T090000   -> локальний час -> трактуємо як Europe/Kyiv
    - 20260216          -> all-day
    """
    if not s:
        return None
    s = s.strip()

    # all-day date
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
    else:
        return dt_naive.replace(tzinfo=KYIV_TZ)


# ---------------------------
# Парсинг "полів" як у твоєму дизайні
# ---------------------------

def parse_teacher(text: str) -> str:
    """
    Шукаємо викладача в описі:
    "доц. Примиченко В.Г."
    "проф. Іваненко І.І."
    "ст. викл. ..."
    """
    if not text:
        return ""
    t = text.strip()

    # найчастіші шаблони
    m = re.search(r"(?i)\b(доц\.|проф\.|викл\.|асист\.|ст\.?\s*викл\.)\s+[^\n,]+", t)
    return m.group(0).strip() if m else ""


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
    """
    Бере предмет з SUMMARY до "—" (довге тире).
    Напр: "Історія України — Лекція" -> "Історія України"
    """
    if not summary:
        return "Заняття"
    parts = [p.strip() for p in summary.split("—")]
    return parts[0] if parts and parts[0] else summary.strip()


def choose_one_url(description: str) -> str:
    """
    Вибираємо один лінк з DESCRIPTION:
    - якщо є zoom.us -> беремо ОСТАННІЙ zoom (часто саме він з pwd)
    - інакше -> останній будь-який
    """
    if not description:
        return ""
    urls = re.findall(r"https?://\S+", description)
    if not urls:
        return ""
    zoom_links = [u for u in urls if "zoom.us" in u]
    return zoom_links[-1] if zoom_links else urls[-1]


def detect_format(location: str, description: str) -> tuple[str, str]:
    """
    Повертає (format, place)
    format: Online / Offline / ""
    place: "Zoom" / "ауд. 207" / інше
    """
    loc = (location or "").strip()
    blob = f"{loc}\n{description}".lower()

    if "zoom" in blob or "online" in blob or "meet.google" in blob:
        place = "Zoom" if "zoom" in blob else "Online"
        return ("Online", place)

    # аудиторія з LOCATION
    m = re.search(r"(?i)\b(ауд\.?\s*\d+)\b", loc)
    if m:
        return ("Offline", m.group(1).replace("  ", " ").strip())

    if loc:
        return ("Offline", loc)

    return ("", "")


def parse_events_for_today(ics_text: str):
    events = []
    today = kyiv_now().date()

    blocks = ics_text.split("BEGIN:VEVENT")
    for b in blocks[1:]:
        b = b.split("END:VEVENT")[0]

        def get(field):
            m = re.search(rf"^{field}[^:]*:(.*)$", b, flags=re.MULTILINE)
            return m.group(1).strip() if m else ""

        dtstart = get("DTSTART")
        dtend = get("DTEND")
        summary = get("SUMMARY") or "Заняття"
        location = get("LOCATION") or ""
        description = (get("DESCRIPTION") or "")

        # Google часто зберігає перенос як "\n"
        description = description.replace("\\n", "\n")

        ds = parse_dt(dtstart)
        de = parse_dt(dtend)

        if not ds:
            continue
        if ds.astimezone(KYIV_TZ).date() != today:
            continue

        subject = parse_subject(summary)
        lesson_type = parse_lesson_type(summary, description)
        teacher = parse_teacher(description)
        url = choose_one_url(description)
        fmt, place = detect_format(location, description)

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


def format_today(events):
    now_k = kyiv_now()
    date_str = now_k.strftime("%d.%m")

    lines = []
    lines.append("Доброго ранку шановні студенти! ☀️")
    lines.append(f"📅 Розклад на сьогодні ({date_str})")
    lines.append("")

    if not events:
        lines.append("🟦 Сьогодні в календарі порожньо 🫡")
        lines.append(f"⏱ Оновлено: {now_k.strftime('%H:%M')}")
        return "\n".join(lines)

    for i, e in enumerate(events, 1):
        st = e["start"].astimezone(KYIV_TZ).strftime("%H:%M")
        en = e["end"].astimezone(KYIV_TZ).strftime("%H:%M") if e["end"] else ""
        time_str = f"{st}–{en}" if en else st

        # "картка" як у твоєму стилі
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


def should_post_now() -> bool:
    """
    Запускаємо workflow часто, але постимо тільки коли вже 08:00+ за Києвом.
    Це робить систему стійкою до “плаваючого” GitHub cron.
    """
    now = kyiv_now()
    return (now.hour > POST_HOUR_KYIV) or (now.hour == POST_HOUR_KYIV and now.minute >= POST_MINUTE_KYIV)


def main():
    st = load_state()
    today_str = kyiv_now().strftime("%Y-%m-%d")

    # антиспам: максимум 1 пост на добу
    if st.get("last_post_date") == today_str:
        return

    # постимо лише після 08:00 Київ
    if not should_post_now():
        return

    ics = fetch_ics()
    events = parse_events_for_today(ics)
    msg = format_today(events)
    tg_send(msg)

    st["last_post_date"] = today_str
    st["last_post_kyiv_time"] = kyiv_now().strftime("%Y-%m-%d %H:%M")
    save_state(st)


if __name__ == "__main__":
    main()
