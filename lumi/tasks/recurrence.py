"""Recurrence rules.

Supported forms (stored as a short string in the database):

* ``none``
* ``daily`` / ``every 1 day`` / ``every 3 days``
* ``weekly`` / ``every 2 weeks`` / ``weekly:mon,wed,fri``
* ``monthly`` / ``every 2 months``
* ``yearly``
* ``weekdays``   (Monday-Friday)

:func:`next_occurrence` is pure and takes the reference datetime explicitly, so
the behaviour is fully testable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from lumi.utils.timeutil import add_months, next_weekday, weekday_index

_WEEKDAY_NAMES = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    "mon|tues|tue|weds|wed|thurs|thur|thu|fri|sat|sun"
)

DEFAULT_RECURRENCE = "none"

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(minute|hour|day|week|month|year)s?$", re.IGNORECASE)
_WEEKLY_DAYS_RE = re.compile(r"^weekly:([a-z0-9,\s]+)$", re.IGNORECASE)
#: "every monday", "mondays and fridays", "every tue", "mon, wed"
_WEEKDAY_LIST_RE = re.compile(
    rf"^(?:every\s+)?(?:(?:{_WEEKDAY_NAMES})s?)(?:\s*(?:,|and|&|/)\s*(?:(?:{_WEEKDAY_NAMES})s?))*$",
    re.IGNORECASE,
)

_WEEKDAY_EXTRA_NAMES = {
    "weekday": "weekdays",
    "weekdays": "weekdays",
    "workday": "weekdays",
    "workdays": "weekdays",
    "everyday": "daily",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "yearly": "yearly",
    "annually": "yearly",
}


def normalise(rule: Optional[str]) -> str:
    """Canonicalise a user-supplied recurrence rule.

    Accepts loose human input ("every Monday", "Mondays and Fridays",
    "every 2 days", "weekly:0,2", "weekdays") and returns one of the canonical
    forms documented in the module docstring.
    """
    if rule is None:
        return DEFAULT_RECURRENCE
    rule_text = str(rule).strip()
    if not rule_text:
        return DEFAULT_RECURRENCE

    text = rule_text.lower().replace(" ", "")
    if text in {"", "none", "no", "never", "once", "one-off", "oneoff", "single"}:
        return DEFAULT_RECURRENCE

    spaced = re.sub(r"\s+", " ", rule_text.strip().lower())

    # Direct aliases ("daily", "everyday", "weekdays", ...)
    alias = _WEEKDAY_EXTRA_NAMES.get(text) or _WEEKDAY_EXTRA_NAMES.get(spaced)
    if alias:
        return alias

    every_unit = re.match(r"^every\s+(day|week|month|year|weekday|weekdays|workday|workdays)$", spaced)
    if every_unit:
        unit = every_unit.group(1)
        return {
            "day": "daily",
            "week": "weekly",
            "month": "monthly",
            "year": "yearly",
            "weekday": "weekdays",
            "weekdays": "weekdays",
            "workday": "weekdays",
            "workdays": "weekdays",
        }[unit]

    # Already canonical: "weekly:0,2"
    weekly_days = _WEEKLY_DAYS_RE.match(spaced)
    if weekly_days:
        days = _collect_weekdays(weekly_days.group(1))
        return "weekly:" + ",".join(str(day) for day in days) if days else "weekly"

    # Interval form: "every 2 days"
    interval = _INTERVAL_RE.match(spaced)
    if interval:
        count = int(interval.group(1))
        unit = interval.group(2).lower()
        if count <= 0:
            return DEFAULT_RECURRENCE
        if unit == "minute":
            minutes = max(1, count)
            return f"every {minutes} minute" + ("s" if minutes != 1 else "")
        if count == 1:
            return {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}[unit]
        return f"every {count} {unit}s"

    # Named weekday(s): "every Monday", "every mon, wed and fri", "mondays and fridays"
    every_days = _WEEKDAY_LIST_RE.match(spaced)
    if every_days:
        days = _collect_weekdays(spaced)
        if days:
            return "weekly:" + ",".join(str(day) for day in days)

    plural = re.match(rf"^({_WEEKDAY_NAMES})s$", spaced)
    if plural:
        index = weekday_index(plural.group(1))
        if index is not None:
            return f"weekly:{index}"

    return DEFAULT_RECURRENCE


def _collect_weekdays(text: str) -> list[int]:
    """Pull unique weekday indexes out of free text (names or 0-6 numbers)."""
    days: list[int] = []
    for token in re.split(r"[^a-z0-9]+", text.lower()):
        if not token:
            continue
        index: Optional[int] = None
        if token.isdigit():
            value = int(token)
            if 0 <= value <= 6:
                index = value
        else:
            index = weekday_index(token)
            if index is None and token.endswith("s"):
                index = weekday_index(token[:-1])  # "mondays" -> "monday"
        if index is not None and index not in days:
            days.append(index)
    return sorted(days)


def describe(rule: Optional[str]) -> str:
    """Human wording for the settings/task UI."""
    value = normalise(rule)
    if value == DEFAULT_RECURRENCE:
        return "Does not repeat"
    if value == "daily":
        return "Every day"
    if value == "weekdays":
        return "Every weekday (Mon-Fri)"
    if value == "weekly":
        return "Every week"
    if value.startswith("weekly:"):
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = [names[int(part)] for part in value.split(":", 1)[1].split(",") if part.isdigit() and int(part) < 7]
        return "Every " + ", ".join(days) if days else "Every week"
    if value == "monthly":
        return "Every month"
    if value == "yearly":
        return "Every year"
    interval = _INTERVAL_RE.match(value)
    if interval:
        count, unit = interval.group(1), interval.group(2).lower()
        return f"Every {count} {unit}s"
    return "Does not repeat"


def next_occurrence(rule: Optional[str], reference: datetime) -> Optional[datetime]:
    """Next datetime after ``reference`` for ``rule`` (``None`` if it never repeats)."""
    value = normalise(rule)
    if value == DEFAULT_RECURRENCE:
        return None

    if value == "daily":
        return reference + timedelta(days=1)
    if value == "weekdays":
        candidate = reference + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if value == "weekly":
        return reference + timedelta(days=7)
    if value.startswith("weekly:"):
        days = sorted(int(part) for part in value.split(":", 1)[1].split(",") if part.isdigit())
        if not days:
            return reference + timedelta(days=7)
        for offset in range(1, 8):
            candidate = reference + timedelta(days=offset)
            if candidate.weekday() in days:
                return candidate
        return reference + timedelta(days=7)
    if value == "monthly":
        return add_months(reference, 1)
    if value == "yearly":
        return add_months(reference, 12)

    interval = _INTERVAL_RE.match(value)
    if interval:
        count = int(interval.group(1))
        unit = interval.group(2).lower()
        if unit == "minute":
            return reference + timedelta(minutes=count)
        if unit == "hour":
            return reference + timedelta(hours=count)
        if unit == "day":
            return reference + timedelta(days=count)
        if unit == "week":
            return reference + timedelta(weeks=count)
        if unit == "month":
            return add_months(reference, count)
        if unit == "year":
            return add_months(reference, 12 * count)
    return None


def next_from_now(rule: Optional[str], reference: datetime, *, anchor: datetime | None = None) -> Optional[datetime]:
    """Next occurrence that is strictly in the future relative to ``reference``.

    Used when a reminder fires: repeatedly advance the anchor until it is after
    ``reference`` so a laptop that was asleep for three days does not queue up
    dozens of missed notifications.
    """
    current = anchor or reference
    value = normalise(rule)
    if value == DEFAULT_RECURRENCE:
        return None
    for _ in range(1000):
        nxt = next_occurrence(value, current)
        if nxt is None:
            return None
        current = nxt
        if current > reference:
            return current
    return None


def default_rule_for_weekday(reference: datetime) -> str:
    """Weekly rule anchored on the reference day (used by 'remeber every Tuesday')."""
    return f"weekly:{reference.weekday()}"


def aligns_with_weekday(rule: Optional[str], reference: datetime) -> datetime:
    """Snap ``reference`` forward to the next day matching a weekly rule."""
    value = normalise(rule)
    if not value.startswith("weekly:"):
        return reference
    days = sorted(int(part) for part in value.split(":", 1)[1].split(",") if part.isdigit())
    if not days or reference.weekday() in days:
        return reference
    for offset in range(1, 8):
        candidate = reference + timedelta(days=offset)
        if candidate.weekday() in days:
            return candidate
    return reference


def weekday_anchor(reference: datetime) -> datetime:
    return next_weekday(reference, reference.weekday(), include_today=True)
