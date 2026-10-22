"""Tests for aristotlelib.question module."""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.question import Question, QuestionClosed


def _payload(**overrides):
    payload = {
        "question_id": "q-1",
        "event_id": "event-1",
        "project_id": "project-123",
        "agent_run_id": "task-456",
        "content": "Which reset polarity does the arbiter use?",
        "suggestions": ["Active low", "Active high"],
        "response": None,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "blocking_since": None,
        "ever_awaited": False,
        "answered_at": None,
        "ignored_at": None,
    }
    payload.update(overrides)
    return payload


def _client(**methods):
    mock_client = AsyncMock()
    for name, method in methods.items():
        setattr(mock_client, name, method)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def test_question_pending_answered_and_ignored():
    pending = Question.model_validate(_payload())
    assert pending.pending
    assert not pending.answered
    assert not pending.ignored
    assert pending.state == "PENDING"

    answered = Question.model_validate(_payload(response="Active low", answered_at="2024-01-01T00:05:00"))
    assert answered.answered
    assert not answered.pending
    assert not answered.ignored
    assert answered.state == "ANSWERED"

    ignored = Question.model_validate(_payload(ignored_at="2024-01-01T00:15:00", ever_awaited=True))
    assert ignored.ignored
    assert not ignored.pending
    assert not ignored.answered
    assert ignored.state == "IGNORED"


def test_question_with_a_response_is_answered_even_once_ignored():
    """A response wins over ignored_at."""
    question = Question.model_validate(_payload(response="Active low", ignored_at="2024-01-01T00:15:00"))
    assert question.answered
    assert not question.ignored
    assert not question.pending


def test_question_tolerates_a_missing_optional_field():
    payload = _payload()
    del payload["blocking_since"]
    del payload["event_id"]
    question = Question.model_validate(payload)
    assert question.blocking_since is None
    assert question.event_id is None


async def test_list_for_task_hits_the_task_endpoint_and_sorts_oldest_first():
    mock_response = Mock()
    mock_response.json.return_value = [
        _payload(question_id="q-2", created_at="2024-01-01T00:10:00"),
        _payload(question_id="q-1", created_at="2024-01-01T00:00:00"),
    ]
    mock_client = _client(get=AsyncMock(return_value=mock_response))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        questions = await Question.list_for_task("task-456")

    mock_client.get.assert_called_once_with("/task/task-456/questions")
    assert [question.question_id for question in questions] == ["q-1", "q-2"]
    assert questions[0].suggestions == ["Active low", "Active high"]
    assert questions[0].created_at == datetime(2024, 1, 1, 0, 0)


async def test_list_for_task_surfaces_a_missing_endpoint():
    mock_client = _client(get=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(AristotleAPIError) as excinfo:
            await Question.list_for_task("task-456")

    assert excinfo.value.status_code == 404


async def test_answer_posts_the_answer_and_takes_the_servers_state():
    question = Question.model_validate(_payload())
    mock_response = Mock()
    mock_response.json.return_value = _payload(
        response="Active low", answered_at="2024-01-01T00:05:00", updated_at="2024-01-01T00:05:00"
    )
    mock_client = _client(post=AsyncMock(return_value=mock_response))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await question.answer("Active low")

    mock_client.post.assert_called_once_with("/question/q-1/answer", json={"answer": "Active low"})
    assert question.answered
    assert question.response == "Active low"
    assert question.answered_at == datetime(2024, 1, 1, 0, 5)


async def test_answer_reports_a_closed_question():
    question = Question.model_validate(_payload())
    mock_client = _client(post=AsyncMock(side_effect=AristotleAPIError("conflict", status_code=409)))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(QuestionClosed, match="already closed") as excinfo:
            await question.answer("Active low")

    # Still a ValueError, like Event.answer raises.
    assert isinstance(excinfo.value, ValueError)

    assert question.pending


async def test_answer_passes_other_errors_through():
    question = Question.model_validate(_payload())
    mock_client = _client(post=AsyncMock(side_effect=AristotleAPIError("forbidden", status_code=403)))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(AristotleAPIError) as excinfo:
            await question.answer("Active low")

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(("run_live", "state"), [(True, "PENDING"), (None, "PENDING"), (False, "UNANSWERED")])
async def test_an_open_question_of_a_run_that_ended_is_unanswered(run_live, state):
    client = _client(get=AsyncMock(return_value=Mock(json=Mock(return_value=[_payload(), _payload(response="Yes")]))))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        questions = await Question.list_for_task("task-456", run_live=run_live)

    assert [q.model_dump(mode="json")["state"] for q in questions] == [state, "ANSWERED"]


def test_an_unanswered_question_read_back_from_its_json_stays_unanswered():
    """A script validating `questions --json` against the model gets the state it printed."""
    question = Question.model_validate(_payload())
    question._run_live = False
    printed = question.model_dump(mode="json")

    assert printed["state"] == "UNANSWERED"
    assert Question.model_validate(printed).state == "UNANSWERED"
    assert Question.model_validate({**printed, "state": "PENDING"}).state == "PENDING"
