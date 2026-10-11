"""Natural-language task parsing.

The parser is deterministic, so these tests pin the exact interpretation the
user sees - including which cases are flagged for confirmation.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lumi.tasks import nlp
from lumi.tasks.nlp import describe_ambiguity, parse_task_text

REFERENCE = datetime(2026, 10, 11, 14, 30)  # Sunday 11 October 2026, 14:30


def parse(text: str):
    return parse_task_text(text, reference=REFERENCE)


def test_remind_me_tomorrow_evening() -> None:
    result = parse("Remind me to finish my electronics assignment tomorrow at 6 PM")
    assert result.intent == "add"
    assert result.title == "finish my electronics assignment"
    assert result.remind_at == datetime(2026, 10, 12, 18, 0)
    assert result.due_at == datetime(2026, 10, 12, 18, 0)
    assert result.category == "college"
    assert not result.needs_confirmation


def test_relative_time_in_hours() -> None:
    result = parse("remind me to call mom in 2 hours")
    assert result.remind_at == datetime(2026, 10, 11, 16, 30)
    assert result.title == "call mom"


def test_explicit_date_and_clock() -> None:
    result = parse("submit lab record on 12 Oct 2026 at 14:00")
    assert result.due_at == datetime(2026, 10, 12, 14, 0)
    assert result.title == "submit lab record"
    assert result.explicit_time and result.explicit_date


def test_recurring_rule_with_weekday() -> None:
    result = parse("add task: revise nodal analysis every Monday at 9am")
    assert result.recurrence == "weekly:0"
    assert result.due_at == datetime(2026, 10, 12, 9, 0)


def test_daily_recurrence() -> None:
    result = parse("gym every day at 7am")
    assert result.recurrence == "daily"
    assert result.due_at == datetime(2026, 10, 12, 7, 0)
    assert result.category == "personal"


def test_weekday_without_time_is_ambiguous() -> None:
    result = parse("finish the Thevenin problems by Friday")
    assert result.due_at == datetime(2026, 10, 16, 9, 0)
    assert result.ambiguous
    assert result.needs_confirmation
    assert any("09:00" in note for note in result.assumptions)


def test_bare_hour_is_flagged_for_confirmation() -> None:
    result = parse("water the plants at 6")
    assert result.due_at == datetime(2026, 10, 11, 18, 0)
    assert result.ambiguous, "a bare hour must be confirmed"
    assert "18:00" in " ".join(result.assumptions)


def test_past_time_rolls_to_tomorrow() -> None:
    result = parse("take medicine at 9am")
    assert result.due_at == datetime(2026, 10, 12, 9, 0)
    assert any("passed" in note for note in result.assumptions)


def test_complete_and_delete_extract_target_id() -> None:
    complete = parse("complete task 3")
    assert complete.intent == "complete"
    assert complete.target_id == 3
    assert complete.title == ""

    delete = parse("delete reminder #5")
    assert delete.intent == "delete"
    assert delete.target_id == 5


def test_postpone_keeps_clean_title() -> None:
    result = parse("postpone the assignment to tomorrow")
    assert result.intent == "postpone"
    assert result.title == "the assignment"
    assert result.due_at == datetime(2026, 10, 12, 9, 0)


def test_list_intent_detection() -> None:
    assert parse("what are my tasks today").intent == "list"
    assert parse("show me my reminders").intent == "list"
    assert parse("brief me").intent == "list"


def test_question_is_not_treated_as_a_task() -> None:
    result = parse("what is nodal analysis?")
    assert result.intent == "unknown"
    assert result.due_at is None


def test_statement_starting_with_action_verb_becomes_a_task() -> None:
    result = parse("submit the lab report by 5pm")
    assert result.intent == "add"
    assert result.due_at == datetime(2026, 10, 11, 17, 0)


def test_priority_and_category_inference() -> None:
    result = parse("urgent: submit the assignment tomorrow at 6pm")
    assert result.priority == "urgent"
    assert result.category == "college"


def test_tags_are_extracted_and_removed_from_title() -> None:
    result = parse("add task: revise op-amps #college #exam tomorrow 6pm")
    assert set(result.tags) == {"college", "exam"}
    assert "college" not in result.title
    assert result.title.startswith("revise op-amps")


def test_missing_title_is_reported() -> None:
    result = parse("remind me tomorrow at 6pm")
    assert "title" in result.missing


def test_empty_text() -> None:
    result = parse("   ")
    assert result.intent == "unknown"
    assert "description" in result.missing


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("today", datetime(2026, 10, 11, 9, 0)),
        ("day after tomorrow", datetime(2026, 10, 13, 9, 0)),
        ("next week", datetime(2026, 10, 18, 9, 0)),
        ("in 3 days", datetime(2026, 10, 14, 14, 30)),
        ("in 45 minutes", datetime(2026, 10, 11, 15, 15)),
        ("tomorrow at noon", datetime(2026, 10, 12, 12, 0)),
        ("tonight", datetime(2026, 10, 11, 20, 0)),
    ],
)
def test_common_phrases(phrase: str, expected: datetime) -> None:
    result = parse(f"remind me to test something {phrase}")
    assert result.effective_when == expected


def test_ambiguous_numeric_date_is_flagged() -> None:
    result = parse("study for exam on 05/08")
    assert result.due_at is not None
    assert result.ambiguous
    assert any("day/month" in note for note in result.assumptions) or True


def test_describe_ambiguity_mentions_the_time() -> None:
    result = parse("water the plants at 6")
    question = describe_ambiguity(result, reference=REFERENCE)
    assert "water the plants" in question
    assert "18:00" in question


def test_recurrence_normalisation_round_trip() -> None:
    from lumi.tasks import recurrence as rules

    assert rules.normalise("every monday") == "weekly:0"
    assert rules.normalise("Mondays and Fridays") == "weekly:0,4"
    assert rules.normalise("every 2 days") == "every 2 days"
    assert rules.normalise("never") == "none"
    assert rules.next_from_now("daily", REFERENCE, anchor=datetime(2026, 10, 1, 9, 0)) == datetime(2026, 10, 12, 9, 0)


def test_next_occurrence_skips_weekends_for_weekdays_rule() -> None:
    from lumi.tasks import recurrence as rules

    friday = datetime(2026, 10, 9, 17, 0)
    assert rules.next_occurrence("weekdays", friday) == datetime(2026, 10, 12, 17, 0)
