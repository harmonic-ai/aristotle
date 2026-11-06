"""Tests for aristotlelib.event module."""

import re
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.event import Event, EventStatus, EventType
from aristotlelib.question import QuestionClosed
from tests.conftest import patched_client


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


def _placeholder(**overrides) -> dict:
    """A question's event as the server writes it: complete from the start, its question kept in the listing."""
    return {
        "event_id": "event-1",
        "agent_task_id": "task-456",
        "event_type": EventType.AGENT_QUESTION.value,
        "created_at": "2024-01-01T00:00:00",
        "content": "Asked a question",
        "file_path": None,
        "explanation": None,
        "suggestions": None,
        "status": EventStatus.COMPLETE.value,
        "question_id": "q-1",
        **overrides,
    }


def _task(status: str = "IN_PROGRESS", read_only: bool = False) -> dict:
    return {
        "project_id": "project-123",
        "agent_task_id": "task-456",
        "status": status,
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T00:00:00",
        "file_name": None,
        "description": None,
        "output_summary": None,
        "read_only": read_only,
    }


def _question(**overrides) -> dict:
    return {
        "question_id": "q-1",
        "event_id": "event-1",
        "project_id": "project-123",
        "agent_run_id": "task-456",
        "content": "Should the count saturate or wrap?",
        "suggestions": ["Saturate", "Wrap"],
        "response": None,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "blocking_since": "2024-01-01T00:00:00",
        "ever_awaited": True,
        "answered_at": None,
        "ignored_at": None,
        **overrides,
    }


def _server(event: dict, task: dict, questions: list[dict] | Exception) -> AsyncMock:
    """A client that serves ``event``, its ``task`` and the task's ``questions``, or raises them, and takes any answer
    to the listed question."""
    client = AsyncMock()

    def get(path):
        if path.endswith("/questions") and isinstance(questions, Exception):
            raise questions
        response = Mock()
        response.json.return_value = {
            "/event/event-1": event,
            "/task/task-456": task,
            "/task/task-456/questions": questions,
        }[path]
        return response

    def post(path, json):
        response = Mock()
        response.json.return_value = _question(response=json["answer"], answered_at="2024-01-01T00:05:00")
        return response

    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def test_answer_answers_the_waiting_question_the_listing_has_for_the_event():
    event = Event.model_validate(_placeholder())
    client = _server(_placeholder(), _task(), [_question(question_id="q-0", event_id="event-0"), _question()])

    with patched_client(client):
        await event.answer("Wrap")

    client.post.assert_awaited_once_with("/question/q-1/answer", json={"answer": "Wrap"})
    assert str(event) == "Aristotle asked: Should the count saturate or wrap?\nYou answered: Wrap"


@pytest.mark.parametrize(
    ("task", "question", "refusal", "says"),
    [
        (
            _task(),
            _question(response="Saturate", answered_at="2024-01-01T00:02:00"),
            "This question has already been answered",
            "You answered: Saturate",
        ),
        (
            _task("COMPLETE", read_only=True),
            _question(response="Saturate", answered_at="2024-01-01T00:02:00"),
            "This question has already been answered",
            "The project's owner answered: Saturate",
        ),
        (
            _task(),
            _question(ignored_at="2024-01-01T00:15:00"),
            "This question was closed without an answer",
            "The question was closed without an answer.",
        ),
        (
            _task("COMPLETE"),
            _question(),
            "The run has ended, so no answer can reach Aristotle now",
            "The run has ended, so no answer can reach Aristotle now.",
        ),
        (
            _task(read_only=True),
            _question(),
            "Only the project's owner can answer this question",
            "Waiting for an answer; only the project's owner can answer.",
        ),
    ],
    ids=["answered", "answered on a shared project", "closed", "its run ended", "waiting on a shared project"],
)
async def test_answer_refuses_a_question_that_no_longer_waits_and_says_why(task, question, refusal, says):
    event = Event.model_validate(_placeholder())
    client = _server(_placeholder(), task, [question])

    with (
        patched_client(client),
        pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"),
    ):
        await event.answer("Wrap")

    client.post.assert_not_awaited()
    assert str(event) == f"Aristotle asked: Should the count saturate or wrap?\n{says}"


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [_question(event_id="event-2")],
        AristotleAPIError("not found", status_code=404),
        AristotleAPIError("method not allowed", status_code=405),
    ],
    ids=["no question", "only another event's question", "no listing (404)", "no listing (405)"],
)
async def test_answer_still_says_a_question_no_listed_question_claims_timed_out(questions):
    event = Event.model_validate(_placeholder(content="Which width?"))
    client = _server(_placeholder(content="Which width?"), _task(), questions)

    with (
        patched_client(client),
        pytest.raises(ValueError, match="^This question timed out. The agent did what it thought was best.$"),
    ):
        await event.answer("8")

    client.post.assert_not_awaited()
    assert "timed out" in str(event)


async def test_answer_sends_nothing_when_the_questions_listing_fails():
    event = Event.model_validate(_placeholder())
    client = _server(_placeholder(), _task(), AristotleAPIError("Server error", status_code=500))

    with (
        patched_client(client),
        pytest.raises(AristotleAPIError, match="Server error"),
    ):
        await event.answer("Wrap")

    client.post.assert_not_awaited()


async def test_answer_raises_question_closed_when_the_server_finds_the_question_closed():
    event = Event.model_validate(_placeholder())
    client = _server(_placeholder(), _task(), [_question()])
    client.post = AsyncMock(side_effect=AristotleAPIError("Question has already been answered", status_code=409))

    with (
        patched_client(client),
        pytest.raises(QuestionClosed),
    ):
        await event.answer("Wrap")


async def test_answer_answers_an_older_servers_sent_question_on_its_event():
    sent = _placeholder(content="Which width?", status=EventStatus.SENT.value, suggestions=["8", "16"])
    event = Event.model_validate(sent)
    client = _server(sent, _task(), [])
    answered = Mock()
    answered.json.return_value = sent | {"explanation": "8", "status": EventStatus.COMPLETE.value}
    client.post = AsyncMock(return_value=answered)

    with patched_client(client):
        await event.answer("8")

    assert [call.args[0] for call in client.get.await_args_list] == ["/event/event-1"]
    client.post.assert_awaited_once_with("/event/event-1/answer", json={"answer": "8"})
    assert str(event) == "Aristotle asked: Which width?\nYou answered: 8"
