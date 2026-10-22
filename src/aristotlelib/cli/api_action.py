from __future__ import annotations

import argparse
import asyncio
import codecs
import concurrent.futures
import json
import logging
import os
import signal
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any, NoReturn, ParamSpec, TypeVar

import pydantic

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import (
    AristotleAPIError,
    AristotleNotFoundError,
    AristotleNotProcessedError,
    AristotleOutcomeUnknownError,
    AristotleProjectLimitError,
    _MalformedKeyError,
    api_key_source,
    get_api_key,
    get_web_url,
    set_api_key,
)
from aristotlelib.cli.login_flow import LoginError, login
from aristotlelib.date_utils import as_utc
from aristotlelib.declaration import Declaration, DeclarationType
from aristotlelib.exit_codes import (
    EXIT_AUTH_ERROR,
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_INTERRUPTED,
    EXIT_NOT_FOUND,
    EXIT_OUTCOME_UNKNOWN,
    EXIT_QUESTIONS_PENDING,
    EXIT_RATE_LIMITED,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.printable import escape_logged_text
from aristotlelib.project import (
    PROJECT_ID_PREFIX_MIN_LENGTH,
    AmbiguousProjectError,
    Project,
    ProjectStatus,
    lookup_at_terminal,
)
from aristotlelib.question import Question
from aristotlelib.results import (
    NO_SUMMARY,
    VERDICT_STATUSES,
    NotProcessed,
    OutcomeUnknown,
    PropertySummary,
    QuestionsPending,
    summarize_properties,
)
from aristotlelib.shell import shell_hint

PROJECT_REFERENCE_HELP = f"its id, the first {PROJECT_ID_PREFIX_MIN_LENGTH} or more characters of it, or its name"
"""How a project argument's help describes what it accepts; Project.resolve_id turns it into an id."""
VERBOSE_HELP = "Show response headers and a short response excerpt when a request is refused."

_P = ParamSpec("_P")
_T = TypeVar("_T")

# Longest the CLI's event loop sleeps: where send() on its self-pipe is refused, as by a sandbox that blocks the network
# by system call, another thread can't wake it.
LOOP_WAKE_SECONDS = 0.5


def add_prompt_arguments(parser: argparse.ArgumentParser, help: str) -> None:
    """The prompt as an argument, or with --prompt-file from a file or stdin, for one too long for the command line."""
    parser.add_argument("prompt", nargs="?", help=f"{help}, unless --prompt-file gives it")
    parser.add_argument(
        "--prompt-file",
        metavar="PATH",
        type=os.path.expanduser,
        help=(
            "Read the prompt from PATH, as UTF-8 text, or from a pipe with -. For a prompt too long for the command "
            "line: Linux takes at most 128 KiB in one argument."
        ),
    )


def read_prompt(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """The prompt given as the argument or with --prompt-file; a usage error unless exactly one gives it, readably.

    Commands read it in preflight: before the key is looked for, and outside the event loop, where Ctrl-C ends a
    blocking read at once.
    """
    if args.prompt is not None and args.prompt_file is not None:
        parser.error("give the prompt as an argument or with --prompt-file, not both")
    if args.prompt_file is None:
        if args.prompt is None:
            parser.error("a prompt is required, as an argument or with --prompt-file")
        return args.prompt
    try:
        if args.prompt_file != "-":
            with open(args.prompt_file, "rb") as file:
                data = file.read()
        elif sys.stdin is None:
            parser.error("--prompt-file -: there is no stdin to read")
        elif sys.stdin.isatty():
            # A terminal keeps at most 4095 bytes of a line (1023 on macOS), and drops the rest without a word.
            parser.error("--prompt-file -: stdin is a terminal, which cuts a long line short; pipe the prompt in")
        else:
            data = sys.stdin.buffer.read()
        # Windows PowerShell 5.1's > writes UTF-16 with a BOM; a UTF-8 BOM, as Windows editors write, isn't text either.
        utf16 = data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        text = data.decode("utf-16" if utf16 else "utf-8-sig")
    except OSError as e:
        parser.error(f"--prompt-file: {args.prompt_file} could not be read: {e.strerror or e}")
    except UnicodeDecodeError:
        parser.error(f"--prompt-file: {args.prompt_file} is not UTF-8 text")
    if "\0" in text:
        # UTF-16 without a BOM, or UTF-32, whose BOM starts as UTF-16's does; no argument can hold a NUL.
        parser.error(f"--prompt-file: {args.prompt_file} is not UTF-8 text")
    # As $(cat PATH) would: the newlines a file ends with aren't part of the prompt.
    return text.replace("\r\n", "\n").rstrip("\n")


def print_json(result: pydantic.BaseModel) -> None:
    """Print a result as indented JSON on stdout, under the names --json gives its fields, such as a project's
    ``name``."""
    print(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))


def _waits(non_interactive: bool, json: bool) -> list[str]:
    """The flags that wait as a wait did: --non-interactive, which stops for questions, or --wait, and --json."""
    return ["--non-interactive"] if non_interactive else ["--wait", "--json"] if json else ["--wait"]


def _destination_option(destination: str) -> str:
    """``--destination`` with ``destination``, as one word of a printed command."""
    # On Windows the CLI reads forward slashes as separators too, and they need no quoting.
    path = destination.replace("\\", "/") if os.name == "nt" else destination
    return f"--destination={path}"


def download_command(project_id: str, destination: str | None = None) -> list[str]:
    """The `aristotle download` command that saves the project's files to ``destination``, else into the current
    directory."""
    command = ["aristotle", "download", project_id]
    return command if destination is None else [*command, _destination_option(destination)]


def download_hint(project_id: str, destination: str | None) -> str:
    """``Get them with:`` and the command that saves the project's files to ``destination``, quoted for the shell."""
    return shell_hint("Get them with", download_command(project_id, destination))


def interrupted_save_hint(project_id: str, destination: str | None) -> str:
    """What an interrupt says after a wait saw its task finish and before the files were saved to ``destination``."""
    not_saved = "The project's files were not saved, since the command was interrupted."
    return f"{not_saved}\n{download_hint(project_id, destination)}"


def wait_command(
    project_id: str,
    task_id: str | None,
    destination: str | None = None,
    *,
    non_interactive: bool = True,
    json: bool = False,
) -> list[str]:
    """The `aristotle show` command that carries on a wait for task ``task_id`` of the project, or for its latest task
    without one, saving to ``destination``: with --non-interactive, which stops for questions, or with --wait, and
    --json, as `submit --wait` waits."""
    command = ["aristotle", "show", project_id]
    command += [] if task_id is None else ["--task", task_id]
    command += _waits(non_interactive, json)
    if destination is not None:
        command.append(_destination_option(destination))
    return command


def resume_hint(task: AgentTask, destination: str | None = None) -> str:
    """How to carry on a ``--non-interactive`` wait for ``task``, saving to ``destination``."""
    return shell_hint("then carry on waiting with", wait_command(task.project_id, task.agent_task_id, destination))


def carry_on_hint(task: AgentTask, destination: str | None, *, non_interactive: bool, json: bool) -> str:
    """How to carry on a wait for ``task`` that ends before the task does, the way it was waiting."""
    return shell_hint(
        "Carry on waiting with",
        wait_command(task.project_id, task.agent_task_id, destination, non_interactive=non_interactive, json=json),
    )


def resume_commands(task: AgentTask, destination: str | None, *, non_interactive: bool, json: bool) -> list[list[str]]:
    """The commands that resume ``task``'s project once it has ended badly, waiting as the wait did: `continue` with
    its flags, or, to save to ``destination``, which `continue` doesn't take, `continue` and then `show` with it, which
    follows the task `continue` starts."""
    resume = ["aristotle", "continue", task.project_id, "Continue"]
    if destination is None:
        return [resume + _waits(non_interactive, json)]
    return [resume, wait_command(task.project_id, None, destination, non_interactive=non_interactive, json=json)]


def refused_run(error: AristotleProjectLimitError, advice: str) -> AristotleNotProcessedError:
    """``error``, a refusal for too many runs, which Aristotle makes before it acts on the request, as not processed,
    followed by ``advice``."""
    stop = "" if str(error).endswith((".", "!", "?")) else "."
    return AristotleNotProcessedError(f"{error}{stop} {advice}", error.status_code)


def print_questions_pending(task: AgentTask, questions: list[Question], destination: str | None = None) -> int:
    """Print the questions a ``--non-interactive`` wait stopped for, say how to go on on stderr, and return 10."""
    print_json(QuestionsPending(project_id=task.project_id, task=task, questions=questions))
    logging.info(
        "Aristotle is waiting for answers. Answer with:\n"
        f"  aristotle questions {task.project_id} --task {task.agent_task_id} --answer <question-id>=<answer>\n"
        f"{resume_hint(task, destination)}"
    )
    return EXIT_QUESTIONS_PENDING


async def fetch_properties(project_id: str) -> list[Declaration] | None:
    """The project's properties; None, debug-logged, when they cannot be fetched, so the command still prints."""
    try:
        return await Declaration.list_all_for_project(project_id, declaration_type=DeclarationType.PROPERTY)
    except Exception as e:
        logging.debug(f"Properties unavailable: {e}")
        return None


async def fetch_tasks(
    project_id: str,
    properties: list[Declaration] | None,
    latest: AgentTask | None = None,
    strict: bool = False,
    *,
    every_update: bool = False,
) -> list[AgentTask]:
    """The project's tasks, newest first, back to the one that set the oldest verdict in ``properties``, or with
    ``every_update`` made the oldest update to any of them: what unchecked() judges them by. When none has one, or
    none predates ``latest``, the project's latest task, just ``latest`` if given, else nothing. When the rest can't be
    fetched, those fetched so far, or ``latest``, with a warning that a verdict may then count as checked; ``strict``
    raises instead."""
    verdicts = [as_utc(p.last_updated) for p in properties or [] if every_update or p.status in VERDICT_STATUSES]
    known = [latest] if latest is not None else []
    if not verdicts or (latest is not None and as_utc(latest.created_at) <= min(verdicts)):
        return known
    tasks: list[AgentTask] = []
    try:
        project = await Project.from_id(project_id)
        pagination_key = None
        while True:
            page, pagination_key = await project.get_tasks(pagination_key=pagination_key, limit=100)
            tasks += page
            if pagination_key is None or not page or as_utc(page[-1].created_at) <= min(verdicts):
                return tasks or known
    except Exception as e:
        if strict:
            raise
        logging.warning(
            "WARNING: The project's tasks couldn't all be listed, so a proof or disproof that an earlier task left "
            f"unchecked may count as checked: {e}"
        )
        return tasks or known


async def fetch_properties_and_tasks(
    project_id: str, latest: AgentTask | None, strict: bool = False
) -> tuple[list[Declaration] | None, list[AgentTask]]:
    """fetch_properties, and fetch_tasks for them from ``latest``, the project's latest task; ``strict`` as there."""
    properties = await fetch_properties(project_id)
    return properties, await fetch_tasks(project_id, properties, latest, strict)


async def judge_properties(project_id: str, latest: AgentTask | None) -> tuple[PropertySummary, Exception | None]:
    """A --json result's properties, from ``latest``, the project's latest task, and None; or NO_SUMMARY and the error
    when the project's tasks can't be listed, since no verdict can then be called checked."""
    try:
        properties, tasks = await fetch_properties_and_tasks(project_id, latest, strict=True)
    except Exception as e:
        return NO_SUMMARY, e
    return summarize_properties(properties, tasks), None


def log_unjudged(project_id: str, error: Exception, level: int = logging.ERROR) -> int:
    """Say that the project's properties weren't judged, as judge_properties couldn't list its tasks, and how to get
    them; the exit code for ``error``."""
    logging.log(
        level,
        "The project's tasks couldn't be listed, so `property_counts`, `requested_counts` and `properties` are null: no "
        f"proof or disproof can be told checked or not. {error}\nGet them once Aristotle answers with:\n"
        f"  aristotle show {project_id} --json",
    )
    return exit_code_for(error) if isinstance(error, AristotleAPIError) else EXIT_INFRASTRUCTURE_ERROR


async def project_from_reference(reference: str) -> Project:
    """The project a UUID, a unique id prefix or a name refers to: as Project.resolve_id's lookup listed it, or
    fetched by id for a UUID, which takes no lookup."""
    project_id, listed = await Project._resolve(reference)
    return listed if listed is not None else await Project.from_id(project_id)


def with_latest_run(project: Project, task: AgentTask) -> Project:
    """``project`` with whether a run is going and its latest run's status taken from ``task``, its newest, which a
    wait keeps fresher than the project: for a result a Ctrl-C cuts short, with no time to refresh the project."""
    project.status = ProjectStatus.RUNNING if task.live else ProjectStatus.IDLE
    project.latest_run_status = task.status
    return project


async def refresh_quietly(project: Project, latest: AgentTask) -> None:
    """Refresh ``project`` before a result prints it. When that fails it stays as it last stood, with its run as
    ``latest``, its newest task, has it."""
    with_latest_run(project, latest)
    try:
        await project.refresh()
    except AristotleAPIError as e:
        logging.debug(f"Project refresh failed: {e}")


async def log_download_hint(project_id: str) -> None:
    """Log the command that downloads the project's files, unless the project is known to have none."""
    try:
        has_files = (await Project.from_id(project_id)).has_files
    except AristotleAPIError:
        has_files = True
    if has_files:
        logging.info(f"Get the project's files with:\n  aristotle download {project_id}")


REJECTED_PAGINATION_KEY = "--pagination-key was rejected; pass the key the previous page printed."


def pagination_key_hint(error: AristotleAPIError, key: str | None) -> str | None:
    """What to say of ``key``, the --pagination-key given, for ``error`` from a request that sent it: that Aristotle
    refused it, or that it is the likely cause of a server error, the answer some endpoints still give a key they can't
    read; None without a key, or for any other error."""
    if key is None or error.status_code is None:
        return None
    if error.status_code == 422:
        return REJECTED_PAGINATION_KEY
    if error.status_code >= 500:
        return (
            "A --pagination-key Aristotle can't read gets this answer too: pass the key the previous page printed, or "
            "leave it out for the first page."
        )
    return None


def with_pagination_key_hint(error: AristotleAPIError, key: str | None) -> str:
    """``error``'s message, followed by what to say of ``key`` if anything, as pagination_key_hint() gives it."""
    hint = pagination_key_hint(error, key)
    if hint is None:
        return str(error)
    stop = "" if str(error).endswith((".", "!", "?")) else "."
    return f"{error}{stop} {hint}"


def raise_for_pagination_key(parser: argparse.ArgumentParser, error: AristotleAPIError, key: str | None) -> NoReturn:
    """Raise ``error``, from a request that sent ``key``: as a usage error when Aristotle refused the key, and naming
    the key as the likely cause of a server error."""
    hint = pagination_key_hint(error, key)
    if hint == REJECTED_PAGINATION_KEY:
        parser.error(hint)
    if hint is not None:
        raise AristotleAPIError(with_pagination_key_hint(error, key), error.status_code) from error
    raise error


def task_exit_code(status: TaskStatus) -> int:
    """Exit code for a waited-on task: 0 complete, 1 finished incomplete, 3 failed, cancelled or still running."""
    if status == TaskStatus.COMPLETE:
        return EXIT_SUCCESS
    if status in (TaskStatus.COMPLETE_WITH_ERRORS, TaskStatus.OUT_OF_BUDGET):
        return EXIT_VERIFICATION_FAILED
    return EXIT_INFRASTRUCTURE_ERROR


class _DaemonThreadExecutor(concurrent.futures.ThreadPoolExecutor):
    """Runs each call in a daemon thread of its own; neither shutdown nor the interpreter's exit waits for one."""

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> concurrent.futures.Future[_T]:
        future: concurrent.futures.Future[_T] = concurrent.futures.Future()

        def call() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

        threading.Thread(target=call, daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Return at once; a call still running is left to its thread."""


def _wake_periodically(loop: asyncio.AbstractEventLoop) -> None:
    """Wake ``loop`` every LOOP_WAKE_SECONDS until it closes, with a timer, which asyncio.run's cancelling of tasks
    leaves running."""
    loop.call_later(LOOP_WAKE_SECONDS, _wake_periodically, loop)


def _run_loop(main: Callable[_P, Coroutine[Any, Any, _T]], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """``asyncio.run(main(*args, **kwargs))``, whose end waits neither for a call still running in the loop's default
    executor, such as a name lookup, nor for a wake-up from another thread: that executor is a _DaemonThreadExecutor, and
    the loop wakes periodically."""

    async def run() -> _T:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(_DaemonThreadExecutor())
        _wake_periodically(loop)
        return await main(*args, **kwargs)

    return asyncio.run(run())


class APIAction(ABC):
    def __init__(self, subparsers: argparse._SubParsersAction[Any]) -> None:
        self.parser: argparse.ArgumentParser = subparsers.add_parser(
            self.command_name,
            help=self.description,
            description=self.description,
        )

    @property
    def command_name(self) -> str:
        raise NotImplementedError()

    @property
    def description(self) -> str:
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def priority(cls) -> int: ...

    @abstractmethod
    def add_action_arguments(self) -> None: ...

    @abstractmethod
    async def run_action(self, args: argparse.Namespace) -> int | None:
        """Perform the command. Return a process exit code, or None to mean success."""

    def preflight(self, args: argparse.Namespace) -> int | None:
        """Check what needs no API key, before one is looked for; an exit code ends the command there."""
        return None

    def add_common_arguments(self) -> None:
        # Taken after the command too, but listed only in `aristotle --help`, which gives it before the command.
        self.parser.add_argument("--api-key", type=str, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    def add_arguments(self) -> None:
        self.parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=VERBOSE_HELP)
        self.add_common_arguments()
        self.add_action_arguments()
        self.parser.set_defaults(func=self.run)

    # Subclasses may set this to True (e.g. for dry-run / local-only modes)
    # to skip API key validation.
    local_only: bool = False

    interrupt_reported: bool = False
    """Whether an interrupt that ends the command was already reported on stderr, so run() adds nothing."""

    def validate(self, offer_login: bool = True) -> bool:
        """Whether a key is available; with ``offer_login``, a missing key may be fixed by logging in at a terminal. A
        key get_api_key() refuses raises its _MalformedKeyError."""
        if self.local_only:
            return True
        try:
            get_api_key()
            return True
        except _MalformedKeyError:
            raise
        except ValueError:
            pass

        if offer_login and self._offer_login():
            return True

        logging.error(
            "Aristotle API key not found.\n"
            "Log in with:\n"
            "  aristotle login\n"
            "For a script or a CI job, create a key at "
            f"{get_web_url()}/keys and either set it in the environment:\n"
            "  export ARISTOTLE_API_KEY=<your-key>\n"
            "or pass it for one command:\n"
            "  aristotle --api-key <your-key> <command>"
        )
        return False

    def _offer_login(self) -> bool:
        """Offer to log in when there is no key; only at an interactive terminal.

        The prompt goes to stderr so a command whose stdout is piped or captured still shows it.
        Ctrl-C, and a key that could not be checked or stored, propagate to ``run``.
        """
        if sys.stdin is None or sys.stderr is None or not (sys.stdin.isatty() and sys.stderr.isatty()):
            return False

        sys.stderr.write("No Aristotle API key found. Log in now? [Y/n] ")
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer or answer.strip().lower() not in ("", "y", "yes"):
            return False

        try:
            _run_loop(login)
        except KeyboardInterrupt:
            # login() has said so.
            self.interrupt_reported = True
            raise
        except LoginError as e:
            if e.exit_code != EXIT_AUTH_ERROR:
                raise
            logging.error(str(e))
            return False
        return True

    def run(self, args: argparse.Namespace) -> int:
        """Run the command and return the process exit code."""
        escape_logged_text()

        if args.api_key is not None:
            set_api_key(args.api_key)

        # A shell's timeout and a cancelled job send SIGTERM, which then ends a command as Ctrl-C does, and a wait
        # says where the run stands. Only the main thread can set a handler.
        handles_sigterm = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler) if handles_sigterm else None
        try:
            if (code := self.preflight(args)) is not None:
                return code
            # A --json, --non-interactive or --answer run never reads stdin, so it is not offered a login.
            scripted = ("json", "non_interactive", "answer")
            reads_stdin = not any(getattr(args, name, None) for name in scripted)
            if not self.validate(offer_login=reads_stdin):
                return EXIT_AUTH_ERROR
            # A lookup's note waits until it is slow only for someone at a terminal; a script's log is unchanged.
            token = lookup_at_terminal.set(reads_stdin and _stderr_is_terminal())
            try:
                return _run_loop(self.run_action, args) or EXIT_SUCCESS
            finally:
                lookup_at_terminal.reset(token)
        except KeyboardInterrupt:
            # Ctrl-C: end the line and exit 130, without a traceback.
            if not self.interrupt_reported:
                sys.stderr.write("\n")
            return EXIT_INTERRUPTED
        except LoginError as e:
            logging.error(str(e))
            return e.exit_code
        except _MalformedKeyError as e:
            logging.error(str(e))
            return EXIT_AUTH_ERROR
        except AristotleNotFoundError as e:
            logging.error(str(e))
            return EXIT_NOT_FOUND
        except AmbiguousProjectError as e:
            logging.error(str(e))
            return EXIT_USAGE_ERROR
        except AristotleAPIError as e:
            if e.status_code == 401:
                logging.error(_invalid_key_message(api_key_source()))
                return EXIT_AUTH_ERROR
            logging.error(str(e))
            if isinstance(e, AristotleOutcomeUnknownError) and getattr(args, "json", False):
                print_json(OutcomeUnknown(error=str(e)))
            elif isinstance(e, AristotleNotProcessedError) and getattr(args, "json", False):
                print_json(NotProcessed(error=str(e)))
            return exit_code_for(e)
        finally:
            if handles_sigterm:
                signal.signal(signal.SIGTERM, previous)


def _stderr_is_terminal() -> bool:
    """Whether stderr, where notes go, is a terminal; a replacement stderr without ``isatty`` is not."""
    isatty = getattr(sys.stderr, "isatty", None)
    return isatty is not None and isatty()


def exit_code_for(error: AristotleAPIError) -> int:
    """7 when Aristotle may have acted on the request, else api_error_exit_code of its status."""
    if isinstance(error, AristotleOutcomeUnknownError):
        return EXIT_OUTCOME_UNKNOWN
    return api_error_exit_code(error.status_code)


def api_error_exit_code(status_code: int | None) -> int:
    """401 is 5, 403 and 404 are 4, 400, 410 and 422 are 2, 429 is 6, and any other status or none is 3."""
    if status_code == 401:
        return EXIT_AUTH_ERROR
    if status_code in (403, 404):
        return EXIT_NOT_FOUND
    if status_code in (400, 410, 422):
        return EXIT_USAGE_ERROR
    if status_code == 429:
        return EXIT_RATE_LIMITED
    return EXIT_INFRASTRUCTURE_ERROR


def _invalid_key_message(source: str | None) -> str:
    """The 401 message, naming where the rejected key came from."""
    if source == "environment":
        return (
            "The API key in ARISTOTLE_API_KEY is not valid. It takes precedence over `aristotle login`, "
            f"so unset it or replace it with a key from {get_web_url()}/keys."
        )
    if source == "argument":
        return f"The API key passed with --api-key is not valid. Get one from {get_web_url()}/keys."
    return "The stored API key is not valid. Log in again with:\n  aristotle login"
