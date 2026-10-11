"""Natural-language understanding for tasks and reminders.

This module is deliberately **deterministic**: ordered regular expressions plus a
small amount of date arithmetic, not an AI model.  That means

* it works with no model installed and no internet connection,
* it is fast (microseconds, not seconds),
* it is fully testable, and
* the assistant can tell the user exactly *what* it understood.

Whenever a value is inferred rather than stated ("at 6" could be 06:00 or 18:00,
"on Friday" has no time) the result is marked *ambiguous* so the chat layer asks
for confirmation before anything is saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from lumi.tasks import recurrence as recurrence_rules
from lumi.utils.timeutil import (
    add_months,
    month_index,
    next_weekday,
    now as current_time,
    start_of_day,
    to_iso,
    weekday_index,
)

MAX_TITLE_LENGTH = 300
DEFAULT_DATE_TIME = (9, 0)  # when a date but no clock time is given

ADD_PREFIXES: tuple[str, ...] = (
    "please remind me to ",
    "remind me that i need to ",
    "remind me to ",
    "remind me ",
    "remember me to ",
    "remember to ",
    "don't let me forget to ",
    "dont let me forget to ",
    "add a reminder to ",
    "add a task to ",
    "add task to ",
    "add a task: ",
    "add task: ",
    "add a todo: ",
    "add todo: ",
    "create a task to ",
    "create a task: ",
    "create task ",
    "new task: ",
    "note down: ",
    "make a note to ",
    "make sure to ",
    "i need to ",
)

LIST_PATTERNS: tuple[str, ...] = (
    r"\b(what|which)\b[^\n]{0,40}\b(tasks?|todos?|to-dos?|deadlines?)\b",
    r"\b(list|show|display)\b[^\n]{0,30}\b(tasks?|todos?|to-dos?|reminders?|deadlines?)\b",
    r"\bmy (tasks?|todos?|reminders?|deadlines?)\b",
    r"\bwhat('s| is) (due|pending|overdue)\b",
    r"\bwhat do i have (today|tomorrow|this week)\b",
    r"\bbrief me\b",
)

WEEKDAY_NAMES = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    "mon|tues|tue|weds|wed|thurs|thur|thu|fri|satur|sat|sunday|sun"
)
MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)

PART_OF_DAY_HOURS = {"morning": 9, "afternoon": 14, "evening": 19, "tonight": 20, "night": 21}
NAMED_CLOCK = {"noon": 12, "midday": 12, "midnight": 0}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "college": (
        "assignment", "homework", "home work", "exam", "test", "quiz", "viva", "lecture", "class",
        "semester", "lab record", "lab", "record", "submission", "submit", "professor", "faculty",
        "college", "tutorial", "revision", "revise", "study", "seminar", "attendance", "fee",
    ),
    "coding": ("code", "coding", "bug", "debug", "commit", "pull request", "refactor", "unit test", "compile"),
    "projects": ("project", "prototype", "pcb", "circuit", "hardware", "design review", "simulation"),
    "personal": ("gym", "workout", "call ", "birthday", "grocery", "groceries", "laundry", "medicine", "bank"),
}

PRIORITY_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(urgent|asap|immediately|critical|emergency|right now)\b", "urgent"),
    (r"\b(important|high priority|must do|top priority)\b", "high"),
    (r"\b(low priority|whenever|someday|if possible|no rush)\b", "low"),
)

_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_\-]{2,30})")


@dataclass
class ParseResult:
    """Outcome of parsing one user sentence."""

    text: str = ""
    intent: str = "unknown"
    title: str = ""
    due_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    recurrence: str = "none"
    assumptions: list[str] = field(default_factory=list)
    ambiguous: bool = False
    explicit_time: bool = False
    explicit_date: bool = False
    missing: list[str] = field(default_factory=list)
    target_id: Optional[int] = None

    @property
    def needs_confirmation(self) -> bool:
        """True when LUMI should check the interpretation before saving."""
        return self.ambiguous and self.effective_when is not None

    @property
    def effective_when(self) -> Optional[datetime]:
        return self.remind_at or self.due_at

    def recurrence_present(self) -> bool:
        return self.recurrence not in ("", "none")

    def describe(self, *, reference: datetime | None = None) -> str:
        from lumi.utils.timeutil import humanise_due

        ref = reference or current_time()
        parts: list[str] = []
        when = self.effective_when
        if when:
            label = "Reminder" if self.remind_at else "Due"
            parts.append(f"{label}: {humanise_due(when, reference=ref)}")
        if self.priority:
            parts.append(f"Priority: {self.priority}")
        if self.category:
            parts.append(f"Category: {self.category}")
        if self.recurrence_present():
            parts.append(recurrence_rules.describe(self.recurrence))
        if self.tags:
            parts.append("Tags: " + ", ".join(self.tags))
        return " · ".join(parts) if parts else "No date or time"

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "title": self.title,
            "due_at": to_iso(self.due_at),
            "remind_at": to_iso(self.remind_at),
            "priority": self.priority,
            "category": self.category,
            "tags": list(self.tags),
            "recurrence": self.recurrence,
            "assumptions": list(self.assumptions),
            "ambiguous": self.ambiguous,
            "needs_confirmation": self.needs_confirmation,
            "missing": list(self.missing),
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _clean_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .,;:-")
    cleaned = re.sub(r"^(to|that|:)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(by|before|due|on|at|in|from|to|until|till|for|about)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .,;:- ")[:MAX_TITLE_LENGTH]


def _remove_span(text: str, start: int, end: int) -> str:
    return (text[:start] + " " + text[end:]).strip()


def _combine(day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _safe_date(year: int, month: int, day: int, reference: datetime) -> Optional[datetime]:
    try:
        candidate = datetime(year, month, day)
    except ValueError:
        return None
    if candidate.date() < reference.date():
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:  # 29 February
            return candidate
    return candidate


def classify_intent(text: str) -> str:
    lowered = (text or "").lower().strip()
    if not lowered:
        return "unknown"
    if any(re.search(pattern, lowered) for pattern in LIST_PATTERNS):
        return "list"
    if re.search(r"\b(delete|remove|cancel)\b[^\n]{0,20}\b(task|reminder|todo|#\d+|\d+)\b", lowered):
        return "delete"
    if re.search(r"\b(postpone|reschedul\w*|push back|delay|move)\b", lowered):
        return "postpone"
    if re.search(r"\b(complete|completed|mark|finish(ed)?|done with)\b[^\n]{0,20}\b(task|#\d+|\d+)\b", lowered):
        return "complete"
    if lowered.startswith(("complete ", "mark ")) or re.match(r"^(i )?(finished|completed|did) ", lowered):
        return "complete"
    if any(lowered.startswith(prefix) for prefix in ADD_PREFIXES):
        return "add"
    if re.search(
        r"\b(remind me|add (a )?(task|todo|reminder)|create (a )?(task|reminder|reminder for)|new task|note down|don't let me forget)\b",
        lowered,
    ):
        return "add"
    if _looks_like_task_statement(lowered):
        return "add"
    return "unknown"


#: Verbs that, combined with an explicit date/time, strongly suggest "save this
#: as a task".  Kept deliberately short - guessing wrong here is annoying.
ACTION_VERBS = (
    "submit", "finish", "complete", "pay", "call", "send", "write", "study", "revise",
    "buy", "book", "attend", "prepare", "practice", "practise", "read", "water", "take",
    "upload", "email", "print", "renew", "apply", "visit", "collect", "pack", "clean",
    "register", "check", "review", "solve", "debug", "deploy", "post", "ask", "meet",
    "gym", "exercise", "workout", "run", "walk", "sleep", "wake", "shower", "cook",
)


def _looks_like_task_statement(lowered: str) -> bool:
    """Heuristic: the sentence *starts* with an action verb and names a date/time."""
    if not re.match(rf"^({'|'.join(ACTION_VERBS)})\b", lowered):
        return False
    return bool(
        re.search(
            r"\b(today|tomorrow|tonight|morning|afternoon|evening|night|noon|midnight|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"next week|next month|in \d+|at \d|\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)|every |"
            r"\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
            lowered,
        )
    )


def _strip_add_prefix(text: str) -> str:
    lowered = text.lower()
    for prefix in sorted(ADD_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    for prefix in ADD_PREFIXES:
        index = lowered.find(prefix)
        if 0 < index <= 16:
            return text[index + len(prefix):].strip()
    return text.strip()


def _strip_command_words(text: str, words: tuple[str, ...]) -> str:
    pattern = r"^\s*(?:" + "|".join(re.escape(word) for word in words) + r")\s+"
    return re.sub(pattern, "", text, flags=re.IGNORECASE)


def _extract_target_id(text: str) -> Optional[int]:
    match = re.search(r"#(\d{1,8})\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(?:task|reminder)\s+(?:number\s+|id\s+|#)?(\d{1,8})\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def infer_category(title: str) -> Optional[str]:
    lowered = f" {title.lower()} "
    best: Optional[str] = None
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword.endswith(" "):
                if keyword in lowered:
                    return category
            elif f" {keyword}" in lowered:
                best = best or category
    return best


def infer_priority(text: str) -> Optional[str]:
    lowered = text.lower()
    for pattern, priority in PRIORITY_HINTS:
        if re.search(pattern, lowered):
            return priority
    return None


def extract_tags(text: str) -> tuple[list[str], str]:
    """Pull ``#tags`` out of ``text``, preserving string length so offsets stay valid."""
    tags: list[str] = []

    def collect(match: re.Match[str]) -> str:
        tags.append(match.group(1).lower())
        return " " * (match.end() - match.start())

    return tags, _TAG_RE.sub(collect, text)


# ---------------------------------------------------------------------------
# time scanning
# ---------------------------------------------------------------------------


@dataclass
class _TimeHit:
    start: int
    end: int
    value: Optional[datetime] = None
    note: str = ""
    ambiguous: bool = False
    kind: str = "time"          # time | date | datetime
    recurrence: str = "none"    # set when kind == "recurrence"


def _scan_clock_times(text: str, reference: datetime) -> list[_TimeHit]:
    """Clock times: ``18:30``, ``6.30 pm``, ``6pm``, ``at 6``, ``at noon``, ``tonight``."""
    hits: list[_TimeHit] = []

    # 1) HH:MM with optional meridiem, optionally preceded by "at "
    for match in re.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?!\d)", text, re.IGNORECASE):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        meridiem = (match.group(3) or "").lower().replace(".", "")
        start, end = match.start(), match.end()
        if text[max(0, start - 3):start].lower().endswith("at "):
            start -= 3
        note = ""
        ambiguous = False
        if meridiem.startswith("p") and hour < 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
        elif not meridiem and 1 <= hour <= 11:
            # 8:30 with no am/pm: assume morning; flag it so the user can correct.
            note = f"I read {match.group(1)}:{match.group(2)} as {hour:02d}:{minute:02d}"
            ambiguous = True
        hits.append(
            _TimeHit(start, end, _combine(reference, hour, minute), note=note, ambiguous=ambiguous, kind="time")
        )

    # 2) "6 pm", "6pm", "6 p.m."
    for match in re.finditer(r"(?<![\d:])(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)(?!\w)", text, re.IGNORECASE):
        if any(hit.start <= match.start() < hit.end for hit in hits):
            continue
        hour = int(match.group(1))
        meridiem = match.group(2).lower().replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23:
            continue
        start, end = match.start(), match.end()
        if text[max(0, start - 3):start].lower().endswith("at "):
            start -= 3
        hits.append(_TimeHit(start, end, _combine(reference, hour, 0), kind="time"))

    # 3) "at 6" / "at 6 o'clock" - genuinely ambiguous on a 12-hour clock
    for match in re.finditer(r"\bat\s+(\d{1,2})(?![\d:])(?:\s*o'?\s*clock)?", text, re.IGNORECASE):
        if any(hit.start <= match.start() < hit.end for hit in hits):
            continue
        hour = int(match.group(1))
        if hour > 23:
            continue
        assumed_pm = 1 <= hour <= 7
        resolved = hour + 12 if assumed_pm else hour
        note = (
            f"you said {hour} - I took that as {resolved:02d}:00 "
            "(give an exact time like 6 pm if that is wrong)"
        )
        hits.append(
            _TimeHit(match.start(), match.end(), _combine(reference, resolved, 0), note=note, ambiguous=True, kind="time")
        )

    # 4) named clock points and parts of the day
    for match in re.finditer(r"\b(noon|midday|midnight)\b", text, re.IGNORECASE):
        hour = NAMED_CLOCK[match.group(1).lower()]
        hits.append(_TimeHit(match.start(), match.end(), _combine(reference, hour, 0), kind="time"))

    for match in re.finditer(
        r"\b(?:(this|in the|tomorrow|by|at)\s+)?(morning|afternoon|evening|night|tonight)\b", text, re.IGNORECASE
    ):
        word = match.group(2).lower()
        if word == "tonight":
            pass
        elif not match.group(1):
            # A bare "morning"/"evening" is usually part of the title ("morning walk").
            continue
        hour = PART_OF_DAY_HOURS[word]
        hits.append(
            _TimeHit(
                match.start(),
                match.end(),
                _combine(reference, hour, 0),
                note=f"I used {hour:02d}:00 for the {word}",
                ambiguous=False,
                kind="time",
            )
        )
    return hits


def _scan_dates(text: str, reference: datetime) -> list[_TimeHit]:
    """Dates: today/tomorrow, weekdays, ``12 Oct``, ``Oct 12``, ``12/10``, ``in 3 days``."""
    hits: list[_TimeHit] = []

    for match in re.finditer(
        r"\b(today|tonight|tomorrow|tmrw|day after tomorrow|yesterday|last night|"
        r"next week|next month|next year)\b",
        text,
        re.IGNORECASE,
    ):
        word = match.group(1).lower()
        base = start_of_day(reference)
        if word in {"today", "tonight"}:
            hits.append(_TimeHit(match.start(), match.end(), base, kind="date"))
        elif word == "yesterday":
            # Legitimate for logging something that already slipped: a task due
            # yesterday is overdue, not invalid.
            hits.append(_TimeHit(match.start(), match.end(), base - timedelta(days=1), kind="date"))
        elif word == "last night":
            hits.append(_TimeHit(match.start(), match.end(), base - timedelta(days=1) + timedelta(hours=21), kind="datetime"))
        elif word in {"tomorrow", "tmrw"}:
            hits.append(_TimeHit(match.start(), match.end(), base + timedelta(days=1), kind="date"))
        elif word == "day after tomorrow":
            hits.append(_TimeHit(match.start(), match.end(), base + timedelta(days=2), kind="date"))
        elif word == "next week":
            hits.append(
                _TimeHit(
                    match.start(), match.end(), base + timedelta(days=7),
                    note="'next week' is vague - I used the same weekday next week",
                    ambiguous=True, kind="date",
                )
            )
        elif word == "next month":
            hits.append(
                _TimeHit(match.start(), match.end(), add_months(base, 1), note="'next month' - I used the same day next month", ambiguous=True, kind="date")
            )
        elif word == "next year":
            hits.append(_TimeHit(match.start(), match.end(), add_months(base, 12), note="'next year' - same day next year", ambiguous=True, kind="date"))

    for match in re.finditer(r"\bin\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b", text, re.IGNORECASE):
        count = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("min"):
            value = reference + timedelta(minutes=count)
        elif unit.startswith(("hour", "hr")):
            value = reference + timedelta(hours=count)
        elif unit.startswith("day"):
            value = reference + timedelta(days=count)
        elif unit.startswith("week"):
            value = reference + timedelta(weeks=count)
        else:
            value = add_months(reference, count)
        hits.append(_TimeHit(match.start(), match.end(), value, kind="datetime"))

    for match in re.finditer(rf"\b(?:(?:on|by|next|this|due)\s+)?({WEEKDAY_NAMES})\b", text, re.IGNORECASE):
        if any(hit.start <= match.start() < hit.end for hit in hits):
            continue
        name = match.group(1)
        index = weekday_index(name)
        if index is None:
            continue
        phrase = match.group(0).lower()
        include_today = not phrase.strip().startswith("next")
        day = next_weekday(reference, index, include_today=include_today)
        if day < start_of_day(reference):
            day += timedelta(days=7)
        hits.append(
            _TimeHit(match.start(), match.end(), day, note="", ambiguous=False, kind="date")
        )

    for match in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})(?:\s+(\d{{4}}))?\b", text, re.IGNORECASE):
        day = int(match.group(1))
        month = month_index(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        value = _safe_date(year, month or 1, day, reference) if month else None
        if value:
            hits.append(_TimeHit(match.start(), match.end(), value, kind="date"))

    for match in re.finditer(rf"\b({MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(\d{{4}}))?\b", text, re.IGNORECASE):
        month = month_index(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        value = _safe_date(year, month or 1, day, reference) if month else None
        if value:
            hits.append(_TimeHit(match.start(), match.end(), value, kind="date"))

    for match in re.finditer(r"(?<![\d/])(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?(?![\d/])", text):
        first, second = int(match.group(1)), int(match.group(2))
        if first > 31 or second > 31 or first == 0 or second == 0:
            continue
        year = reference.year
        if match.group(3):
            year = int(match.group(3))
            if year < 100:
                year += 2000
        # Day/month ordering (the convention used in India and most of Europe).
        ambiguous = False
        if first > 12:
            day, month = first, second          # unambiguously day/month
        elif second > 12:
            day, month = second, first          # month/day written the other way
        else:
            ambiguous = True                    # e.g. 05/08 - could be either
            candidates = [
                candidate
                for candidate in (_safe_date(year, first, second, reference), _safe_date(year, second, first, reference))
                if candidate is not None
            ]
            if candidates:
                best = min(candidates, key=lambda candidate: (candidate < reference, candidate))
            else:
                best = None
            if best is None:
                continue
            day, month = best.day, best.month
            hits.append(
                _TimeHit(
                    match.start(), match.end(), best,
                    note=f"I read {match.group(0)} as {best.strftime('%d %b %Y')} (day/month) - correct me if it is month/day",
                    ambiguous=True, kind="date",
                )
            )
            continue
        value = _safe_date(year, month, day, reference)
        if value:
            hits.append(
                _TimeHit(
                    match.start(),
                    match.end(),
                    value,
                    note="I read that date as day/month" if ambiguous else "",
                    ambiguous=ambiguous,
                    kind="date",
                )
            )
    return hits


def _scan_recurrence(text: str, reference: datetime) -> list[_TimeHit]:
    hits: list[_TimeHit] = []
    patterns = (
        r"\bevery\s+(\d{1,2})\s+(minutes?|hours?|days?|weeks?|months?|years?)\b",
        rf"\bevery\s+({WEEKDAY_NAMES})s?\b",
        r"\bevery\s+(day|week|month|year|weekdays?)\b",
        r"\b(daily|weekly|monthly|yearly|annually)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(0)
            rule = recurrence_rules.normalise(raw)
            if rule == "none":
                continue
            value = _first_occurrence(rule, reference)
            hits.append(
                _TimeHit(match.start(), match.end(), value, kind="recurrence", recurrence=rule)
            )
    return hits


def _first_occurrence(rule: str, reference: datetime) -> Optional[datetime]:
    """First date for a rule that has no explicit date attached."""
    if rule.startswith("weekly:"):
        days = sorted(int(part) for part in rule.split(":", 1)[1].split(",") if part.isdigit())
        for offset in range(1, 8):
            candidate = reference + timedelta(days=offset)
            if candidate.weekday() in days:
                return start_of_day(candidate)
    if rule in {"daily", "weekdays"}:
        return start_of_day(reference + timedelta(days=1))
    if rule == "weekly":
        return start_of_day(reference + timedelta(days=7))
    if rule == "monthly":
        return start_of_day(add_months(reference, 1))
    if rule == "yearly":
        return start_of_day(add_months(reference, 12))
    match = re.match(r"^every\s+(\d+)\s+(minute|hour|day|week|month|year)s$", rule)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        delta = {
            "minute": timedelta(minutes=count),
            "hour": timedelta(hours=count),
            "day": timedelta(days=count),
            "week": timedelta(weeks=count),
        }.get(unit)
        if delta is not None:
            return reference + delta
        return add_months(reference, count if unit == "month" else 12 * count)
    return start_of_day(reference + timedelta(days=1))


# ---------------------------------------------------------------------------
# merging hits into a single interpretation
# ---------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


@dataclass
class _Interpretation:
    when: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)
    ambiguous: bool = False
    explicit_time: bool = False
    explicit_date: bool = False
    recurrence: str = "none"
    spans: list[tuple[int, int]] = field(default_factory=list)


def _interpret(hits: list[_TimeHit], reference: datetime) -> _Interpretation:
    if not hits:
        return _Interpretation()

    # Keep non-overlapping hits, preferring the one that starts earliest and the longest.
    ordered = sorted(hits, key=lambda hit: (hit.start, -(hit.end - hit.start)))
    kept: list[_TimeHit] = []
    for hit in ordered:
        if any(existing.start <= hit.start < existing.end for existing in kept):
            continue
        kept.append(hit)

    result = _Interpretation(spans=[(hit.start, hit.end) for hit in kept])

    time_hits = [hit for hit in kept if hit.kind == "time"]
    date_hits = [hit for hit in kept if hit.kind in {"date", "datetime"}]
    recurrence_hits = [hit for hit in kept if hit.kind == "recurrence"]

    if recurrence_hits:
        result.recurrence = recurrence_hits[0].recurrence
        if recurrence_hits[0].value and not date_hits:
            date_hits = [recurrence_hits[0]]

    def absorb(hit: _TimeHit) -> None:
        if hit.note:
            result.notes.append(hit.note)
        if hit.ambiguous:
            result.ambiguous = True

    date_hit = next((hit for hit in date_hits if hit.value is not None), None)
    time_hit = next((hit for hit in time_hits if hit.value is not None), None)

    if date_hit and time_hit:
        absorb(date_hit)
        absorb(time_hit)
        result.when = _combine(date_hit.value, time_hit.value.hour, time_hit.value.minute)  # type: ignore[arg-type]
    elif date_hit and date_hit.kind == "datetime":
        # "in 2 hours" already contains a clock time - use it verbatim.
        absorb(date_hit)
        result.when = date_hit.value
    elif date_hit:
        absorb(date_hit)
        result.when = _combine(date_hit.value, *DEFAULT_DATE_TIME)  # type: ignore[arg-type]
        result.notes.append(f"no time was given, so I used {DEFAULT_DATE_TIME[0]:02d}:00")
        result.ambiguous = True
    elif time_hit:
        absorb(time_hit)
        candidate = time_hit.value  # type: ignore[assignment]
        assert candidate is not None
        if candidate <= reference:
            candidate = candidate + timedelta(days=1)
            result.notes.append("that time has already passed today, so I used tomorrow")
            result.ambiguous = True
        result.when = candidate

    result.explicit_time = bool(time_hits)
    result.explicit_date = bool(date_hits) or bool(recurrence_hits)

    if result.recurrence != "none" and result.when is not None:
        aligned = recurrence_rules.aligns_with_weekday(result.recurrence, result.when)
        if aligned != result.when:
            result.when = aligned
            result.notes.append(f"aligned the first occurrence to {aligned.strftime('%A %d %b %H:%M')}")
    if result.recurrence != "none" and result.when is None:
        first = _first_occurrence(result.recurrence, reference)
        if first is not None:
            result.when = _combine(first, *DEFAULT_DATE_TIME)
            result.notes.append("repeating entry - I set the first occurrence for 09:00")

    result.notes = _dedupe(result.notes)
    return result


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def parse_task_text(text: str, *, reference: datetime | None = None, is_reminder: bool | None = None) -> ParseResult:
    """Parse a sentence that adds, completes, deletes or postpones a task."""
    ref = reference or current_time()
    original = (text or "").strip()
    result = ParseResult(text=original, intent=classify_intent(original))

    if not original:
        result.missing.append("description")
        return result
    if result.intent == "list":
        return result

    result.target_id = _extract_target_id(original)

    if result.intent == "add":
        body = _strip_add_prefix(original)
    elif result.intent in {"complete", "delete"}:
        body = _strip_command_words(original, ("complete", "completed", "finish", "finished", "mark", "delete", "remove", "cancel"))
        body = re.sub(r"\b(mark(ed)?|as|done|task|reminder|#\d+)\b", " ", body, flags=re.IGNORECASE)
    elif result.intent == "postpone":
        body = _strip_command_words(original, ("postpone", "reschedule", "push", "delay", "move", "shift"))
    else:
        body = original

    if result.intent in {"complete", "delete", "postpone"} and result.target_id is not None:
        # Drop the id so it does not leak into the title ("complete task 3").
        body = re.sub(r"#\d{1,8}\b", " ", body)
        body = re.sub(r"\b(task|reminder|todo)\s*(?:number\s+|id\s+|#)?\d{1,8}\b", " ", body, flags=re.IGNORECASE)
        body = re.sub(r"^\s*\d{1,8}\s*$", " ", body)

    tags, body = extract_tags(body)
    result.tags = tags

    hits = _scan_clock_times(body, ref) + _scan_dates(body, ref) + _scan_recurrence(body, ref)
    interpretation = _interpret(hits, ref)

    title_body = body
    for start, end in sorted(interpretation.spans, reverse=True):
        title_body = _remove_span(title_body, start, end)
    title = _clean_title(title_body)

    if not title and result.target_id is None:
        if interpretation.when is not None:
            # The whole sentence was a time phrase ("remind me tomorrow at 6").
            result.missing.append("title")
            title = _clean_title(
                re.sub(rf"\b({MONTH_NAMES}|at|on|by|in|every|tomorrow|today|tonight)\b", " ", original, flags=re.IGNORECASE)
            )
    result.title = title

    result.recurrence = interpretation.recurrence
    result.assumptions = list(interpretation.notes)
    result.ambiguous = interpretation.ambiguous
    result.explicit_time = interpretation.explicit_time
    result.explicit_date = interpretation.explicit_date

    when = interpretation.when
    if when is not None:
        if is_reminder is None:
            is_reminder = bool(re.search(r"\b(remind|reminder|alert|notify|wake me|don't let me forget)\b", original, re.IGNORECASE))
        if is_reminder:
            result.remind_at = when
            result.due_at = when
        else:
            # Tasks: a past date stays in the past so it shows up as overdue.
            result.due_at = when

    priority = infer_priority(original)
    if priority:
        result.priority = priority
    result.category = infer_category(result.title)

    if result.intent == "add" and result.effective_when is None and not result.recurrence_present():
        result.assumptions.append("no date or time was mentioned, so this task will have no deadline")

    return result


def describe_ambiguity(result: ParseResult, *, reference: datetime | None = None) -> str:
    """One-line confirmation question used by the chat layer."""
    when = result.effective_when
    if when is None:
        return f'Should I add "{result.title}" with no deadline?'
    return f'Just to confirm — "{result.title}" ({result.describe(reference=reference)}). Should I save it?'


def parse_reminder_target(text: str) -> Optional[int]:
    return _extract_target_id(text)
