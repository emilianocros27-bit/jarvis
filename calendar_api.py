"""Google Calendar access for Jarvis — read upcoming events and create new
ones on the primary calendar only. Note: this file is deliberately NOT named
`calendar.py` — that would shadow Python's stdlib `calendar` module (used
internally by email/http machinery) for the whole process.
"""
import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_auth import get_credentials

DEFAULT_LOOKAHEAD_DAYS = 7
DEFAULT_DURATION_MINUTES = 60

DAY_NAMES_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTH_NAMES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


class CalendarError(Exception):
    pass


def _service():
    try:
        return build("calendar", "v3", credentials=get_credentials())
    except Exception as e:
        raise CalendarError(str(e))


def local_tz():
    """Fixed-offset tzinfo matching the machine's current local timezone
    (correct for near-future events; does not track DST transitions far out)."""
    return datetime.datetime.now().astimezone().tzinfo


def format_day_label(d):
    return f"{DAY_NAMES_ES[d.weekday()]} {d.day} de {MONTH_NAMES_ES[d.month]}"


def format_dt_label(dt):
    return f"{DAY_NAMES_ES[dt.weekday()]} {dt.day} de {MONTH_NAMES_ES[dt.month]}, {dt.strftime('%H:%M')}"


def _parse_event_start(start_field):
    """Google returns either {'dateTime': iso, 'timeZone': ...} (timed) or
    {'date': 'YYYY-MM-DD'} (all-day)."""
    if "dateTime" in start_field:
        return datetime.datetime.fromisoformat(start_field["dateTime"]), False
    return datetime.datetime.fromisoformat(start_field["date"]), True


def get_upcoming_events(days=DEFAULT_LOOKAHEAD_DAYS):
    """Returns [{day_label, events: [{summary, time_label}]}] for the next
    `days` days on the primary calendar, in chronological order."""
    service = _service()
    now = datetime.datetime.now(datetime.timezone.utc)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days)).isoformat()

    try:
        events = []
        page_token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                )
                .execute()
            )
            events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        raise CalendarError(str(e))

    by_day = {}
    order = []
    for ev in events:
        dt, all_day = _parse_event_start(ev.get("start", {}))
        # Google sometimes echoes an event's dateTime back in UTC (calendar-
        # timezone-dependent) even when it was created with a local offset —
        # always display/bucket in the machine's local time, never raw UTC
        if not all_day:
            dt = dt.astimezone()
        day_key = dt.date().isoformat()
        if day_key not in by_day:
            by_day[day_key] = []
            order.append(day_key)
        time_label = "todo el día" if all_day else dt.strftime("%H:%M")
        by_day[day_key].append({
            "summary": ev.get("summary", "(sin título)"),
            "time_label": time_label,
            "start_iso": None if all_day else dt.isoformat(),
        })

    return [
        {"day_label": format_day_label(datetime.date.fromisoformat(day_key)), "events": by_day[day_key]}
        for day_key in order
    ]


def create_event(title, start, duration_minutes=DEFAULT_DURATION_MINUTES):
    """start is a timezone-aware datetime.datetime. Returns
    {summary, start_label, end_label, html_link}."""
    end = start + datetime.timedelta(minutes=duration_minutes)

    service = _service()
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }

    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
    except HttpError as e:
        raise CalendarError(str(e))

    return {
        "summary": created.get("summary", title),
        "start_label": format_dt_label(start),
        "end_label": end.strftime("%H:%M"),
        "html_link": created.get("htmlLink"),
    }
