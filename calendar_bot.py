import os
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
GCAL_ICS_URL = os.environ["GCAL_ICS_URL"]

# Україна зараз зазвичай EET/EEST, але в GitHub немає локальної TZ.
# Робимо простіше: беремо "сьогодні" за Києвом = UTC+2 (зараз зима).
# Якщо буде літній час — скажеш, підправимо на UTC+3 автоматично.
KYIV_OFFSET_HOURS = 2

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

def parse_events_for_today(ics_text: str):
    # Дуже простий парсер VEVENT з DTSTART/DTEND/SUMMARY/DESCRIPTION/LOCATION/URL
    # Працює для типового експорту Google Calendar.
    events = []

    # "сьогодні" за Києвом
    now_utc = datetime.now(timezone.utc)
    kyiv_now = now_utc + timedelta(hours=KYIV_OFFSET_HOURS)
    today = kyiv_now.date()

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

        # Google інколи кладе посилання в DESCRIPTION
        if not url:
            m = re.search(r"https?://\S+", description)
            if m:
                url = m.group(0)

        # Парсимо формат типу 20260214T090000Z або 20260214T090000
        def parse_dt(s):
            if not s:
                return None
            s = s.replace("Z", "")
            try:
                return datetime.strptime(s[:15], "%Y%m%dT%H%M%S")
            except ValueError:
                # fallback: дата без часу
                try:
                    return datetime.strptime(s[:8], "%Y%m%d")
                except ValueError:
                    return None

        ds = parse_dt(dtstart)
        de = parse_dt(dtend)

        if not ds:
            continue

        # Вважаємо, що час у календарі “локальний”, і показуємо як є
        if ds.date() != today:
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
    now_utc = datetime.now(timezone.utc)
    kyiv_now = now_utc + timedelta(hours=KYIV_OFFSET_HOURS)
    header = f"📅 Розклад на сьогодні ({kyiv_now.strftime('%d.%m')})\n"

    if not events:
        return header + "\nСьогодні в календарі порожньо 🫡"

    lines = [header]
    for i, e in enumerate(events, 1):
        st = e["start"].strftime("%H:%M")
        en = e["end"].strftime("%H:%M") if e["end"] else ""
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
    ics = fetch_ics()
    events = parse_events_for_today(ics)
    msg = format_today(events)
    tg_send(msg)

if __name__ == "__main__":
    main()
