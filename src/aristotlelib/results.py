"""Result records the CLI prints under --json; each model's JSON is its schema."""

from collections.abc import Sequence
from typing import Literal, NamedTuple

import pydantic

from aristotlelib.agent_task import TERMINAL_STATUSES, AgentTask, TaskStatus
from aristotlelib.date_utils import as_utc
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
)
from aristotlelib.event import Event
from aristotlelib.project import Project
from aristotlelib.question import Question

VERDICT_STATUSES = frozenset({DeclarationStatus.PROVED, DeclarationStatus.REFUTED})
"""A proof or a refutation, which Aristotle checks when the task that reported it finishes."""

Outcome = Literal["proved", "refuted", "in_progress", "unresolved", "unchecked", "unknown"]
"""What a property's status comes to, in the one word every command, the web app and the MCP use for it."""


def setter(declaration: Declaration, tasks: Sequence[AgentTask]) -> AgentTask | None:
    """The task that last updated ``declaration``, by the project's ``tasks``, newest first: the newest created by its
    last update; None when every listed task is newer."""
    updated = as_utc(declaration.last_updated)
    return next((task for task in tasks if as_utc(task.created_at) <= updated), None)


def unchecked(declaration: Declaration, tasks: Sequence[AgentTask]) -> bool:
    """Whether a property's proof or refutation is one Aristotle never checked, by the project's ``tasks``, newest first.

    Aristotle checks a task's results when it finishes, so a verdict is unchecked if the task that set it, the newest
    created by the verdict's last update, ended first: every verdict if it ran out of budget, a proof if it failed or
    was cancelled. It stays so until a later task updates it, in any way, which moves its last update; a later task
    that only finishes may not have checked it. A verdict older than every task in ``tasks``, as when listing them was
    cut short, was set by an earlier task that isn't listed, so it is unchecked if any listed task's ending would leave
    a verdict so: never more leniently than by the latest alone.
    """
    found = setter(declaration, tasks)
    ended = {found.status} if found is not None else {task.status for task in tasks}
    if TaskStatus.OUT_OF_BUDGET in ended:
        return declaration.status in VERDICT_STATUSES
    return bool(ended & {TaskStatus.FAILED, TaskStatus.CANCELED}) and declaration.status == DeclarationStatus.PROVED


class PropertyCounts(pydantic.BaseModel):
    """A project's properties by status, a proof or refutation Aristotle never checked counted as ``unchecked``."""

    proved: int = 0
    refuted: int = 0
    """Refuted properties, each counted once regardless of evidence kind."""
    in_progress: int = 0
    """No verdict yet: still ``IN_PROGRESS`` when the task ended."""
    unresolved: int = 0
    """``FAILED``: the run stopped first, or the proof no longer checks. Not a refutation."""
    unchecked: int = 0

    @classmethod
    def from_declarations(cls, declarations: list[Declaration], tasks: Sequence[AgentTask]) -> "PropertyCounts":
        """Count by status, with the verdicts the project's ``tasks`` show Aristotle never checked apart, as unchecked()
        judges them; other statuses are not counted."""
        counts = cls()
        for declaration in declarations:
            if unchecked(declaration, tasks):
                counts.unchecked += 1
            elif declaration.status == DeclarationStatus.PROVED:
                counts.proved += 1
            elif declaration.status == DeclarationStatus.REFUTED:
                counts.refuted += 1
            elif declaration.status == DeclarationStatus.IN_PROGRESS:
                counts.in_progress += 1
            elif declaration.status == DeclarationStatus.FAILED:
                counts.unresolved += 1
        return counts


class DeclarationRecord(Declaration):
    """``aristotle properties --json`` and ``property --json``: a declaration as the API gives it, and whether its
    verdict is one Aristotle never checked."""

    unchecked: bool = False
    """True for a proof or refutation Aristotle never checked, as unchecked() judges it by the project's tasks; ``status``
    stays as reported. Always false for a definition."""

    @classmethod
    def of(cls, declaration: Declaration, tasks: Sequence[AgentTask]) -> "DeclarationRecord":
        """``declaration`` with ``unchecked`` judged by the project's ``tasks``, newest first; validated already, so its
        description isn't decoded twice."""
        return cls.model_construct(**dict(declaration), unchecked=unchecked(declaration, tasks))

    @pydantic.field_validator("evidence")
    @classmethod
    def _known_evidence(
        cls, evidence: DeclarationEvidence | None, info: pydantic.ValidationInfo
    ) -> DeclarationEvidence | None:
        """Replaces Declaration's check, so printed records with ``UNKNOWN`` evidence validate as well."""
        return evidence


class PropertyStatus(pydantic.BaseModel):
    """A property's id, name and status, from its declaration."""

    declaration_id: str
    name: str
    status: DeclarationStatus
    failure_reason: DeclarationFailureReason | None = None
    """Set only when ``status`` is ``FAILED``; null otherwise."""
    evidence: DeclarationEvidence | None = None
    evidence_message: str | None = None
    unchecked: bool = False
    """True for a proof or refutation Aristotle never checked, which ``property_counts`` counts as ``unchecked``."""
    source: DeclarationSource = DeclarationSource.UNKNOWN
    """``USER`` for a property you asked for, ``AGENT`` for one Aristotle suggested, as the declaration has it."""
    artifact_paths: list[str] = []
    """Paths in the project of the files Aristotle linked to the property, as ``download --artifact`` takes them."""

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def status_name(self) -> str:
        """``status``'s name, such as ``PROVED``."""
        return self.status.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def failure_reason_name(self) -> str | None:
        """``failure_reason``'s name, such as ``OUT_OF_BUDGET``; null when it is."""
        return None if self.failure_reason is None else self.failure_reason.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def source_name(self) -> str:
        """``source``'s name: ``USER``, ``AGENT`` or ``UNKNOWN``."""
        return self.source.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def outcome(self) -> Outcome:
        """What ``status`` comes to: ``refuted`` for ``REFUTED``, ``in_progress`` for
        ``IN_PROGRESS``, which has no verdict, ``unresolved`` for ``FAILED``, ``unchecked`` for a proof or refutation
        Aristotle never checked, and ``unknown`` for a status this version doesn't know."""
        if self.unchecked and self.status in VERDICT_STATUSES:
            return "unchecked"
        if self.status == DeclarationStatus.PROVED:
            return "proved"
        if self.status == DeclarationStatus.REFUTED:
            return "refuted"
        if self.status == DeclarationStatus.IN_PROGRESS:
            return "in_progress"
        if self.status == DeclarationStatus.FAILED:
            return "unresolved"
        return "unknown"


def requested(declarations: list[Declaration]) -> list[Declaration]:
    """The properties you asked for: every one but those Aristotle suggested, so one of unknown source counts."""
    return [d for d in declarations if d.source != DeclarationSource.AGENT]


class PropertySummary(NamedTuple):
    """A result's properties: ``property_counts`` over every one, ``requested_counts`` over those you asked for, and
    ``properties``."""

    property_counts: PropertyCounts | None
    requested_counts: PropertyCounts | None
    properties: list[PropertyStatus] | None


NO_SUMMARY = PropertySummary(None, None, None)
"""The summary of properties that could not be fetched, or of a wait an interrupt cut short."""


def summarize_properties(declarations: list[Declaration] | None, tasks: Sequence[AgentTask] = ()) -> PropertySummary:
    """A result's counts and ``properties``, but none Aristotle withdrew, judged by the project's ``tasks``, newest
    first, as unchecked() judges them; all None when ``declarations`` is, and the counts None until the latest task
    has ended, when they stand."""
    if declarations is None:
        return NO_SUMMARY
    listed = [d for d in declarations if d.status != DeclarationStatus.REMOVED]
    statuses = [
        PropertyStatus(
            declaration_id=d.declaration_id,
            name=d.name,
            status=d.status,
            failure_reason=d.failure_reason,
            evidence=d.evidence,
            evidence_message=d.evidence_message,
            unchecked=unchecked(d, tasks),
            source=d.source,
            artifact_paths=d.artifact_paths,
        )
        for d in listed
    ]
    if tasks and tasks[0].status not in TERMINAL_STATUSES:
        return PropertySummary(None, None, statuses)
    return PropertySummary(
        PropertyCounts.from_declarations(listed, tasks),
        PropertyCounts.from_declarations(requested(listed), tasks),
        statuses,
    )


class TaskSnapshot(pydantic.BaseModel):
    """``aristotle show --json``: a project's task as it is now, without waiting for it."""

    project_id: str
    project: Project | None
    """The project, as it stood when the result was printed, after any wait, or, in a result an interrupt cut short,
    with its ``status`` and ``latest_run_status`` as the latest task has them, unless ``--task`` named a finished one,
    which may be older; null when it could not be fetched, which only a task asked for with ``--task`` risks."""
    task: AgentTask | None
    """The requested task, or the latest one; null when the project has no task."""
    web_url: str
    """The project's page in Aristotle's web app."""
    events: list[Event]
    """A page of the task's events, the most recent unless ``--pagination-key`` names another, oldest first."""
    result_path: str | None = None
    """Where ``--destination`` saved the result archive; null without it, when there was nothing to download, the task
    had not finished, or saving failed."""
    property_counts: PropertyCounts | None
    """Null until the task has ended, when the counts stand, when the project's properties could not be fetched, or
    when a wait was interrupted."""
    requested_counts: PropertyCounts | None
    """``property_counts`` over the properties you asked for, without those Aristotle suggested; null when it is."""
    properties: list[PropertyStatus] | None
    """Every property but those Aristotle withdrew, most recently updated first; null when they could not be fetched,
    or when a wait was interrupted."""


class SubmitResult(pydantic.BaseModel):
    """``aristotle submit --json``: the new project and its task, as they last stood when ``--wait`` ended."""

    project: Project
    """The project, or, in a result an interrupt cut short, with its ``status`` and ``latest_run_status`` as its task
    has them."""
    task: AgentTask | None
    """The project's task; null when the server lists none or listing it failed."""
    web_url: str
    result_path: str | None = None
    """Where ``--wait`` saved the result archive; null when there was nothing to download, the task had not finished, or saving failed."""
    property_counts: PropertyCounts | None
    """Null until the task has ended, when the counts stand, when the project's properties could not be fetched, or
    when a wait was interrupted."""
    requested_counts: PropertyCounts | None
    """``property_counts`` over the properties you asked for, without those Aristotle suggested; null when it is."""
    properties: list[PropertyStatus] | None
    """Every property but those Aristotle withdrew, most recently updated first; null when they could not be fetched,
    or when a wait was interrupted."""


class ContinueResult(pydantic.BaseModel):
    """``aristotle continue --json``: the task the prompt went to, as it last stood when ``--wait`` ended."""

    project_id: str
    project: Project
    """The project, as it stood when the result was printed, after any wait, or, in a result an interrupt cut short,
    with its ``status`` and ``latest_run_status`` as ``task`` has them."""
    task: AgentTask
    """A new task if the project was idle; otherwise the running one, which the prompt steered."""
    web_url: str
    """The project's page in Aristotle's web app."""
    property_counts: PropertyCounts | None
    """Null until the task has ended, when the counts stand, when the project's properties could not be fetched, or
    when a wait was interrupted."""
    requested_counts: PropertyCounts | None
    """``property_counts`` over the properties you asked for, without those Aristotle suggested; null when it is."""
    properties: list[PropertyStatus] | None
    """Every property but those Aristotle withdrew, most recently updated first; null when they could not be fetched,
    or when a wait was interrupted."""


class QuestionsPending(pydantic.BaseModel):
    """Printed, with exit code 10, when a ``--non-interactive`` wait stops for Aristotle's questions."""

    project_id: str
    task: AgentTask
    questions: list[Question]
    """Every open question of the task, oldest first."""


class OutcomeUnknown(pydantic.BaseModel):
    """Printed under ``--json``, with exit code 7, when a request's response was lost and Aristotle may have acted on it, or
    an earlier copy of it was still in progress when the retries ran out."""

    outcome: Literal["unknown"] = "unknown"
    error: str
    """What failed, and what to check before sending the request again."""


class NotProcessed(pydantic.BaseModel):
    """Printed under ``--json`` when a request that is not safe to repeat failed on every attempt before Aristotle acted
    on it, or was never sent as a request before it failed, so it did nothing and the same command can run again; exit
    code 3, or 6 when rate limited."""

    outcome: Literal["not_processed"] = "not_processed"
    error: str
    """What failed, and that nothing was done."""


class DownloadResult(pydantic.BaseModel):
    """``aristotle download --json``: where the archive was saved."""

    project_id: str
    result_path: str


class SavedArtifact(pydantic.BaseModel):
    """A file ``aristotle download --artifact`` saved."""

    artifact_path: str
    """Its path in the project, as ``--artifact`` gave it."""
    saved_path: str
    """Where it was saved: at that path under ``--destination``."""


class ArtifactDownloadResult(pydantic.BaseModel):
    """``aristotle download --artifact --json``: where each file was saved, and which the project has no file at."""

    project_id: str
    files: list[SavedArtifact]
    """The files saved, in the order given."""
    missing: list[str]
    """The paths given that the project has no file at, for which the command exits 4."""


class LoginPending(pydantic.BaseModel):
    """``aristotle login --json``, first, while the login waits for approval in the browser: what to open and type."""

    verification_uri: str
    """The page to open, in a browser signed in to Aristotle."""
    user_code: str
    """The code to type on that page."""
    expires_in: int
    """Seconds until the code expires."""


class LoginResult(pydantic.BaseModel):
    """``aristotle login --json``, last: where the key was stored."""

    credentials_path: str
    key_name: str | None
    """The key's name on the API keys page, for a key approved in the browser; null for one given, piped or pasted."""


class ProjectPage(pydantic.BaseModel):
    """``aristotle list --json``: a page of your projects, newest first."""

    projects: list[Project]
    pagination_key: str | None
    """The next page's key, as ``--pagination-key`` takes it; null on the last page."""


class TaskPage(pydantic.BaseModel):
    """``aristotle tasks --json``: a page of a project's tasks, newest first."""

    project_id: str
    tasks: list[AgentTask]
    pagination_key: str | None
    """The next page's key, as ``--pagination-key`` takes it; null on the last page."""


class DeclarationPage(pydantic.BaseModel):
    """``aristotle properties --json``: a project's properties or definitions, read from every page."""

    project_id: str
    declarations: list[DeclarationRecord]
    pagination_key: str | None
    """Always null: there is no next page."""


class TaskQuestions(pydantic.BaseModel):
    """``aristotle questions --json``: a task's questions, as the exit-10 result gives a wait's open ones."""

    project_id: str
    task: AgentTask | None
    """The requested task, or the latest one; null when the project has no task."""
    questions: list[Question]
    """Every question of the task, oldest first; with ``--answer``, the ones it answered."""
