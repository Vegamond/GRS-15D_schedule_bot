import os
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
GCAL_ICS_URL = os.environ["GCAL_ICS_URL"]

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"


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


def fetch_ics() -> str:
    with urllib.request.urlopen(GCAL_ICS_URL, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


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
    Підтримка Google iCal:
    - 20260216T090000Z  -> UTC (Z) -> конвертуємо в Europe/Kyiv
    - 20260216T090000   -> локальний час календаря -> трактуємо як Europe/Kyiv
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
        summary = get("SUMMARY")
        location = get("LOCATION")
        description = get("DESCRIPTION")
        url = get("URL")

        if not url:
            m = re.search(r"https?://\S+", description)
            if m:
                url = m.group(0)

        ds = parse_dt(dtstart)
        de = parse_dt(dtend)

        if not ds:
            continue

        # фільтр на "сьогодні" по Києву
        if ds.astimezone(KYIV_TZ).date() != today:
            continue

        events.append({
            "start": ds,
            "end": de,
            "summary": summary or "Заняття",
            "location": location,
            "url": url,
        })

    events.sort(key=lambda x: x["start"])
    return events


def format_today(events):
    now_k = kyiv_now()
    header = f"📅 Розклад на сьогодні ({now_k.strftime('%d.%m')})\n"

    if not events:
        return header + "\nСьогодні в календарі порожньо 🫡"

    lines = [header]
    for i, e in enumerate(events, 1):
        st = e["start"].astimezone(KYIV_TZ).strftime("%H:%M")
        en = e["end"].astimezone(KYIV_TZ).strftime("%H:%M") if e["end"] else ""
        time_str = f"{st}–{en}" if en else st

        extra = []
        if e["location"]:
            extra.append(e["location"])
        if e["url"]:
            extra.append(e["url"])

        tail = ("\n   " + "\n   ".join(extra)) if extra else ""
        lines.append(f"{i}) {time_str} — {e['summary']}{tail}")

    return "\n".join(lines)


def main():
    # антиспам: постимо максимум 1 раз на день
    st = load_state()
    today_str = kyiv_now().strftime("%Y-%m-%d")
    if st.get("last_post_date") == today_str:
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
