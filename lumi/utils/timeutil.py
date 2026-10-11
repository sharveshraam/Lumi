"""Time helpers.

LUMI stores timestamps as ISO-8601 strings in **local time without an offset**
(``2026-10-11T18:00:00``).  Reminders, tasks and focus sessions are all things a
person reasons about in local time ("tomorrow at 6 PM"), and the application runs
on a single machine owned by one person, so local-naive keeps the code honest and
readable.  :func:`parse_user_datetime` always requires an explicit *now* so that
behaviour is deterministic in tests.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"


def now() -> datetime:
    """Current local naive datetime (single injection point for tests)."""
    return datetime.now().replace(microsecond=0)


def today() -> date:
    return now().date()


def to_iso(value: datetime | date | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).strftime(ISO_FORMAT)
    return value.strftime(DATE_FORMAT)


def from_iso(value: str | None) -> Optional[datetime]:
    """Parse a stored timestamp.  Tolerant of ``Z`` and fractional seconds."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    text = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (ISO_FORMAT, DATE_FORMAT, "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(microsecond=0, tzinfo=None)


#: Readable alias used across the codebase for the reverse of :func:`to_iso`.
parse_iso = from_iso


def start_of_day(value: datetime | date) -> datetime:
    d = value.date() if isinstance(value, datetime) else value
    return datetime.combine(d, time.min)


def end_of_day(value: datetime | date) -> datetime:
    return start_of_day(value) + timedelta(days=1) - timedelta(seconds=1)


def is_same_day(a: datetime, b: datetime) -> bool:
    return a.date() == b.date()


def humanise_delta(target: datetime, *, reference: datetime | None = None) -> str:
    """Human wording such as ``in 2 hours`` / ``3 days ago``."""
    ref = reference or now()
    seconds = (target - ref).total_seconds()
    future = seconds >= 0
    seconds = abs(seconds)

    if seconds < 45:
        return "in a moment" if future else "just now"
    if seconds < 90:
        return "in a minute" if future else "a minute ago"
    minutes = seconds / 60
    if minutes < 60:
        value = int(round(minutes))
        label = f"{value} minute{'s' if value != 1 else ''}"
        return f"in {label}" if future else f"{label} ago"
    hours = minutes / 60
    if hours < 24:
        value = int(round(hours))
        label = f"{value} hour{'s' if value != 1 else ''}"
        return f"in {label}" if future else f"{label} ago"
    days = hours / 24
    if days < 14:
        value = int(round(days))
        label = f"{value} day{'s' if value != 1 else ''}"
        return f"in {label}" if future else f"{label} ago"
    weeks = days / 7
    if weeks < 9:
        value = int(round(weeks))
        label = f"{value} week{'s' if value != 1 else ''}"
        return f"in {label}" if future else f"{label} ago"
    months = days / 30.44
    value = max(1, int(round(months)))
    label = f"{value} month{'s' if value != 1 else ''}"
    return f"in {label}" if future else f"{label} ago"


def humanise_due(target: Optional[datetime], *, reference: datetime | None = None) -> str:
    """Wording used in the task list (``Today 18:00``, ``Overdue by 2 days``)."""
    if target is None:
        return "No due date"
    ref = reference or now()
    time_part = target.strftime("%H:%M")
    if is_same_day(target, ref):
        delta = (target - ref).total_seconds()
        if delta < -60:
            return f"Overdue today (was {time_part})"
        return f"Today {time_part}"
    if is_same_day(target, ref + timedelta(days=1)):
        return f"Tomorrow {time_part}"
    if is_same_day(target, ref - timedelta(days=1)):
        return f"Yesterday {time_part} · overdue"
    if target < ref:
        return f"Overdue · {target.strftime('%d %b %Y %H:%M')}"
    return f"{target.strftime('%d %b %Y %H:%M')} · {humanise_delta(target, reference=ref)}"


def format_timestamp(value: datetime | str | None, *, with_date: bool = True) -> str:
    dt = from_iso(value) if isinstance(value, str) else value
    if dt is None:
        return ""
    if with_date and not is_same_day(dt, now()):
        return dt.strftime("%d %b %Y %H:%M")
    return dt.strftime("%H:%M")


# ---------------------------------------------------------------------------
# Day/time parsing used by the reminder + task natural-language helpers
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def weekday_index(name: str) -> Optional[int]:
    return _WEEKDAYS.get(name.strip().lower().rstrip("."))


def month_index(name: str) -> Optional[int]:
    return _MONTHS.get(name.strip().lower().rstrip("."))


def next_weekday(reference: datetime, weekday: int, *, include_today: bool = True) -> datetime:
    """Next occurrence of ``weekday`` (0=Monday) after ``reference``."""
    delta = (weekday - reference.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return start_of_day(reference) + timedelta(days=delta)


def add_months(reference: datetime, months: int) -> datetime:
    month = reference.month - 1 + months
    year = reference.year + month // 12
    month = month % 12 + 1
    day = min(reference.day, _days_in_month(year, month))
    return reference.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def clamp_to_future(target: datetime, reference: datetime | None = None) -> datetime:
    ref = reference or now()
    return target if target > ref else ref + timedelta(minutes=1)
