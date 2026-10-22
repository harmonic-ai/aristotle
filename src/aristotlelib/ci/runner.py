"""Submitting a scoped project and polling it to a verdict."""

import asyncio
import contextlib
import dataclasses
import io
import logging
import os
import random
import re
import secrets
import signal
import stat
import sys
import tarfile
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Generator, Iterable
from pathlib import Path
from typing import Any

from aristotlelib import local_file_utils
from aristotlelib.agent_task import TERMINAL_STATUSES, AgentTask, TaskStatus
from aristotlelib.api_request import (
    AristotleAPIError,
    AristotleOutcomeUnknownError,
    AristotleProjectLimitError,
    AristotleRequestClient,
)
from aristotlelib.ci.manifest import Kind, Manifest, RequestedProperty
from aristotlelib.ci.record import RunRecord
from aristotlelib.ci.scope import ScopeResult
from aristotlelib.declaration import Declaration
from aristotlelib.event import EventType
from aristotlelib.local_file_utils import archive_name
from aristotlelib.printable import escape_controls
from aristotlelib.project import AgentQuestionsSetting, Project, create_sent
from aristotlelib.verilog import bundle as verilog_bundle

logger = logging.getLogger("aristotle")

POLL_INTERVAL_SECONDS = 20

INTERRUPT_CANCEL_SECONDS = 5
"""How long a cancel waits for Aristotle's answer, after an interrupt or when the time budget runs out; GitHub follows
SIGINT with SIGTERM 7.5 s later."""

CANCEL_CHECK_SECONDS = 2
"""Longest the refresh may take that checks whether a cancel Aristotle didn't answer in time took effect."""

INTERRUPTED_CREATE_SECONDS = 10
"""How long an interrupt during a create that was sent looks for the project the create may have made."""

INTERRUPTED_CREATE_LOOKUP_SECONDS = 1
"""Time between two lookups for the project an interrupted create may have made."""

HEARTBEAT_SECONDS = 300
"""Log the state at least this often even when unchanged; GitLab aborts a job that is silent for an hour."""

SLOT_RETRY_SECONDS = 30
"""First wait before resubmitting a refused run when none of the account's projects in progress is listed."""

MAX_SLOT_RETRY_SECONDS = 600
"""Cap on the no-listing retry delay as it doubles from SLOT_RETRY_SECONDS; jitter adds up to half again."""

NAME_TIMEOUT_SECONDS = 10
"""Longest naming a new project may take before the run goes ahead under the name Aristotle gave it."""

PROJECTS_SEARCHED_FOR_A_LOST_CREATE = 100
"""How many of the newest projects are searched for the one a create whose response was lost may have made."""

LOST_CREATE_LOOKUPS = 3
"""How many times they are searched, POLL_INTERVAL_SECONDS apart; a create that timed out may still be running."""

STARTED_STATUSES = [status for status in TaskStatus if status is not TaskStatus.UNKNOWN]
"""A run's statuses once Aristotle has queued it; the run of a create that failed on the server is never queued."""

LANGUAGE_ACCESS_ENDPOINTS = {
    "Verilog": "/user/has-hardware-verification-access",
    "C": "/user/has-software-verification-access",
}
"""Where Aristotle says whether this account may verify a gated language; it says nothing of Rust."""

LANGUAGE_CHECK_SECONDS = 10
"""Longest language_access may take, after which every language it was asked about counts as unknown."""

MAX_FOCUS_FILES_IN_PROMPT = 100

FOCUS_MANIFEST = ".aristotle-focus.txt"
"""Added at the top of the upload: every file under verification, one path per line."""


class RunnerError(Exception):
    """Raised when a run could not be completed for reasons unrelated to the code."""


class SlotTimeoutError(RunnerError):
    """Raised when ``resubmit_by`` passes while Aristotle still refuses the run for too many in progress or queued."""


class UnusableUploadError(RunnerError):
    """Raised when the checkout gives no upload Aristotle can take: nothing to verify in it, or too large an archive."""


@dataclasses.dataclass(frozen=True)
class Upload:
    """What build_tarball archived, and what it left out."""

    uploaded: list[Path]
    """Repository files in the tarball, not counting FOCUS_MANIFEST."""
    excluded: list[Path]
    """Credential files, by local_file_utils.is_secret_path: named like one, or in a folder that holds them."""


def build_tarball(
    root: Path,
    files: list[Path],
    destination: Path,
    focus_files: list[Path] | None,
    block: verilog_bundle.Bundle | None = None,
    allowed: Collection[str] = (),
) -> Upload:
    """Archive the regular ``files`` under ``root``, plus FOCUS_MANIFEST listing the uploaded ``focus_files`` if given,
    and ``block``'s files, tracked, an ignore rule's or not, or at the ``allowed`` paths in the upload, which
    --allow-untracked lets through, with its ``aristotle.f``, generated or as committed.

    A symlink goes in as its target's content only when the target is itself archived, as ``aristotle submit`` does.

    Raises:
        verilog_bundle.BundleError: a file of ``block`` changed after it was read.
        local_file_utils.FileChangedError: a file was replaced while it was being archived.
    """
    excluded: list[Path] = []
    uploaded: list[Path] = []
    archived: set[tuple[int, int]] = set()
    symlinks: list[Path] = []
    skipped_symlinks: dict[Path, str] = {}
    not_files: list[Path] = []
    members = {m.archive: m for m in block.members} if block is not None else {}
    generated = block.filelist_text if block is not None else None
    filelist_name = block.filelist_name if block is not None else verilog_bundle.FILELIST_NAME
    if block is not None and generated is None and block.committed is not None:
        # As a block file, so that a committed link to a file an ignore rule holds back still goes up.
        members.setdefault(filelist_name, verilog_bundle.Member(filelist_name, block.committed, ""))
    held_back: list[str] = []
    # In UTF-8, as Aristotle reads it, whatever the locale's encoding; with dereference, no file goes in as a hard link.
    with tarfile.open(destination, "w:gz", encoding="utf-8", dereference=True) as tar:
        for rel_path in files:
            if generated is not None and rel_path.as_posix() == filelist_name:
                continue
            if (member := members.pop(rel_path.as_posix(), None)) is not None:
                verilog_bundle.write_member(tar, block, member, archived)
                uploaded.append(rel_path)
                continue
            if local_file_utils.is_secret_path(rel_path, root):
                excluded.append(rel_path)
                continue
            if focus_files is not None and rel_path.as_posix() == FOCUS_MANIFEST:
                logger.warning(f"Not uploading the tracked {FOCUS_MANIFEST}: the list of files to verify goes there.")
                continue
            absolute = root / rel_path
            try:
                file_stat = os.lstat(absolute)
            except OSError:
                not_files.append(rel_path)
                continue
            if stat.S_ISLNK(file_stat.st_mode):
                symlinks.append(rel_path)
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                # A submodule's directory, for one.
                not_files.append(rel_path)
                continue
            local_file_utils.add_file(tar, absolute, archive_name(rel_path), file_stat)
            uploaded.append(rel_path)
            if (identity := local_file_utils.file_identity(file_stat)) is not None:
                archived.add(identity)

        # The block's files ``files`` leaves out: tracked ones held back by an ignore rule or behind a linked folder,
        # and those --allow-untracked lets through.
        for member in members.values():
            verilog_bundle.write_member(tar, block, member, archived)
            uploaded.append(Path(member.archive))
            if member.archive not in allowed:
                held_back.append(member.archive)

        # After the loop, so that every file a link may point to has been archived.
        for rel_path in symlinks:
            try:
                local_file_utils.add_symlinked_file(tar, root / rel_path, archive_name(rel_path), archived)
            except local_file_utils.SkippedSymlinkError as e:
                skipped_symlinks[rel_path] = str(e)
                continue
            uploaded.append(rel_path)

        # Last, so that it names only the -I folders that hold an uploaded file.
        if block is not None and generated is not None:
            block.filelist_text = block.generate({p.as_posix() for p in uploaded})
            verilog_bundle.write_text(tar, filelist_name, block.filelist_text)
            uploaded.append(Path(filelist_name))

        if focus_files is not None:
            in_upload = set(uploaded)
            focus = "".join(f"{archive_name(p)}\n" for p in focus_files if p in in_upload).encode()
            info = tarfile.TarInfo(FOCUS_MANIFEST)
            info.size = len(focus)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(focus))

    if held_back:
        logger.info(
            f"Uploading {len(held_back)} tracked {'file' if len(held_back) == 1 else 'files'} the checkout's upload "
            "leaves out, held back by an ignore rule or reached through a linked folder, since the Verilog block "
            f"needs {'it' if len(held_back) == 1 else 'them'}: {', '.join(sorted(held_back))}"
        )
    if skipped_symlinks:
        logger.warning(local_file_utils.skipped_symlinks_warning(skipped_symlinks))
    if not_files:
        logger.warning(
            f"Not uploading {len(not_files)} tracked path(s) that are not files in the checkout "
            f"(a submodule, or a file missing from the checkout):{local_file_utils.listing(not_files)}"
        )
    return Upload(uploaded=uploaded, excluded=excluded)


def log_filelist(scope_result: ScopeResult) -> None:
    """Log where the Verilog build configuration comes from: the filelists the upload holds, else the sources."""
    # A block's reading logs its filelist, which always goes up, generated or as committed.
    if not scope_result.has_verilog_focus or scope_result.block is not None:
        return
    if (canonical := scope_result.canonical_filelist) is not None:
        logger.info(f"Using the Verilog filelist committed at {canonical.as_posix()}")
    elif scope_result.filelists:
        listed = ", ".join(p.as_posix() for p in scope_result.filelists)
        logger.info(f"Found candidate Verilog filelists: {listed}. Aristotle will use one if it covers the design.")
    else:
        logger.info("No Verilog filelist is uploaded. Aristotle will infer the build configuration from the sources.")


def describe_upload(file_count: int, size_bytes: int) -> str:
    """The file count and archive size, e.g. ``1 file (412 bytes)`` or ``30 files (2.4 MB)``."""
    if size_bytes < 1024:
        size = f"{size_bytes} bytes"
    elif round(size_bytes / 1024, 1) < 1024:
        size = f"{size_bytes / 1024:.1f} KB"
    else:
        size = f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{file_count} {'file' if file_count == 1 else 'files'} ({size})"


def _filelist_instruction(scope_result: ScopeResult) -> str:
    """Prompt text naming a committed Verilog filelist, and a block's black boxes, or empty when there is none."""
    if scope_result.block is not None:
        # It goes up, generated or as committed, even when an ignore rule matches it.
        return f"\n\n{verilog_bundle.prompt_lines(scope_result.block)}\n"
    filelists = scope_result.filelists
    if not filelists:
        return ""

    canonical = scope_result.canonical_filelist
    if canonical is not None:
        return f"\n\nUse {canonical.as_posix()} as the commands file.\n"

    listed = "\n".join(f"- {escape_controls(archive_name(p))}" for p in filelists)
    return (
        "\n\nThe repository contains what appear to be Verilog filelists:\n"
        f"{listed}\n"
        "If one of them covers the files under verification, use it as the commands file "
        "so that include directories, defines, and library paths are honored.\n"
    )


def build_prompt(scope_result: ScopeResult, extra_instructions: str | None = None) -> str:
    """Compose the instruction sent to Aristotle for a CI run."""
    focus = scope_result.focus_files
    shown = focus[:MAX_FOCUS_FILES_IN_PROMPT]
    file_list = "\n".join(f"- {escape_controls(archive_name(p))}" for p in shown)
    if len(focus) > len(shown):
        file_list += f"\n- ...and {len(focus) - len(shown)} more files"

    prompt = (
        "This is an automated verification run in a continuous integration pipeline.\n\n"
        "Verify the following files:\n"
        f"{file_list}\n\n"
        f"The complete list is also in {FOCUS_MANIFEST} at the top of the upload, one path per line.\n\n"
        "For each top-level module or function in scope, identify the properties that should "
        "hold of it, register each one with add_spec_declaration, and prove it. Give every "
        "property a description in the terms of the original source language, so that an "
        "engineer who does not read formal proofs can tell what was checked. If a property "
        "does not hold, record a counterexample rather than weakening the statement.\n\n"
        "The rest of the repository is provided for context. Do not modify the source files "
        "under verification."
        f"{_filelist_instruction(scope_result)}"
    )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions from the project:\n{extra_instructions}"
    return prompt


#: How the prompt states each kind of manifest property.
_REQUESTED_STATEMENTS = {
    Kind.NATURAL_LANGUAGE: "{}",
    Kind.SVA: "The SystemVerilog assertion {} holds.",
    Kind.C_FUNCTION_NO_UB: "The C function {} has no undefined behavior.",
}


def _requested_property(requested: RequestedProperty) -> str:
    statement = _REQUESTED_STATEMENTS[requested.kind].format(requested.statement)
    listed = f"- id: {requested.id}\n  Property: " + statement.replace("\n", "\n    ")
    if requested.assumptions:
        listed += "\n  Assumptions: " + requested.assumptions.replace("\n", "\n    ")
    return listed


def build_manifest_prompt(manifest: Manifest, scope_result: ScopeResult, root: Path | None = None) -> str:
    """Compose the instruction for a CI run that checks the manifest's properties, each under its own id, naming the
    manifest by its path under ``root`` when it is there, else by its file name."""
    listed = "\n".join(_requested_property(p) for p in manifest.properties)
    try:
        named = manifest.path.resolve().relative_to(root.resolve()).as_posix() if root else manifest.path.name
    except ValueError:
        named = manifest.path.name
    return (
        "This is an automated verification run in a continuous integration pipeline. "
        "Check each of the following properties of this repository.\n\n"
        f"{listed}\n\n"
        'Register each one with add_spec_declaration as a property with source "user", using its id above, exactly '
        "as written, as the declaration_id. Then prove it, or show that it does not hold. A property's assumptions, "
        "where it lists any, settle questions the code doesn't: check the property under them. Do not rename, merge or "
        "split these properties, and do not weaken one to make it provable: if a property does not hold, record a "
        "counterexample. Give each a description in the terms of the original source language, so that an "
        "engineer who does not read formal proofs can tell what was checked.\n\n"
        'You may also register other properties that you find should hold, with source "agent" and ids not listed '
        "above. They are reported as suggestions and do not affect the result.\n\n"
        "Nobody can reply to this run, so don't ask for a reply. If a property needs a decision only the user can "
        f"make, state the question in your summary and say that the answer goes in {named}, in the property's "
        "statement or its assumptions, or in the code it concerns, before the next run.\n\n"
        "The rest of the repository is provided for context. Do not modify its source files."
        f"{_filelist_instruction(scope_result)}"
    )


@dataclasses.dataclass(frozen=True)
class Package:
    """An archived upload, checked, with the instructions for its run."""

    summary: str
    """What goes up and what the run does with it, as ``2 files (543 bytes); checking 3 properties``."""
    prompt: str
    record: RunRecord | None = None
    """What a manifest run checks, which its prompt ends with; None for a run without a manifest."""


def archive(root: Path, scope_result: ScopeResult, tarball_path: Path, focus_files: list[Path] | None = None) -> Upload:
    """build_tarball() of ``scope_result``'s upload and block.

    Raises:
        RunnerError: a file couldn't be archived, as when one of the block's changed or went away after it was read.
    """
    try:
        return build_tarball(
            root, scope_result.upload_files, tarball_path, focus_files, scope_result.block, scope_result.allowed
        )
    except verilog_bundle.BundleError as e:
        raise RunnerError(str(e)) from e
    except OSError as e:
        raise RunnerError(local_file_utils.archive_error(e)) from e


def package(
    root: Path,
    scope_result: ScopeResult,
    tarball_path: Path,
    manifest: Manifest | None = None,
    extra_instructions: str | None = None,
) -> Package:
    """Archive the upload to ``tarball_path`` and compose the run's instructions, as submit does; with ``manifest``,
    they end with the run's record.

    Raise UnusableUploadError if nothing to verify could be archived, or the archive is too large; RunnerError if a
    file couldn't be archived, as when one of the block's changed or went away after it was read.
    """
    upload = archive(root, scope_result, tarball_path, None if manifest is not None else scope_result.focus_files)
    in_upload = set(upload.uploaded)
    # Only the filelists the upload holds, as one left out, such as a broken link, can't be used.
    scope_result = dataclasses.replace(scope_result, filelists=[p for p in scope_result.filelists if p in in_upload])
    log_filelist(scope_result)
    local_file_utils.log_excluded_secrets(upload.excluded)
    size = tarball_path.stat().st_size
    uploading = describe_upload(len(upload.uploaded), size)

    record = None
    if manifest is not None:
        if not upload.uploaded:
            raise UnusableUploadError("No file could be uploaded; the warnings above say why.")
        doing = f"checking {len(manifest.properties)} {'property' if len(manifest.properties) == 1 else 'properties'}"
        # From the archive, as a file changed since it was archived would make a record of the checkout describe code
        # the run never checks.
        record = RunRecord.of_upload(root, tarball_path, upload.uploaded, manifest)
        prompt = f"{build_manifest_prompt(manifest, scope_result, root)}\n\n{record.prompt_ending}"
    else:
        focus = [p for p in scope_result.focus_files if p in in_upload]
        if not focus:
            raise UnusableUploadError("None of the files to verify could be uploaded; the warnings above say why.")
        doing = f"verifying {len(focus)}"
        prompt = build_prompt(dataclasses.replace(scope_result, focus_files=focus), extra_instructions)

    check_size(root, len(upload.uploaded), size)
    return Package(summary=f"{uploading}; {doing}", prompt=prompt, record=record)


def check_size(root: Path, file_count: int, size: int) -> None:
    """Raise UnusableUploadError, naming ``root``, if an archive of ``size`` bytes is over MAX_FILE_SIZE_BYTES."""
    limit = local_file_utils.MAX_FILE_SIZE_BYTES
    if size > limit:
        raise UnusableUploadError(
            f"The upload of {root} is too large: {describe_upload(file_count, size)} compressed ({size} bytes), and "
            f"Aristotle takes at most {limit / (1024 * 1024):.0f} MB ({limit} bytes). A tracked file that a "
            ".gitignore rule matches stays out of the upload, so add one for the large files Aristotle doesn't need, "
            "or set --directory to the part of the repository to verify."
        )


async def submit(
    root: Path,
    scope_result: ScopeResult,
    tarball_path: Path,
    resubmit_by: float,
    extra_instructions: str | None = None,
    manifest: Manifest | None = None,
    name: str | None = None,
    cancel_on_interrupt: bool = True,
) -> Project:
    """Package the repository and create a project, waiting for a run slot until ``resubmit_by`` (monotonic).

    With ``manifest`` the run checks its properties and no FOCUS_MANIFEST is uploaded; otherwise it verifies the
    scope's focus files. With ``name``, the project is then renamed, as name_project does; an interrupt meanwhile
    cancels the run unless ``cancel_on_interrupt`` is false, as one during the wait does, and so does one during the
    create, as interrupted_create says. A project whose create response was lost is looked up by the name the server
    gives it, after its upload. Raise what package raises, SlotTimeoutError if no slot freed up in time, and
    AristotleOutcomeUnknownError if a lost create's project is not found, or several are.
    """
    packaged = package(root, scope_result, tarball_path, manifest, extra_instructions)
    logger.info(f"Uploading {packaged.summary}")
    prompt = packaged.prompt

    upload_name = new_upload_name(root)

    async def create() -> Project:
        return await Project.create(
            prompt=prompt,
            tar_file_path=tarball_path,
            public_file_path=f"{upload_name}.tar.gz",
            # A CI run has nobody to answer questions.
            agent_questions_setting=AgentQuestionsSetting.DISABLED,
        )

    try:
        try:
            project = await _create_in_a_free_slot(create, resubmit_by)
        except AristotleOutcomeUnknownError as e:
            project = await _find_created(upload_name, e)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await interrupted_create(upload_name, cancel_on_interrupt)
        raise
    if name is not None:
        try:
            await name_project(project, name)
        except (asyncio.CancelledError, KeyboardInterrupt):
            await _interrupted_before_the_wait(project, cancel_on_interrupt)
            raise
    return project


def new_upload_name(root: Path) -> str:
    """A name for the archive, which the server names the project after: ``root``'s, in ASCII, and a random suffix."""
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '-', root.name).strip('-') or 'project'} {secrets.token_hex(6)}"


async def interrupted_create(upload_name: str, cancel: bool) -> None:
    """Say what an interrupt during a create left: nothing if the create wasn't sent, else the project uploaded as
    ``upload_name``, looked for for up to INTERRUPTED_CREATE_SECONDS and, if ``cancel``, once its run starts, cancelled
    as an interrupt during the wait cancels it. A second interrupt ends the search."""
    if not create_sent.get():
        logger.warning("Interrupted before the run was submitted: no project was created.")
        return
    logger.warning(
        f"Interrupted while creating the project; looking for it by the name it was uploaded under, {upload_name}."
    )
    with _second_interrupt_cancels():
        project, failure = await _look_for_interrupted(upload_name, until_started=cancel)
        interrupted = isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError))
        if project is None:
            if interrupted:
                reason = "Interrupted again before the project was found."
            elif isinstance(failure, asyncio.TimeoutError):
                reason = f"Your projects could not be listed to find it within {INTERRUPTED_CREATE_SECONDS} s."
            elif failure is not None:
                text = (str(failure) or type(failure).__name__).rstrip(".")
                reason = f"Your projects could not be listed to find it: {text}."
            else:
                reason = (
                    f"None of your newest projects is named {upload_name} after {INTERRUPTED_CREATE_SECONDS} s, so "
                    "Aristotle either did not create it or has not yet."
                )
            logger.warning(f"{reason} Look for it in `aristotle list`, and cancel it with `aristotle cancel <id>`.")
        elif not cancel:
            logger.warning(f"Found it; the run carries on: {project.web_url()}")
        elif project.latest_run_status not in STARTED_STATUSES:
            when = "when interrupted again" if interrupted else f"after {INTERRUPTED_CREATE_SECONDS} s"
            logger.warning(
                f"Found it, but its run hadn't started {when}, so it couldn't be cancelled. Once it starts, cancel it "
                f"with:\n  aristotle cancel {project.project_id}"
            )
        else:
            logger.warning(f"Found it; cancelling the run: {project.web_url()}")
            await _cancel_current_run(project)


async def _look_for_interrupted(upload_name: str, until_started: bool) -> tuple[Project | None, BaseException | None]:
    """The one project named ``upload_name`` among the newest PROJECTS_SEARCHED_FOR_A_LOST_CREATE, looked for every
    INTERRUPTED_CREATE_LOOKUP_SECONDS until it is listed, or with ``until_started`` until its run has started, for up
    to INTERRUPTED_CREATE_SECONDS: the project as last listed, or None, and the second interrupt that ended the search,
    or the last lookup's failure while none has got through."""
    deadline = time.monotonic() + INTERRUPTED_CREATE_SECONDS
    project: Project | None = None
    failure: BaseException | None = None
    listed = False
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            projects, _ = await asyncio.wait_for(
                Project.list_projects(limit=PROJECTS_SEARCHED_FOR_A_LOST_CREATE), remaining
            )
        except (KeyboardInterrupt, asyncio.CancelledError) as e:
            return project, _handled(e)
        except Exception as e:
            failure = None if listed else e
        else:
            listed, failure = True, None
            named = [p for p in projects if p.description == upload_name]
            project = named[0] if len(named) == 1 else None
            if project is not None and (not until_started or project.latest_run_status in STARTED_STATUSES):
                return project, None
        try:
            await asyncio.sleep(min(INTERRUPTED_CREATE_LOOKUP_SECONDS, max(deadline - time.monotonic(), 0)))
        except (KeyboardInterrupt, asyncio.CancelledError) as e:
            return project, _handled(e)
    return project, failure


async def _find_created(upload_name: str, error: AristotleOutcomeUnknownError) -> Project:
    """The one project named ``upload_name`` among the newest PROJECTS_SEARCHED_FOR_A_LOST_CREATE whose run has
    started, looked for up to LOST_CREATE_LOOKUPS times, POLL_INTERVAL_SECONDS apart.

    Raises:
        AristotleOutcomeUnknownError: no such project, or several, or the projects could not be listed.
    """
    # The error as the request raised it: submit's advice to check `aristotle list` is moot, as this looks it up.
    reason = error.__cause__ if isinstance(error.__cause__, AristotleOutcomeUnknownError) else error
    logger.warning(f"{reason} Looking for the project by the name it was uploaded under, {upload_name}.")
    for lookup in range(LOST_CREATE_LOOKUPS):
        if lookup:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        matches = await _named(upload_name, error, latest_run_status=STARTED_STATUSES)
        if len(matches) == 1:
            logger.info(f"Found it, so following {matches[0].project_id}.")
            return matches[0]
        if matches:
            raise AristotleOutcomeUnknownError(
                f"{len(matches)} of your projects are named {upload_name}. Check `aristotle list` before running "
                "this again.",
                error.status_code,
            )
    if unstarted := await _named(upload_name, error):
        raise AristotleOutcomeUnknownError(
            f"Project {unstarted[0].project_id} is named {upload_name}, but its run hasn't started, so there is "
            "nothing to follow yet. Check `aristotle list` before running this again.",
            error.status_code,
        )
    raise AristotleOutcomeUnknownError(
        f"None of your newest projects is named {upload_name}, so Aristotle either did not create it or has not yet. "
        "Check `aristotle list` before running this again.",
        error.status_code,
    )


async def _named(upload_name: str, error: AristotleOutcomeUnknownError, **filters: Any) -> list[Project]:
    """The newest PROJECTS_SEARCHED_FOR_A_LOST_CREATE projects, filtered as list_projects is, named ``upload_name``.

    Raises:
        AristotleOutcomeUnknownError: the projects could not be listed.
    """
    try:
        projects, _ = await Project.list_projects(limit=PROJECTS_SEARCHED_FOR_A_LOST_CREATE, **filters)
    except AristotleAPIError as e:
        raise AristotleOutcomeUnknownError(
            f"Your projects could not be listed to find it: {e} Check `aristotle list` before running this again.",
            error.status_code,
        ) from e
    return [p for p in projects if p.description == upload_name]


async def name_project(project: Project, name: str) -> None:
    """Rename ``project`` to ``name``, logging rather than raising when that fails or exceeds NAME_TIMEOUT_SECONDS."""
    try:
        await asyncio.wait_for(project.rename(name), NAME_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        reason = f"no answer in {NAME_TIMEOUT_SECONDS} s"
    except Exception as e:
        # The run exists by now, so no error here may stop the command from following it.
        reason = str(e) or type(e).__name__
        logger.debug("Naming the project failed", exc_info=True)
    else:
        logger.info(f"Project name: {project.description}")
        return
    logger.warning(f'Could not name the project "{name}", so it keeps the name "{project.description}": {reason}')


async def _projects_in_progress() -> list[Project]:
    projects, _ = await Project.list_projects(limit=100, latest_run_status=[TaskStatus.QUEUED, TaskStatus.IN_PROGRESS])
    return projects


def _list_urls(projects: list[Project]) -> str:
    return "".join(f"\n  {p.web_url()}" for p in projects[:5]) + ("\n  ..." if len(projects) > 5 else "")


async def _create_in_a_free_slot(create: Callable[[], Awaitable[Project]], resubmit_by: float) -> Project:
    """Call ``create``; while it is refused for too many runs in progress or queued, wait for one to finish and retry.

    Each try uploads the archive again. The next is made once fewer projects are listed than at the most since the
    refusal; while none is listed, after a jittered backoff of SLOT_RETRY_SECONDS doubling to MAX_SLOT_RETRY_SECONDS;
    and once more at ``resubmit_by``, after which a refusal raises SlotTimeoutError.
    """
    retry_seconds = SLOT_RETRY_SECONDS
    while True:
        try:
            return await create()
        except AristotleProjectLimitError as e:
            refusal = str(e)
        # Refused before Aristotle made a project, so an interrupt from here on has none to look for.
        create_sent.set(False)
        if time.monotonic() >= resubmit_by:
            message = (
                f"Aristotle refused the run: {refusal} No room freed up in time to leave a run enough of the time "
                "budget, so nothing was verified."
            )
            try:
                in_progress = await _projects_in_progress()
            except AristotleAPIError:
                raise SlotTimeoutError(message) from None
            if not in_progress:
                message += (
                    " This key lists none of your projects in progress or queued; ones in another organization "
                    "count too."
                )
                raise SlotTimeoutError(message)
            raise SlotTimeoutError(f"{message} Your projects in progress or queued:{_list_urls(in_progress)}")
        listing_error: AristotleAPIError | None = None
        try:
            in_progress = await _projects_in_progress()
        except AristotleAPIError as e:
            if not is_transient(e):
                raise
            # The wait's own listings poll past the failure, as they do past a later one.
            in_progress, listing_error = [], e
        retry_at = min(time.monotonic() + random.uniform(1.0, 1.5) * retry_seconds, resubmit_by)
        if in_progress:
            retry_seconds = SLOT_RETRY_SECONDS
            logger.warning(
                f"Waiting to submit: Aristotle refused the run: {refusal}\n"
                f"Your projects in progress or queued ({len(in_progress)} now):{_list_urls(in_progress)}"
            )
        elif listing_error is not None:
            logger.warning(
                f"Waiting to submit: Aristotle refused the run: {refusal}\n"
                f"Your projects in progress or queued could not be listed: {listing_error}"
            )
        else:
            retry_seconds = min(retry_seconds * 2, MAX_SLOT_RETRY_SECONDS)
            logger.warning(
                f"Waiting to submit: Aristotle refused the run: {refusal}\n"
                "This key lists none of your projects in progress or queued (one may be in another organization, "
                f"or have just finished). Trying again in {retry_at - time.monotonic():.0f} s."
            )
        await _wait_for_a_slot(in_progress, retry_at, resubmit_by)


async def _wait_for_a_slot(in_progress: list[Project], retry_at: float, resubmit_by: float) -> None:
    """Return once fewer projects are listed than at the most since the refusal, at ``retry_at`` if none is listed,
    or at ``resubmit_by``. A project that leaves as another starts in its place frees no slot, so it costs no upload."""
    listed = {p.project_id for p in in_progress}
    most = len(listed)
    waiting_since = last_logged_at = time.monotonic()
    while True:
        remaining = resubmit_by - time.monotonic()
        if remaining <= 0:
            logger.info("Submitting once more before giving up.")
            return
        await asyncio.sleep(min(random.uniform(0.75, 1.25) * POLL_INTERVAL_SECONDS, remaining))
        try:
            ids = {p.project_id for p in await _projects_in_progress()}
        except AristotleAPIError as e:
            if not is_transient(e):
                raise
            logger.warning(f"Could not list your projects in progress, retrying: {e}")
            continue

        if len(ids) < most:
            logger.info("A project finished. Submitting again.")
            return
        if not ids and time.monotonic() >= retry_at:
            logger.info("Submitting again.")
            return
        now = time.monotonic()
        if ids != listed or now - last_logged_at >= HEARTBEAT_SECONDS:
            waited = int(now - waiting_since)
            logger.info(
                f"[{waited // 60:02d}:{waited % 60:02d}] Still waiting to submit: "
                f"{len(ids)} of your projects in progress or queued."
            )
            last_logged_at = now
        listed, most = ids, max(most, len(ids))


async def _current_task(project: Project) -> AgentTask:
    tasks, _ = await project.get_tasks(limit=1)
    if not tasks:
        raise RunnerError(f"Project {project.project_id} was created but has no task to wait on.")
    return tasks[0]


async def wait_for_completion(
    project: Project, timeout_seconds: float, cancel_on_timeout: bool = True
) -> TaskStatus | None:
    """Poll until the run reaches a terminal state and return it; on running out of budget, return QUEUED if the run
    never started, else None, unless the cancel finds the run has finished, which returns its status."""
    try:
        task = await _current_task(project)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _interrupted_before_the_wait(project, cancel_on_timeout)
        raise
    try:
        return await _poll(task, timeout_seconds, cancel_on_timeout)
    except (asyncio.CancelledError, KeyboardInterrupt):
        with _second_interrupt_cancels():
            # A cancelled CI job would otherwise leave its run consuming budget, so the cancel goes first.
            if cancel_on_timeout:
                logger.warning("Interrupted; cancelling the run.")
                await _cancel_after_an_interrupt(task)
            elif await _confirmed_queued(task):
                logger.warning("Interrupted before the run started: it is still queued, and will start later.")
            else:
                logger.warning(f"Interrupted; the run carries on: {project.web_url()}")
        raise


async def _interrupted_before_the_wait(project: Project, cancel: bool) -> None:
    """Log the run's URL and, if ``cancel``, cancel its task as _cancel_current_run does."""
    if not cancel:
        logger.warning(f"Interrupted; the run carries on: {project.web_url()}")
        return
    logger.warning(f"Interrupted; cancelling the run: {project.web_url()}")
    with _second_interrupt_cancels():
        await _cancel_current_run(project)


@contextlib.contextmanager
def _second_interrupt_cancels() -> Generator[None, None, None]:
    """While open, in the main thread, SIGINT and SIGTERM cancel the running task, so a second interrupt reaches the
    code handling the first as a CancelledError wherever the loop waits, asyncio.run's shutdown included; the previous
    handlers come back at its end."""
    task = asyncio.current_task()
    if task is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()

    def cancel(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(task.cancel)

    previous = [(sig, signal.signal(sig, cancel)) for sig in (signal.SIGINT, signal.SIGTERM)]
    try:
        yield
    finally:
        for sig, handler in previous:
            if handler is not None:
                signal.signal(sig, handler)


def _handled(interrupt: BaseException) -> BaseException:
    """``interrupt``, a second interrupt, once its CancelledError is taken back, on Python 3.11+, from the running task,
    so that asyncio.run still ends as the first interrupt does."""
    task = asyncio.current_task()
    if isinstance(interrupt, asyncio.CancelledError) and task is not None and sys.version_info >= (3, 11):
        task.uncancel()
    return interrupt


async def _cancel_current_run(project: Project) -> None:
    """Cancel ``project``'s current task as _cancel_after_an_interrupt does, finding it within the same
    INTERRUPT_CANCEL_SECONDS."""
    deadline = time.monotonic() + INTERRUPT_CANCEL_SECONDS
    try:
        task = await asyncio.wait_for(_current_task(project), INTERRUPT_CANCEL_SECONDS)
    except Exception as e:
        _could_not_cancel(project.project_id, e)
        return
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        _could_not_cancel(project.project_id, _handled(e))
        return
    await _cancel_after_an_interrupt(task, max(deadline - time.monotonic(), 0))


async def _cancel_after_an_interrupt(task: AgentTask, seconds: float | None = None) -> None:
    """Cancel ``task`` as _cancel does and log the outcome; a second interrupt ends the wait for Aristotle's answer, and
    the run is then checked once as for an unanswered cancel, a third ending that check."""
    try:
        failure: BaseException | None = await _cancel(task, seconds)
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        failure = _handled(e)
        if task.status not in TERMINAL_STATUSES and await _refreshed(task, interrupted=True):
            failure = None if task.status == TaskStatus.CANCELED else failure
    _log_cancel(task, failure)


async def _cancel(task: AgentTask, seconds: float | None = None) -> Exception | None:
    """Cancel ``task``, waiting ``seconds``, by default INTERRUPT_CANCEL_SECONDS, for Aristotle's answer; without one,
    refresh it within CANCEL_CHECK_SECONDS, as Aristotle marks a run CANCELED before it answers. None if the run reads
    CANCELED, else what kept the cancel from being confirmed."""
    try:
        await asyncio.wait_for(task.cancel(), INTERRUPT_CANCEL_SECONDS if seconds is None else seconds)
    except Exception as e:
        failure = e
    else:
        return None
    # A refused cancel has already refreshed the task.
    if task.status not in TERMINAL_STATUSES:
        await _refreshed(task)
    return None if task.status == TaskStatus.CANCELED else failure


async def _refreshed(task: AgentTask, interrupted: bool = False) -> bool:
    """Whether ``task`` was refreshed within CANCEL_CHECK_SECONDS; an interrupt ends the refresh with False once
    ``interrupted``, and otherwise propagates."""
    try:
        await asyncio.wait_for(task.refresh(), CANCEL_CHECK_SECONDS)
    except Exception:
        return False
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        if not interrupted:
            raise
        _handled(e)
        return False
    return True


def _log_cancel(task: AgentTask, failure: BaseException | None) -> None:
    """Log that the run was cancelled, or, as _could_not_cancel does, that it wasn't."""
    if failure is None:
        logger.info("The run was cancelled.")
    else:
        _could_not_cancel(task.project_id, failure, task)


def _could_not_cancel(project_id: str, error: BaseException, task: AgentTask | None = None) -> None:
    """Warn that cancelling the run failed, saying why, as a cancel that ran out of time or was interrupted has no
    message, and how to cancel it by hand; a run that ``task`` shows had already finished is only noted, as nothing was
    left to cancel."""
    if task is not None and task.status in TERMINAL_STATUSES:
        logger.info(f"The run had already finished ({task.status.name}), so there was nothing to cancel.")
        return
    if isinstance(error, asyncio.TimeoutError):
        reason = f"it didn't finish within {INTERRUPT_CANCEL_SECONDS} s"
    elif isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
        reason = "interrupted again before Aristotle answered"
    else:
        reason = (str(error) or type(error).__name__).rstrip(".")
    logger.warning(f"Could not cancel the run: {reason}. Cancel it with:\n  aristotle cancel {project_id}")


async def _confirmed_queued(task: AgentTask) -> bool:
    """Whether a refresh within INTERRUPT_CANCEL_SECONDS shows the run still queued; a run never returns to QUEUED."""
    if task.status != TaskStatus.QUEUED:
        return False
    try:
        await asyncio.wait_for(task.refresh(), INTERRUPT_CANCEL_SECONDS)
    except Exception:
        return False
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        _handled(e)
        return False
    return task.status == TaskStatus.QUEUED


async def _poll(task: AgentTask, timeout_seconds: float, cancel_on_timeout: bool) -> TaskStatus | None:
    deadline = time.monotonic() + timeout_seconds
    # Whether task.status came from the latest refresh, rather than an earlier one before a failed refresh.
    current = True
    last_status: TaskStatus | None = None
    last_logged_at = time.monotonic()

    while True:
        if task.status in TERMINAL_STATUSES:
            logger.info(f"Run finished: {task.status.name}")
            return task.status

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            never_started = current and task.status == TaskStatus.QUEUED
            if never_started:
                logger.warning("The time budget ran out before the run started: it was still queued.")
            else:
                logger.warning("The time budget ran out before the run finished.")
            if cancel_on_timeout:
                logger.info("Cancelling the run. Use --on-timeout leave to let it continue in the background.")
                _log_cancel(task, await _cancel(task))
                # The cancel's refresh saw the run finish after the last poll, so its results stand.
                if task.status in TERMINAL_STATUSES and task.status != TaskStatus.CANCELED:
                    logger.info(f"Run finished: {task.status.name}")
                    return task.status
            return TaskStatus.QUEUED if never_started else None

        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        try:
            await task.refresh()
        except AristotleAPIError as e:
            if not is_transient(e):
                raise
            # One blip must not fail the build and orphan the run; the deadline still bounds the wait.
            logger.warning(f"Could not check the run, retrying: {e}")
            current = False
            continue
        current = True

        now = time.monotonic()
        if task.status != last_status or now - last_logged_at >= HEARTBEAT_SECONDS:
            elapsed = int(timeout_seconds - (deadline - now))
            logger.info(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {task.status.name}")
            last_status = task.status
            last_logged_at = now


def is_transient(error: AristotleAPIError) -> bool:
    return error.status_code is None or error.status_code == 429 or error.status_code >= 500


_STOP_MARKER = re.compile(r"(?:^|\n)## Aristotle stopped before finishing\n")
"""The marker Aristotle appends to the summary of a run it stopped because only the user can make a decision."""

NO_STOPPED_SUMMARY = "Aristotle gave no summary of the run."
"""fetch_stopped_summary's answer for a stopped run whose summary is the marker alone."""


async def fetch_stopped_summary(project: Project) -> str | None:
    """The summary of a run Aristotle stopped for decisions only the user can make, without the marker, else None.

    The summary is the agent's own user-facing text, where it lists the open questions."""
    task = await _current_task(project)
    if task.status != TaskStatus.COMPLETE_WITH_ERRORS or task.output_summary is None:
        return None
    sections = _STOP_MARKER.split(task.output_summary)
    if len(sections) == 1:
        return None
    return "\n".join(sections[:-1]).strip() or NO_STOPPED_SUMMARY


async def fetch_summary(project: Project) -> str | None:
    """The run's summary, the agent's own user-facing text; None when it has none."""
    task = await _current_task(project)
    return (task.output_summary or "").strip() or None


async def fetch_prompt(task: AgentTask) -> str | None:
    """The prompt ``task`` was started with, its first message, recorded with the task; None when it has none."""
    events, _ = await task.get_events(limit=5, newest_first=False)
    return next((event.content for event in events if event.event_type == EventType.MESSAGE), None)


async def language_access(languages: Iterable[str]) -> dict[str, bool | None]:
    """Whether this account may verify each of ``languages``: None where Aristotle can't say, or doesn't in time."""

    async def check(language: str) -> bool | None:
        endpoint = LANGUAGE_ACCESS_ENDPOINTS.get(language)
        if endpoint is None:
            return None
        try:
            async with AristotleRequestClient() as client:
                has_access = (await client.get(endpoint)).json()["has_access"]
        except Exception:
            # Only a log message depends on the answer, so no failure here may stop the command.
            logger.debug(f"Could not check whether {language} is enabled", exc_info=True)
            return None
        return has_access if isinstance(has_access, bool) else None

    asked = list(languages)
    try:
        answers = await asyncio.wait_for(asyncio.gather(*map(check, asked)), LANGUAGE_CHECK_SECONDS)
    except asyncio.TimeoutError:
        return dict.fromkeys(asked)
    return dict(zip(asked, answers))


async def fetch_declarations(project: Project) -> list[Declaration]:
    """Fetch the run's declarations."""
    return await project.get_declarations()
