"""Questions Aristotle asks during a task, as listed and answered through the questions endpoint."""

from typing import Any, Literal

import pydantic
from pydantic.functional_validators import ModelWrapValidatorHandler

from aristotlelib import api_request
from aristotlelib.date_utils import UTCDatetime

NOT_ANSWERED = "The question was not answered, so it is safe to answer it again."
"""What an AristotleNotProcessedError for an answer says to do."""


class QuestionClosed(ValueError):
    """The server refused the answer (HTTP 409) because the question is closed."""


class Question(pydantic.BaseModel):
    question_id: str
    # The AGENT_QUESTION event that announced it; None when the server has no such event.
    event_id: str | None = None
    project_id: str
    agent_task_id: str = pydantic.Field(validation_alias=pydantic.AliasChoices("agent_run_id", "agent_task_id"))
    """The task the question was asked during, which the API calls ``agent_run_id``."""
    content: str
    suggestions: list[str] = pydantic.Field(default_factory=list)
    response: str | None = None
    created_at: UTCDatetime
    updated_at: UTCDatetime
    # Set while the agent is blocked on this question.
    blocking_since: UTCDatetime | None = None
    ever_awaited: bool = False
    answered_at: UTCDatetime | None = None
    ignored_at: UTCDatetime | None = None
    # Whether the question's run was live when it was listed; None when that wasn't said.
    _run_live: bool | None = pydantic.PrivateAttr(default=None)

    @property
    def answered(self) -> bool:
        return self.response is not None

    @property
    def ignored(self) -> bool:
        """Closed without an answer."""
        return self.response is None and self.ignored_at is not None

    @property
    def pending(self) -> bool:
        return self.response is None and self.ignored_at is None

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def _read_unanswered(cls, data: Any, handler: ModelWrapValidatorHandler["Question"]) -> "Question":
        """Keep UNANSWERED in a question read back from --json, whose run's liveness is otherwise unknown."""
        question = handler(data)
        if isinstance(data, dict) and data.get("state") == "UNANSWERED":
            question._run_live = False
        return question

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def state(self) -> Literal["PENDING", "ANSWERED", "IGNORED", "UNANSWERED"]:
        """ANSWERED, IGNORED (closed without an answer), PENDING while it awaits one, or UNANSWERED once its run
        has ended without one, as list_for_task() learns when told the run's liveness, so no answer can reach it."""
        if self.answered:
            return "ANSWERED"
        if self.ignored:
            return "IGNORED"
        return "UNANSWERED" if self._run_live is False else "PENDING"

    @classmethod
    async def list_for_task(cls, agent_task_id: str, run_live: bool | None = None) -> list["Question"]:
        """Every question asked during a task, in any state, oldest first; ``run_live`` says whether the task's run
        is live, so that an open question of one that ended is UNANSWERED.

        Raises:
            AristotleAPIError: with status 404 or 405 when the server has no questions endpoint.
        """
        async with api_request.AristotleRequestClient() as client:
            response = await client.get(f"/task/{agent_task_id}/questions")
            questions: list[Question] = [cls.model_validate(item) for item in response.json()]
        for question in questions:
            question._run_live = run_live
        # The server does not promise an order.
        questions.sort(key=lambda question: question.created_at)
        return questions

    async def answer(self, answer: str) -> None:
        """Post the answer and adopt the state the server returns.

        Raises:
            QuestionClosed: the question is already closed (HTTP 409).
        """
        async with api_request.AristotleRequestClient() as client:
            try:
                response = await client.post(f"/question/{self.question_id}/answer", json={"answer": answer})
            except api_request.AristotleOutcomeUnknownError as e:
                raise api_request.AristotleOutcomeUnknownError(
                    f"{e} If it did, the question was answered: check `aristotle questions {self.project_id} --json`, "
                    "where an answered question has `response` set, before answering it again.",
                    e.status_code,
                ) from e
            except api_request.AristotleNotProcessedError as e:
                raise api_request.AristotleNotProcessedError(f"{e} {NOT_ANSWERED}", e.status_code) from e
            except api_request.AristotleAPIError as e:
                if e.status_code == 409:
                    raise QuestionClosed("This question is already closed") from e
                raise
        updated = self.model_validate(response.json())
        for field_name in type(self).model_fields:
            setattr(self, field_name, getattr(updated, field_name))
