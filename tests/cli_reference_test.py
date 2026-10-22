"""The CLI reference's JSON output section names every field the --json records carry."""

from pathlib import Path

import pydantic
import pytest

from aristotlelib.agent_task import AgentTask
from aristotlelib.declaration import Declaration
from aristotlelib.event import Event
from aristotlelib.project import Project
from aristotlelib.question import Question
from aristotlelib.results import (
    ArtifactDownloadResult,
    ContinueResult,
    DeclarationPage,
    DownloadResult,
    LoginPending,
    LoginResult,
    NotProcessed,
    OutcomeUnknown,
    ProjectPage,
    PropertyCounts,
    PropertyStatus,
    QuestionsPending,
    SavedArtifact,
    SubmitResult,
    TaskPage,
    TaskQuestions,
    TaskSnapshot,
)

REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "cli-reference.md"


def _json_output_section() -> str:
    text = REFERENCE.read_text(encoding="utf-8")
    start = text.index("## JSON output\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


@pytest.mark.parametrize(
    "model",
    [
        Project,
        AgentTask,
        Event,
        Question,
        Declaration,
        TaskSnapshot,
        SubmitResult,
        ContinueResult,
        DownloadResult,
        ArtifactDownloadResult,
        SavedArtifact,
        LoginPending,
        LoginResult,
        PropertyCounts,
        PropertyStatus,
        QuestionsPending,
        OutcomeUnknown,
        NotProcessed,
        ProjectPage,
        TaskPage,
        DeclarationPage,
        TaskQuestions,
    ],
    ids=lambda model: model.__name__,
)
def test_every_field_is_documented(model: type[pydantic.BaseModel]):
    section = _json_output_section()

    # The serialization schema names every key the JSON carries, the computed ones too, on any pydantic 2.
    names = model.model_json_schema(mode="serialization")["properties"]
    missing = [name for name in names if f"`{name}`" not in section and f'"{name}"' not in section]

    assert missing == []
