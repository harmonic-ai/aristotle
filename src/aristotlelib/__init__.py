import logging

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import (
    AristotleAPIError,
    AristotleNotFoundError,
    AristotleNotProcessedError,
    AristotleOutcomeUnknownError,
    AristotleProjectLimitError,
    AristotleRequestClient,
    set_api_key,
)
from aristotlelib.event import Event, EventStatus, EventType
from aristotlelib.project import (
    AgentQuestionsSetting,
    AmbiguousProjectError,
    Project,
    ProjectHasNoFilesError,
    ProjectStatus,
)
from aristotlelib.question import Question, QuestionClosed

__all__ = [
    "set_api_key",
    "AgentTask",
    "TaskStatus",
    "Project",
    "ProjectStatus",
    "ProjectHasNoFilesError",
    "AmbiguousProjectError",
    "AgentQuestionsSetting",
    "Event",
    "EventType",
    "EventStatus",
    "Question",
    "QuestionClosed",
    "AristotleRequestClient",
    "AristotleAPIError",
    "AristotleNotFoundError",
    "AristotleNotProcessedError",
    "AristotleOutcomeUnknownError",
    "AristotleProjectLimitError",
]


logging.getLogger("aristotle").addHandler(logging.NullHandler())
