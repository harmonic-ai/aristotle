"""Tests for aristotlelib.event module."""

from datetime import datetime

import pytest

from aristotlelib.event import Event, EventStatus, EventType


def test_event_str_with_explanation():
    """Test event string representation with explanation."""
    event = Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=EventType.THINKING,
        created_at=datetime.now(),
        content="Raw content here",
        file_path=None,
        explanation="Human-readable explanation",
        status=EventStatus.COMPLETE,
    )

    result = str(event)
    assert "THINKING" in result
    assert "Human-readable explanation" in result
    assert "Raw content here" not in result  # Should prefer explanation


def test_event_str_without_explanation():
    """Test event string representation without explanation."""
    event = Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=EventType.EDITING_FILE,
        created_at=datetime.now(),
        content="Editing file.lean",
        file_path="src/file.lean",
        explanation=None,
        status=EventStatus.COMPLETE,
    )

    result = str(event)
    assert "EDITING_FILE" in result
    assert "src/file.lean" in result


def _without_text(
    event_type: EventType, explanation: str | None = None, file_path: str | None = None, content: str = ""
) -> Event:
    return Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=event_type,
        created_at=datetime.now(),
        content=content,
        file_path=file_path,
        explanation=explanation,
        status=EventStatus.COMPLETE,
    )


@pytest.mark.parametrize(
    ("explanation", "file_path", "content"),
    [(None, None, ""), ("", "", "  "), ("\n", None, "")],
    ids=["empty", "blank", "a bare newline"],
)
def test_an_event_with_no_text_says_what_aristotle_was_doing(explanation, file_path, content):
    assert str(_without_text(EventType.RUNNING_COMMAND, explanation, file_path, content)) == "Running command"


@pytest.mark.parametrize(
    ("event_type", "activity"),
    [
        (EventType.BUILDING, "Building"),
        (EventType.SEARCHING_LOCAL, "Searching project"),
        (EventType.READING_LEAN, "Reading Lean"),
        (EventType.RUNNING_LEAN, "Running Lean"),
    ],
    ids=lambda value: getattr(value, "name", value),
)
def test_each_activity_reads_as_the_web_app_labels_it(event_type, activity):
    assert str(_without_text(event_type)) == activity


def test_an_event_of_a_type_this_version_does_not_know_says_aristotle_is_working():
    assert str(_without_text(EventType.UNKNOWN)) == "Working"


@pytest.mark.parametrize(
    "event_type",
    [t for t in EventType if t not in (EventType.QUESTION, EventType.AGENT_QUESTION)],
    ids=lambda t: t.name,
)
def test_no_event_without_text_prints_its_type_name(event_type):
    assert str(_without_text(event_type)) not in {event_type.name, ""}


def test_a_blank_explanation_gives_way_to_the_file_path():
    event = Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=EventType.EDITING_FILE,
        created_at=datetime.now(),
        content="",
        file_path="src/file.lean",
        explanation=" ",
        status=EventStatus.COMPLETE,
    )

    assert str(event) == "EDITING_FILE: src/file.lean"


def test_event_type_unknown_handling():
    """Test that unknown event type values are handled gracefully."""
    event = Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=999,  # Unknown type
        created_at=datetime.now(),
        content="Some content",
        file_path=None,
        explanation=None,
        status=EventStatus.COMPLETE,
    )

    assert event.event_type == EventType.UNKNOWN


def test_event_status_unknown_handling():
    """Test that unknown event status values are handled gracefully."""
    event = Event(
        event_id="event-123",
        agent_task_id="task-456",
        event_type=EventType.THINKING,
        created_at=datetime.now(),
        content="Some content",
        file_path=None,
        explanation=None,
        status=999,  # Unknown status
    )

    assert event.status == EventStatus.UNKNOWN


def test_event_object_id():
    """Test that object_id property returns event_id."""
    event = Event(
        event_id="event-abc-123",
        agent_task_id="task-456",
        event_type=EventType.PROVING,
        created_at=datetime.now(),
        content="Proving theorem",
        file_path=None,
        explanation=None,
        status=EventStatus.COMPLETE,
    )

    assert event.object_id == "event-abc-123"
