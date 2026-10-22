"""Tests for the questions command."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError, AristotleOutcomeUnknownError
from aristotlelib.cli.command.questions import QuestionsAction
from aristotlelib.cli.controller import main
from aristotlelib.cli.question_flow import DEFAULT_DEADLINE_SECONDS, FlowResult
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.question import Question, QuestionClosed
from tests.cli.conftest import LOOKUP_FAILURES


def _action() -> QuestionsAction:
    parser = argparse.ArgumentParser()
    action = QuestionsAction(parser.add_subparsers())
    action.add_action_arguments()
    return action


def _question(number: int, response: str | None = None, ignored: bool = False) -> Question:
    return Question(
        question_id=f"q-{number}",
        event_id=f"event-{number}",
        project_id="project-123",
        agent_run_id="task-456",
        content=f"Question number {number}, which is long enough to need clipping in the table?",
        suggestions=["Active low", "Active high"],
        response=response,
        created_at=datetime(2024, 1, 1, 0, number),
        updated_at=datetime(2024, 1, 1, 0, number),
        answered_at=datetime(2024, 1, 1, 0, number + 1) if response else None,
        ignored_at=datetime(2024, 1, 1, 0, number + 15) if ignored else None,
    )


def _listed(*questions: Question) -> AsyncMock:
    """Question.list_for_task returning ``questions``, told the run's liveness as the real one is."""

    async def list_for_task(agent_task_id: str, run_live: bool | None = None) -> list[Question]:
        for question in questions:
            question._run_live = run_live
        return list(questions)

    return AsyncMock(side_effect=list_for_task)


def _latest_task() -> AgentTask:
    return AgentTask(
        project_id="project-123",
        agent_task_id="task-456",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime(2024, 1, 1),
        last_updated_at=datetime(2024, 1, 1),
        percent_complete=10,
        file_name=None,
        description=None,
        output_summary=None,
    )


def _project_with(task: AgentTask) -> Mock:
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_tasks = AsyncMock(return_value=([task], None))
    return mock_project


def _never_prompt():
    return patch(READ_LINE, AsyncMock(side_effect=AssertionError("must not prompt")))


READ_LINE = "aristotlelib.cli.command.questions.read_stdin_line"


async def test_questions_table_output(capsys, caplog):
    action = _action()
    args = action.parser.parse_args(["project-123"])
    questions = [_question(1, response="Active low"), _question(2), _question(3, ignored=True)]

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=questions)),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=False),
        _never_prompt(),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(args)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["STATUS", "ASKED", "QUESTION", "ANSWER"]
    assert lines[2].startswith("ANSWERED")
    assert "Active low" in lines[2]
    assert lines[3].startswith("PENDING")
    assert lines[4].startswith("IGNORED")
    assert "Question number 2, which is long enough to need..." in lines[3]
    assert "clipping" not in lines[3]
    assert (
        "1 question pending. Answer at a terminal with: aristotle questions project-123\n"
        "or from a script with: aristotle questions project-123 --answer <question-id>=<answer>"
    ) in caplog.messages


async def test_questions_json_output(capsys):
    action = _action()
    args = action.parser.parse_args(["project-123", "--json"])
    questions = [_question(1), _question(2, response="Active high")]

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=questions)),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        _never_prompt(),
    ):
        await action.run_action(args)

    page = json.loads(capsys.readouterr().out)
    # The keys of the exit-10 result, so a script reads both alike.
    assert list(page) == ["project_id", "task", "questions"]
    data = page["questions"]
    assert [item["question_id"] for item in data] == ["q-1", "q-2"]
    assert data[0]["response"] is None
    assert data[1]["response"] == "Active high"
    assert data[0]["created_at"] == "2024-01-01T00:01:00+00:00"
    assert data[0]["agent_task_id"] == page["task"]["agent_task_id"] and "agent_run_id" not in data[0]


async def test_questions_with_task_id():
    action = _action()
    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = _latest_task()
    mock_task.agent_task_id = "task-789"

    with (
        patch("aristotlelib.cli.command.questions.AgentTask.from_id", return_value=mock_task) as mock_from_id,
        patch("aristotlelib.cli.command.questions.Project.from_id") as mock_project_from_id,
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[])) as listed,
    ):
        await action.run_action(args)

    mock_from_id.assert_called_once_with("task-789")
    mock_project_from_id.assert_not_called()
    listed.assert_called_once_with("task-789", run_live=True)


async def test_questions_task_project_mismatch(caplog):
    action = _action()
    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = _latest_task()
    mock_task.project_id = "different-project"

    with (
        patch("aristotlelib.cli.command.questions.AgentTask.from_id", return_value=mock_task),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock()) as listed,
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(args)

    assert exited.value.code == 4
    listed.assert_not_called()
    assert "belongs to project different-project" in caplog.text


async def test_questions_task_project_mismatch_writes_no_json(capsys, caplog):
    action = _action()
    mock_task = _latest_task()
    mock_task.project_id = "different-project"

    with (
        patch("aristotlelib.cli.command.questions.AgentTask.from_id", return_value=mock_task),
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-789", "--json"]))

    assert exited.value.code == 4
    assert capsys.readouterr().out == ""
    assert "belongs to project different-project" in caplog.text


PROJECT_ID = "2fa30605-173d-4e2b-8cad-849b55641da3"


@pytest.mark.parametrize("typed", [PROJECT_ID.upper(), PROJECT_ID.replace("-", "")])
async def test_questions_task_accepts_the_project_id_in_another_case_or_without_dashes(typed, capsys):
    action = _action()
    task = _latest_task()
    task.project_id = PROJECT_ID

    with (
        patch("aristotlelib.cli.command.questions.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[])) as listed,
    ):
        await action.run_action(action.parser.parse_args([typed, "--task", "task-456", "--json"]))

    listed.assert_awaited_once_with("task-456", run_live=True)
    assert json.loads(capsys.readouterr().out)["questions"] == []


async def test_questions_no_tasks_with_json_prints_no_questions(capsys):
    action = _action()
    mock_project = Mock(spec=Project)
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.cli.command.questions.Project.from_id", return_value=mock_project):
        await action.run_action(action.parser.parse_args(["project-empty", "--json"]))

    assert json.loads(capsys.readouterr().out) == {"project_id": "project-empty", "task": None, "questions": []}


async def test_questions_no_tasks(caplog):
    action = _action()
    args = action.parser.parse_args(["project-empty"])

    mock_project = Mock(spec=Project)
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=mock_project),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(args)

    assert "No tasks found for this project." in caplog.text


async def test_questions_none_asked(caplog):
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(args)

    assert "Aristotle has not asked any questions on this task." in caplog.text


async def test_questions_offers_to_answer_when_pending_and_at_a_terminal(capsys):
    action = _action()
    args = action.parser.parse_args(["project-123"])
    answered, pending = _question(1, response="Active low"), _question(2)

    task = _latest_task()

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch(
            "aristotlelib.cli.command.questions.Question.list_for_task",
            new=AsyncMock(return_value=[answered, pending]),
        ),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch(READ_LINE, AsyncMock(return_value="")) as reader,
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock(return_value=FlowResult())) as flow,
    ):
        await action.run_action(args)

    assert "Answer it now? [Y/n]" in capsys.readouterr().out
    flow.assert_called_once()
    assert flow.call_args.args[0] == [pending]
    assert flow.call_args.kwargs["read_line"] is reader
    assert flow.call_args.kwargs["deadline_seconds"] == DEFAULT_DEADLINE_SECONDS
    assert flow.call_args.kwargs["run_is_live"] == task.refresh_live


async def test_questions_declined_offer_runs_no_flow(capsys):
    """And says so, for someone who answered a yes-or-no question with "no"."""
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[_question(1)])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch(READ_LINE, AsyncMock(return_value="n")),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
    ):
        await action.run_action(args)

    flow.assert_not_called()
    assert "Nothing was sent. Answer later with: aristotle questions project-123" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("replies", "flows", "asked_again"),
    [
        (["Registered, it saturates", "y"], 1, 1),
        (["maybe", "no"], 0, 1),
        # A pasted answer of two paragraphs: the blank line between them doesn't start answering.
        (["Rising edge", "", "Sampled on posedge", "n"], 0, 3),
    ],
)
async def test_any_other_reply_at_the_offer_sends_nothing_says_so_and_asks_again(replies, flows, asked_again, capsys):
    """An answer typed at the [Y/n] prompt isn't dropped silently, nor sent as the answer to some question."""
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[_question(1)])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch(READ_LINE, AsyncMock(side_effect=replies)),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock(return_value=FlowResult())) as flow,
    ):
        await action.run_action(args)

    out = capsys.readouterr().out
    assert out.count("Nothing was sent. Type y to answer, n to leave it.") == asked_again
    assert flow.call_count == flows


async def test_questions_eof_at_the_offer_is_a_no():
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[_question(1)])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch(READ_LINE, AsyncMock(side_effect=EOFError)),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
    ):
        await action.run_action(args)

    flow.assert_not_called()


async def test_questions_never_prompts_without_a_terminal(caplog):
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch(
            "aristotlelib.cli.command.questions.Question.list_for_task",
            new=AsyncMock(return_value=[_question(1), _question(2)]),
        ),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=False),
        _never_prompt(),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(args)

    flow.assert_not_called()
    assert (
        "2 questions pending. Answer at a terminal with: aristotle questions project-123\n"
        "or from a script with: aristotle questions project-123 --answer <question-id>=<answer>"
    ) in caplog.messages


async def test_questions_nothing_pending_does_not_offer():
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch(
            "aristotlelib.cli.command.questions.Question.list_for_task",
            new=AsyncMock(return_value=[_question(1, response="Active low")]),
        ),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        _never_prompt(),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
    ):
        await action.run_action(args)

    flow.assert_not_called()


async def test_questions_enter_at_the_offer_runs_the_real_flow(capsys):
    action = _action()
    args = action.parser.parse_args(["project-123"])
    pending = _question(2)
    typed = ["", "1", ""]

    async def fake_read_line() -> str:
        return typed.pop(0)

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(_latest_task())),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[pending])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        _never_prompt(),
        patch("aristotlelib.cli.command.questions.read_stdin_line", new=fake_read_line),
        patch.object(AgentTask, "refresh", new=AsyncMock()) as refreshed,
        patch.object(Question, "answer", autospec=True) as answer,
    ):
        await action.run_action(args)

    refreshed.assert_awaited_once()
    answer.assert_called_once_with(pending, "Active low")
    out = capsys.readouterr().out
    assert "Question 1 of 1" in out
    assert "Answered question 1: Active low" in out


async def test_questions_sends_nothing_when_the_run_cannot_be_checked(capsys):
    """A failed status check before sending leaves every question pending."""
    action = _action()
    task = _latest_task()
    pending = _question(2)
    typed = ["", "1", ""]

    async def fake_read_line() -> str:
        return typed.pop(0)

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[pending])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch("aristotlelib.cli.command.questions.read_stdin_line", new=fake_read_line),
        patch.object(AgentTask, "refresh", new=AsyncMock(side_effect=AristotleAPIError("Request failed"))),
        patch.object(Question, "answer", autospec=True) as answer,
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))

    answer.assert_not_called()
    out = capsys.readouterr().out
    assert "Could not check that the run is still live, so nothing was sent: Request failed" in out
    assert "Answer again with: aristotle questions project-123" in out


async def test_questions_sends_nothing_when_the_run_ended_during_the_flow(capsys):
    """The run is re-checked just before sending; one that ended meanwhile gets no answers."""
    action = _action()
    task = _latest_task()
    pending = _question(2)
    typed = ["", "1", ""]

    async def fake_read_line() -> str:
        return typed.pop(0)

    async def run_ended() -> None:
        task.status = TaskStatus.COMPLETE

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[pending])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        patch("aristotlelib.cli.command.questions.read_stdin_line", new=fake_read_line),
        patch.object(AgentTask, "refresh", new=AsyncMock(side_effect=run_ended)),
        patch.object(Question, "answer", autospec=True) as answer,
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))

    answer.assert_not_called()
    out = capsys.readouterr().out
    assert "The run has ended, so nothing was sent; no answer can reach Aristotle now." in out
    assert "Answered question" not in out


@pytest.mark.parametrize("status", [TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.CANCELED])
async def test_questions_from_an_ended_run_are_unanswered_and_not_offered(status, capsys, caplog):
    """As on the web: an open question is awaiting an answer only while its run is live."""
    action = _action()
    task = _latest_task()
    task.status = status

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=_listed(_question(1))),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        _never_prompt(),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))

    flow.assert_not_called()
    assert "UNANSWERED" in capsys.readouterr().out
    assert "The run has ended" in caplog.text


async def test_questions_on_someone_elses_project_are_not_offered(caplog):
    action = _action()
    task = _latest_task()
    task.read_only = True

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=[_question(1)])),
        patch("aristotlelib.cli.command.questions.can_prompt", return_value=True),
        _never_prompt(),
        patch("aristotlelib.cli.command.questions.run_question_flow", new=AsyncMock()) as flow,
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))

    flow.assert_not_called()
    assert "Only the project's owner can answer these." in caplog.text


async def _answer(argv: list[str], task: AgentTask, questions: list[Question], answer: AsyncMock | None = None):
    action = _action()
    answer = answer or AsyncMock()
    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=AsyncMock(return_value=questions)),
        patch.object(Question, "answer", answer),
        _never_prompt(),
    ):
        # Preflight, then the action, as run() does.
        args = action.parser.parse_args(["project-123", *argv])
        code = action.preflight(args)
        if code is None:
            code = await action.run_action(args)
    return code, answer


async def test_answer_sends_each_answer_without_prompting(caplog, capsys):
    q1, q2 = _question(1), _question(2)

    with caplog.at_level(logging.INFO):
        code, answer = await _answer(
            ["--answer", "q-1=Active low", "--answer", " q-2 = 8 bits "], _latest_task(), [q1, q2]
        )

    assert code == 0
    assert [c.args for c in answer.await_args_list] == [("Active low",), ("8 bits",)]
    assert caplog.messages == ["Answered question q-1: Active low", "Answered question q-2: 8 bits"]
    assert capsys.readouterr().out == ""


async def test_answer_with_json_prints_the_answered_questions(capsys):
    q1 = _question(1)

    async def answered(text: str) -> None:
        q1.response = text

    code, _ = await _answer(
        ["--answer", "q-1=Active low", "--json"], _latest_task(), [q1], AsyncMock(side_effect=answered)
    )

    assert code == 0
    [record] = json.loads(capsys.readouterr().out)["questions"]
    assert record["question_id"] == "q-1"
    assert record["response"] == "Active low"


async def test_an_answer_whose_response_is_lost_exits_7_once_the_rest_are_sent(caplog):
    lost = AristotleOutcomeUnknownError(
        "ReadTimeout on POST /question/q-1/answer. Aristotle may have acted on it, so it was not sent again."
    )

    code, answer = await _answer(
        ["--answer", "q-1=yes", "--answer", "q-2=no"],
        _latest_task(),
        [_question(1), _question(2)],
        AsyncMock(side_effect=[lost, None]),
    )

    assert code == 7
    assert answer.await_count == 2
    assert caplog.messages[0] == f"Question q-1 could not be answered: {lost}"


@pytest.mark.parametrize(
    "lost_at",
    [1, 0],
    ids=["after another failure", "before another failure"],
)
@pytest.mark.parametrize(
    "failure",
    [QuestionClosed("This question is already closed"), AristotleAPIError("Request failed")],
    ids=["closed meanwhile", "request failed"],
)
async def test_a_lost_answer_exits_7_whatever_else_failed(failure, lost_at):
    side_effect: list[Exception | None] = [failure, None]
    side_effect.insert(
        lost_at, AristotleOutcomeUnknownError("ReadTimeout. Aristotle may have acted on it, so it was not sent again.")
    )

    code, answer = await _answer(
        ["--answer", "q-1=yes", "--answer", "q-2=no", "--answer", "q-3=maybe"],
        _latest_task(),
        [_question(1), _question(2), _question(3)],
        AsyncMock(side_effect=side_effect),
    )

    assert code == 7
    assert answer.await_count == 3


async def test_an_unknown_question_id_sends_nothing_and_exits_4(caplog):
    code, answer = await _answer(["--answer", "q-1=yes", "--answer", "q-9=no"], _latest_task(), [_question(1)])

    assert code == 4
    answer.assert_not_awaited()
    assert caplog.messages[0].startswith("Task task-456 has no question q-9.")


async def test_a_question_answered_twice_is_a_usage_error_that_sends_nothing(capsys):
    answer = AsyncMock()

    with pytest.raises(SystemExit) as exited:
        await _answer(["--answer", "q-1=yes", "--answer", "q-1=no"], _latest_task(), [_question(1)], answer)

    assert exited.value.code == 2
    answer.assert_not_awaited()
    assert capsys.readouterr().err.endswith("error: Each question takes one answer; q-1 has more than one.\n")


@pytest.mark.parametrize(
    "question",
    [_question(1, response="Active low"), _question(1, ignored=True)],
    ids=["answered", "ignored"],
)
async def test_a_closed_question_sends_nothing_and_exits_4(question, caplog):
    code, answer = await _answer(
        ["--answer", "q-1=yes", "--answer", "q-2=no"], _latest_task(), [question, _question(2)]
    )

    assert code == 4
    answer.assert_not_awaited()
    assert caplog.messages == ["Question q-1 is already closed. Nothing was sent."]


async def test_answers_to_an_ended_run_or_someone_elses_project_are_not_sent(caplog):
    ended = _latest_task()
    ended.status = TaskStatus.COMPLETE
    shared = _latest_task()
    shared.read_only = True

    code_ended, answer_ended = await _answer(["--answer", "q-1=yes"], ended, [_question(1)])
    code_shared, answer_shared = await _answer(["--answer", "q-1=yes"], shared, [_question(1)])

    assert (code_ended, code_shared) == (4, 4)
    answer_ended.assert_not_awaited()
    answer_shared.assert_not_awaited()
    assert caplog.messages == [
        "The run has ended, so no answer can reach Aristotle now. Nothing was sent.",
        "Only the project's owner can answer these. Nothing was sent.",
    ]


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        (QuestionClosed("This question is already closed"), 4, "Question q-1: This question is already closed"),
        (AristotleAPIError("Request failed"), 3, "Question q-1 could not be answered: Request failed"),
        (AristotleAPIError("Too many requests", 429), 6, "Question q-1 could not be answered: Too many requests"),
        (AristotleAPIError("Unauthorized", 401), 5, "Question q-1 could not be answered: Unauthorized"),
    ],
    ids=["closed meanwhile", "request failed", "rate limited", "key rejected"],
)
async def test_a_refused_answer_still_sends_the_rest(failure, code, message, caplog):
    answer = AsyncMock(side_effect=[failure, None])

    with caplog.at_level(logging.INFO):
        result, _ = await _answer(
            ["--answer", "q-1=yes", "--answer", "q-2=no"], _latest_task(), [_question(1), _question(2)], answer
        )

    assert result == code
    assert answer.await_count == 2
    assert caplog.messages == [message, "Answered question q-2: no"]


REFUSED = [httpx.ConnectError("[Errno 111] Connection refused")] * api_request.RETRY_ATTEMPTS


def _project_record() -> httpx.Response:
    project = Project(
        project_id="project-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
        description=None,
        has_input=True,
        has_files=True,
    )
    return httpx.Response(200, json=project.model_dump(mode="json"))


def _tasks_record() -> httpx.Response:
    return httpx.Response(200, json={"agent_tasks": [_latest_task().model_dump(mode="json")], "pagination_key": None})


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize(("failure", "code", "message"), LOOKUP_FAILURES)
@pytest.mark.parametrize(
    ("answers", "advice"),
    [
        (["--answer", "q-1=yes"], "The question was not answered, so it is safe to answer it again."),
        (
            ["--answer", "q-1=yes", "--answer", "q-2=no"],
            "No question was answered, so it is safe to answer them again.",
        ),
    ],
    ids=["one answer", "two answers"],
)
def test_answers_whose_project_lookup_failed_say_none_was_sent(
    failure, code, message, answers, advice, api_server, caplog, capsys
):
    requests = api_server(*[failure] * api_request.RETRY_ATTEMPTS)

    with patch("sys.argv", ["aristotle", "questions", "project-123", *answers, "--json"]):
        assert main() == code

    assert [request.method for request in requests] == ["GET"] * api_request.RETRY_ATTEMPTS
    error = f"{message.format(request='GET /project/project-123')} {advice}"
    assert caplog.messages[-1] == error
    assert json.loads(capsys.readouterr().out) == {"outcome": "not_processed", "error": error}


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize(
    ("argv", "answered", "failed"),
    [
        (["--task", "task-456"], [], "GET /task/task-456"),
        ([], [_project_record], "GET /project/project-123/tasks"),
        ([], [_project_record, _tasks_record], "GET /task/task-456/questions"),
    ],
    ids=["the task", "the latest task", "the questions"],
)
@pytest.mark.parametrize("json_flag", [True, False], ids=["json", "text"])
def test_answers_are_not_sent_when_any_lookup_fails(argv, answered, failed, json_flag, api_server, caplog, capsys):
    requests = api_server(*(record() for record in answered), *REFUSED)

    flags = ["--json"] if json_flag else []
    with patch("sys.argv", ["aristotle", "questions", "project-123", "--answer", "q-1=yes", *argv, *flags]):
        assert main() == 3

    assert {request.method for request in requests} == {"GET"}
    error = (
        f"ConnectError on {failed} after 5 attempts over 0 s: [Errno 111] Connection refused. The question was not "
        "answered, so it is safe to answer it again."
    )
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    if json_flag:
        assert json.loads(out) == {"outcome": "not_processed", "error": error}
    else:
        assert out == ""


@pytest.mark.usefixtures("frozen_api_clock")
def test_a_listing_whose_lookup_failed_sends_nothing_to_report(api_server, caplog, capsys):
    api_server(*REFUSED)

    with patch("sys.argv", ["aristotle", "questions", "project-123", "--json"]):
        assert main() == 3

    assert caplog.messages[-1] == (
        "ConnectError on GET /project/project-123 after 5 attempts over 0 s: [Errno 111] Connection refused"
    )
    assert capsys.readouterr().out == ""


async def test_answer_on_a_project_without_tasks_exits_4():
    action = _action()
    mock_project = Mock(spec=Project)
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.cli.command.questions.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-empty", "--answer", "q-1=yes"])) == 4


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("yes", "expected QUESTION_ID=ANSWER, got 'yes'"),
        ("=yes", "expected QUESTION_ID=ANSWER, got '=yes'"),
        ("q-1=", "the answer to q-1 is empty"),
        ("q-1=   ", "the answer to q-1 is empty"),
    ],
)
def test_a_malformed_answer_is_a_usage_error(value, error, capsys):
    with pytest.raises(SystemExit) as exited:
        _action().parser.parse_args(["project-123", "--answer", value])

    assert exited.value.code == 2
    assert error in capsys.readouterr().err


def test_an_answer_may_contain_an_equals_sign():
    args = _action().parser.parse_args(["project-123", "--answer", "q-1=width = 8"])

    assert args.answer == [("q-1", "width = 8")]


@pytest.mark.parametrize(
    ("status", "open_state"), [(TaskStatus.IN_PROGRESS, "PENDING"), (TaskStatus.COMPLETE, "UNANSWERED")]
)
async def test_questions_json_gives_each_ones_state_as_the_table_does(status, open_state, capsys):
    action = _action()
    task = _latest_task()
    task.status = status
    questions = [_question(1), _question(2, response="Active high")]

    with (
        patch("aristotlelib.cli.command.questions.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.cli.command.questions.Question.list_for_task", new=_listed(*questions)),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--json"]))

    assert [item["state"] for item in json.loads(capsys.readouterr().out)["questions"]] == [open_state, "ANSWERED"]
