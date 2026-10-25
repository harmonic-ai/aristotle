"""Tests for the --json result records."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.ci import results as ci_results
from aristotlelib.cli.command import properties
from aristotlelib.cli.command.show import format_property_counts
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.event import Event, EventStatus, EventType
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.question import Question
from aristotlelib.results import DeclarationRecord, PropertyCounts, PropertyStatus, summarize_properties


def _declaration(status: DeclarationStatus) -> Declaration:
    return Declaration(
        project_id="project-1",
        declaration_id=f"id-{status.name}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.AGENT,
        name=f"Spec.{status.name}",
        status=status,
        evidence=DeclarationEvidence.NEGATION_PROVED if status == DeclarationStatus.REFUTED else None,
        filename="Spec.lean",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
    )


VERDICTS = [_declaration(s) for s in (DeclarationStatus.PROVED, DeclarationStatus.REFUTED)]


def _task(status: TaskStatus, created_at: datetime = datetime(2026, 1, 1)) -> AgentTask:
    # Created before VERDICTS were last updated, so it set them unless a test says otherwise.
    return Mock(spec=AgentTask, status=status, created_at=created_at)


LATER = datetime(2026, 1, 3)
"""After VERDICTS were last updated."""


@pytest.mark.parametrize(
    ("task_status", "expected"),
    [
        (TaskStatus.COMPLETE, {"proved": 1, "refuted": 1, "unchecked": 0}),
        (TaskStatus.COMPLETE_WITH_ERRORS, {"proved": 1, "refuted": 1, "unchecked": 0}),
        (None, {"proved": 1, "refuted": 1, "unchecked": 0}),
        (TaskStatus.OUT_OF_BUDGET, {"proved": 0, "refuted": 0, "unchecked": 2}),
        (TaskStatus.FAILED, {"proved": 0, "refuted": 1, "unchecked": 1}),
        (TaskStatus.CANCELED, {"proved": 0, "refuted": 1, "unchecked": 1}),
    ],
)
def test_a_proof_or_disproof_aristotle_never_checked_counts_as_unchecked(task_status, expected):
    # Each ended status has counts, as does a project with no task (None).
    counts, _, _ = summarize_properties(VERDICTS, [_task(task_status)] if task_status else [])

    assert counts is not None
    assert counts.model_dump() == {"in_progress": 0, "unresolved": 0, **expected}


def test_each_listed_property_says_whether_aristotle_checked_it():
    _, _, properties = summarize_properties(VERDICTS, [_task(TaskStatus.CANCELED)])

    assert [(p.name, p.unchecked) for p in properties or []] == [
        ("Spec.PROVED", True),
        ("Spec.REFUTED", False),
    ]


@pytest.mark.parametrize("status", [TaskStatus.OUT_OF_BUDGET, TaskStatus.FAILED, TaskStatus.CANCELED])
def test_a_verdict_a_finished_task_set_stays_checked_after_a_later_task_ends_unchecked(status):
    counts, _, _ = summarize_properties(VERDICTS, [_task(status, created_at=LATER), _task(TaskStatus.COMPLETE)])

    assert counts == PropertyCounts(proved=1, refuted=1)


def test_a_verdict_the_task_that_set_it_left_unchecked_stays_so_after_a_later_task_ends_unchecked():
    counts, _, _ = summarize_properties(
        VERDICTS, [_task(TaskStatus.CANCELED, created_at=LATER), _task(TaskStatus.OUT_OF_BUDGET)]
    )

    assert counts == PropertyCounts(unchecked=2)


def test_a_later_task_that_only_finished_leaves_what_an_earlier_one_left_unchecked_unchecked():
    # Only a task that sets a verdict again, moving its last update, can have checked it.
    counts, _, _ = summarize_properties(
        VERDICTS, [_task(TaskStatus.COMPLETE, created_at=LATER), _task(TaskStatus.OUT_OF_BUDGET)]
    )

    assert counts == PropertyCounts(unchecked=2)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((TaskStatus.COMPLETE, TaskStatus.CANCELED), PropertyCounts(refuted=1, unchecked=1)),
        ((TaskStatus.CANCELED, TaskStatus.COMPLETE), PropertyCounts(refuted=1, unchecked=1)),
        ((TaskStatus.COMPLETE, TaskStatus.OUT_OF_BUDGET), PropertyCounts(unchecked=2)),
        ((TaskStatus.COMPLETE, TaskStatus.COMPLETE_WITH_ERRORS), PropertyCounts(proved=1, refuted=1)),
    ],
    ids=["oldest cancelled", "latest cancelled", "one out of budget", "all finished"],
)
def test_a_verdict_older_than_every_task_listed_is_unchecked_if_any_of_them_could_have_left_it_so(statuses, expected):
    # As when listing the tasks was cut short: each listed task was created after the verdicts.
    tasks = [_task(status, created_at=datetime(2026, 1, 4 - i)) for i, status in enumerate(statuses)]

    counts, _, _ = summarize_properties(VERDICTS, tasks)

    assert counts == expected


def test_a_verdict_updated_the_moment_a_task_was_created_is_that_tasks():
    counts, _, _ = summarize_properties(VERDICTS, [_task(TaskStatus.CANCELED, created_at=datetime(2026, 1, 2))])

    assert counts == PropertyCounts(refuted=1, unchecked=1)


@pytest.mark.parametrize(("offset", "unchecked"), [(timedelta(hours=1), 0), (timedelta(hours=-1), 1)])
def test_a_zoned_creation_time_compares_with_the_zoneless_utc_of_a_verdict(offset, unchecked):
    # 19:00 five hours behind UTC is the verdicts' update, 2026-01-02 00:00 UTC; the cancelled task is created an hour
    # after it, so a finished one set them, or an hour before it, so it did.
    created_at = datetime(2026, 1, 1, 19, tzinfo=timezone(timedelta(hours=-5))) + offset
    tasks = [
        _task(TaskStatus.CANCELED, created_at=created_at),
        _task(TaskStatus.COMPLETE, created_at=datetime(2025, 12, 1)),
    ]

    counts, _, _ = summarize_properties(VERDICTS, tasks)

    assert counts is not None
    assert (counts.proved, counts.unchecked) == (1 - unchecked, unchecked)


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.UNKNOWN])
def test_no_count_stands_until_the_task_has_ended(status):
    counts, _, properties = summarize_properties(VERDICTS, [_task(status)])

    assert counts is None
    assert [p.name for p in properties or []] == ["Spec.PROVED", "Spec.REFUTED"]


def test_a_property_aristotle_withdrew_is_neither_listed_nor_counted():
    withdrawn = _declaration(DeclarationStatus.REMOVED)

    counts, _, properties = summarize_properties([withdrawn, *VERDICTS], [_task(TaskStatus.COMPLETE)])

    assert [p.name for p in properties or []] == ["Spec.PROVED", "Spec.REFUTED"]
    assert counts == PropertyCounts(proved=1, refuted=1)


def test_each_listed_property_carries_its_artifact_paths():
    linked = VERDICTS[1].model_copy(update={"artifact_paths": ["sim/cex.vcd", "sim/cex_tb.sv"]})

    _, _, properties = summarize_properties([VERDICTS[0], linked], [_task(TaskStatus.COMPLETE)])

    assert [p.model_dump(mode="json")["artifact_paths"] for p in properties or []] == [
        [],
        ["sim/cex.vcd", "sim/cex_tb.sv"],
    ]


def test_a_declaration_record_carries_its_artifact_paths():
    linked = VERDICTS[1].model_copy(update={"artifact_paths": ["sim/cex.vcd"]})

    assert DeclarationRecord.of(linked, []).model_dump(mode="json")["artifact_paths"] == ["sim/cex.vcd"]


def test_refutations_count_once_by_evidence_and_keep_evidence_in_summary_json():
    message = "Refuted by a counterexample and a formal proof of the negation."
    declarations = [
        Declaration.model_validate(
            {
                **VERDICTS[1].model_dump(mode="json"),
                "evidence": evidence,
                "evidence_message": message,
                "artifact_paths": ["sim/cex.vcd"],
            }
        )
        for evidence in DeclarationEvidence
        if evidence != DeclarationEvidence.UNKNOWN
    ]

    counts, _, properties = summarize_properties(declarations)

    assert counts == PropertyCounts(refuted=3)
    assert PropertyCounts.model_validate(counts.model_dump(mode="json")) == counts
    assert format_property_counts(declarations) == "0 proved · 3 refuted · 0 in progress"
    assert properties is not None
    both = properties[2].model_dump(mode="json")
    assert both["status_name"] == "REFUTED"
    assert both["evidence"] == DeclarationEvidence.BOTH
    assert both["evidence_message"] == message
    assert both["artifact_paths"] == ["sim/cex.vcd"]


def test_a_declaration_record_carries_unchecked_without_decoding_its_description_again():
    declaration = Declaration.model_validate({**VERDICTS[0].model_dump(mode="json"), "description": "a &amp;lt; b"})

    record = DeclarationRecord.of(declaration, [_task(TaskStatus.CANCELED)])

    assert (record.description, record.unchecked) == ("a &lt; b", True)
    assert record.model_dump(mode="json") == {**declaration.model_dump(mode="json"), "unchecked": True}


@pytest.mark.parametrize(
    ("status", "unchecked", "expected"),
    [
        (DeclarationStatus.PROVED, False, "proved"),
        (DeclarationStatus.REFUTED, False, "refuted"),
        (DeclarationStatus.IN_PROGRESS, False, "in_progress"),
        (DeclarationStatus.FAILED, False, "unresolved"),
        (DeclarationStatus.PROVED, True, "unchecked"),
        (DeclarationStatus.REFUTED, True, "unchecked"),
        (DeclarationStatus.UNKNOWN, False, "unknown"),
        (DeclarationStatus.REMOVED, False, "unknown"),
    ],
)
def test_each_status_comes_to_one_outcome(status, unchecked, expected):
    record = PropertyStatus(declaration_id="d", name="Spec.P", status=status, unchecked=unchecked)

    assert record.model_dump(mode="json")["outcome"] == expected


@pytest.mark.parametrize(
    "status",
    [
        DeclarationStatus.PROVED,
        DeclarationStatus.REFUTED,
        DeclarationStatus.IN_PROGRESS,
        DeclarationStatus.FAILED,
    ],
)
@pytest.mark.parametrize(
    "reason",
    [
        None,
        DeclarationFailureReason.OUT_OF_BUDGET,
        DeclarationFailureReason.SYSTEM_ERROR,
        DeclarationFailureReason.PROOF_CHECK_FAILED,
    ],
)
def test_no_command_calls_a_property_failed_or_negated(status, reason):
    """ "failed" meant refuted in `aristotle ci` and unresolved in `show`; each outcome now has one word."""
    declaration = _declaration(status).model_copy(
        update={"failure_reason": reason if status == DeclarationStatus.FAILED else None}
    )

    labels = [
        format_property_counts([declaration]),
        properties.status_label(declaration, (), short=True),
        properties.status_label(declaration, ()),
        ci_results.result_label(declaration),
        ci_results.RunResults.from_declarations([declaration], duration_seconds=1.0).summary_line(),
    ]

    assert [label for label in labels if "fail" in label.lower() or "negated" in label.lower()] == []


def test_each_numeric_code_has_its_name_beside_it():
    failed = _declaration(DeclarationStatus.FAILED).model_copy(
        update={"failure_reason": DeclarationFailureReason.OUT_OF_BUDGET}
    )
    event = Event(
        event_id="e",
        agent_task_id="t",
        event_type=EventType.AGENT_QUESTION,
        created_at=datetime(2026, 1, 1),
        content="Which depth?",
        file_path=None,
        explanation=None,
        status=EventStatus.SENT,
    )
    project = Project(
        project_id="p",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 1),
        description=None,
        status=ProjectStatus.IDLE,
        has_input=True,
        has_files=False,
    )
    # A code this version doesn't know reads as 0, so its name is UNKNOWN.
    unknown = Declaration.model_validate({**failed.model_dump(mode="json"), "status": 99, "failure_reason": None})
    status = summarize_properties([failed]).properties

    def names(record) -> dict:
        return {key: value for key, value in record.model_dump(mode="json").items() if key.endswith("_name")}

    assert names(failed) == {
        "declaration_type_name": "PROPERTY",
        "source_name": "AGENT",
        "status_name": "FAILED",
        "failure_reason_name": "OUT_OF_BUDGET",
    }
    assert names(event) == {"event_type_name": "AGENT_QUESTION", "status_name": "SENT"}
    assert names(project) == {"status_name": "IDLE"}
    assert (names(unknown)["status_name"], names(unknown)["failure_reason_name"]) == ("UNKNOWN", None)
    assert status is not None and names(status[0]) == {
        "status_name": "FAILED",
        "failure_reason_name": "OUT_OF_BUDGET",
        "source_name": "AGENT",
    }


def test_requested_counts_leave_out_what_aristotle_suggested():
    asked = _declaration(DeclarationStatus.REFUTED).model_copy(update={"source": DeclarationSource.USER})
    # A property of unknown source isn't one Aristotle suggested, so it counts as asked for.
    legacy = _declaration(DeclarationStatus.PROVED).model_copy(
        update={"declaration_id": "id-legacy", "source": DeclarationSource.UNKNOWN}
    )
    suggested = _declaration(DeclarationStatus.PROVED)

    summary = summarize_properties([asked, legacy, suggested], [_task(TaskStatus.COMPLETE)])
    running = summarize_properties([asked], [_task(TaskStatus.IN_PROGRESS)])

    assert summary.property_counts is not None and summary.requested_counts is not None
    assert (summary.property_counts.proved, summary.property_counts.refuted) == (2, 1)
    assert (summary.requested_counts.proved, summary.requested_counts.refuted) == (1, 1)
    assert summary.properties is not None
    assert [p.model_dump(mode="json")["source_name"] for p in summary.properties] == ["USER", "UNKNOWN", "AGENT"]
    assert (running.property_counts, running.requested_counts) == (None, None)


def test_timestamps_carry_their_offset_in_json_and_read_back_as_the_api_sends_them():
    project = Project(
        project_id="p",
        created_at=datetime(2026, 10, 11, 9, 50, 21, 413442),
        last_updated=datetime(2026, 10, 11, 9, 50, 21),
        description=None,
        status=ProjectStatus.IDLE,
        has_input=True,
        has_files=False,
    )

    printed = project.model_dump(mode="json")
    zoned = Project.model_validate({**printed, "created_at": "2026-10-11T11:50:21.413442+02:00"})

    assert (printed["created_at"], printed["last_updated"]) == (
        "2026-10-11T09:50:21.413442+00:00",
        "2026-10-11T09:50:21+00:00",
    )
    # In Python a time stays as the API sends it, without a zone, whichever way it was read.
    assert Project.model_validate(printed).created_at == project.created_at
    assert zoned.created_at == project.created_at and zoned.created_at.tzinfo is None


def test_the_cli_names_a_project_name_and_a_questions_task_agent_task_id_and_reads_them_back():
    project = Project(
        project_id="p",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 1),
        description="FIFO proofs",
        status=ProjectStatus.IDLE,
        has_input=True,
        has_files=False,
    )
    # As the API sends a question, with agent_run_id.
    question = Question.model_validate(
        {
            "question_id": "q",
            "project_id": "p",
            "agent_run_id": "t",
            "content": "Which depth?",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
    )

    printed_project = project.model_dump(mode="json", by_alias=True)
    printed_question = question.model_dump(mode="json", by_alias=True)

    assert (printed_project["name"], "description" in printed_project) == ("FIFO proofs", False)
    assert (printed_question["agent_task_id"], "agent_run_id" in printed_question) == ("t", False)
    # Python keeps the 2.x attribute; a program validating the output gets the same records back.
    assert Project.model_validate(printed_project).description == "FIFO proofs"
    assert Question.model_validate(printed_question).agent_task_id == "t"
