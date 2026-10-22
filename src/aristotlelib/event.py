import logging

import pydantic

from aristotlelib import api_request
from aristotlelib.aristotle_object import AristotleObject, SafeIntEnum
from aristotlelib.date_utils import UTCDatetime

logger = logging.getLogger("aristotle")


class EventType(SafeIntEnum):
    UNKNOWN = 0
    MESSAGE = 1
    BUILDING = 2
    THINKING = 3
    EDITING_FILE = 4
    SEARCHING_LOCAL = 5
    RUNNING_COMMAND = 6
    PROVING = 7
    READING_FILES = 8
    REVIEWING = 9
    FINISHED = 10
    ERROR = 11
    READING_LEAN = 12
    SEARCHING_EXTERNAL = 13
    RUNNING_LEAN = 14
    QUESTION = 15
    SUMMARY = 16
    AGENT_QUESTION = 17
    AGENT_FOLLOW_UP = 18


class EventStatus(SafeIntEnum):
    UNKNOWN = 0
    COMPLETE = 1
    SENT = 2


# What an event with no text says Aristotle was doing, in the web app's words.
_ACTIVITIES = {
    EventType.UNKNOWN: "Working",
    EventType.MESSAGE: "User message",
    EventType.BUILDING: "Building",
    EventType.THINKING: "Thinking",
    EventType.EDITING_FILE: "Editing file",
    EventType.SEARCHING_LOCAL: "Searching project",
    EventType.RUNNING_COMMAND: "Running command",
    EventType.PROVING: "Proving",
    EventType.READING_FILES: "Reading files",
    EventType.REVIEWING: "Reviewing",
    EventType.FINISHED: "Finished",
    EventType.ERROR: "Error",
    EventType.READING_LEAN: "Reading Lean",
    EventType.SEARCHING_EXTERNAL: "Searching external sources",
    EventType.RUNNING_LEAN: "Running Lean",
    EventType.SUMMARY: "Output summary",
    EventType.AGENT_FOLLOW_UP: "Aristotle",
}


class Event(AristotleObject):
    event_id: str
    agent_task_id: str
    event_type: EventType
    created_at: UTCDatetime
    content: str
    file_path: str | None
    explanation: str | None
    suggestions: list[str] | None = None
    status: EventStatus
    duration_seconds: float | None = None

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def event_type_name(self) -> str:
        """``event_type``'s name, such as ``AGENT_QUESTION``; ``UNKNOWN`` for one this version doesn't know."""
        return self.event_type.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def status_name(self) -> str:
        """``status``'s name: ``COMPLETE``, ``SENT`` while a question awaits an answer, or ``UNKNOWN``."""
        return self.status.name

    endpoint_prefix = "event"

    @property
    def object_id(self) -> str:
        return self.event_id

    def __str__(self) -> str:
        if self.event_type == EventType.QUESTION:
            if self.explanation:
                return f"You asked: {self.content}\nAnswer: {self.explanation}"
            return f"You asked: {self.content}"
        elif self.event_type == EventType.AGENT_QUESTION:
            response = f"Aristotle asked: {self.content}"
            if self.explanation:
                return response + f"\nYou answered: {self.explanation}"
            elif self.status == EventStatus.COMPLETE:
                return response + "\nAristotle timed out waiting for a response and went with its best guess."
            if self.suggestions:
                suggestions_str = "\n".join(f"  - {s}" for s in self.suggestions)
                return response + f"\nSuggested answers:\n{suggestions_str}"
            return response
        else:
            texts = (self.explanation, self.file_path, self.content)
            content = next((text.strip() for text in texts if text and text.strip()), None)
            if content:
                return f"{self.event_type.name}: {content}"
            # An event with no text, such as a command run without its line, says what Aristotle was doing.
            return _ACTIVITIES.get(self.event_type, self.event_type.name)

    async def answer(self, answer: str) -> None:
        """Answer an agent question.

        Args:
            answer: The answer to the question.
        """
        await self.refresh()
        if self.event_type != EventType.AGENT_QUESTION:
            raise ValueError("Can only answer AGENT_QUESTION events")
        if self.explanation:
            raise ValueError("This question has already been answered")
        if self.status != EventStatus.SENT:
            raise ValueError("This question timed out. The agent did what it thought was best.")

        async with api_request.AristotleRequestClient() as client:
            response = await client.post(
                f"/{self.endpoint_prefix}/{self.event_id}/answer",
                json={"answer": answer},
            )
            self._update_from_response(response.json())
