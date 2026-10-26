"""
Command-line interface for Aristotle SDK.
"""

import argparse
import difflib
import importlib
import inspect
import io
import logging
import os
import pkgutil
import re
import sys
import traceback
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar, NoReturn

import aristotlelib.cli.command as _command_pkg
from aristotlelib.api_request import get_web_url, set_surface
from aristotlelib.ci import context, github
from aristotlelib.cli.api_action import VERBOSE_HELP, APIAction
from aristotlelib.cli.version import get_version
from aristotlelib.exit_codes import EXIT_BROKEN_PIPE, EXIT_INFRASTRUCTURE_ERROR, EXIT_USAGE_ERROR
from aristotlelib.printable import printable

ASK_REPLACED_MESSAGE = "`aristotle ask` has been replaced by `aristotle continue`."
VERILOG_REMOVED_MESSAGE = (
    "`aristotle verilog` has been removed. Submit the design directory instead; Aristotle works "
    'out the sources, includes and macros itself:\n  aristotle submit "<prompt>" --project-dir rtl'
)
FORMALIZE_REMOVED_MESSAGE = (
    "`aristotle formalize` has been removed. Aristotle verifies Verilog, C and Rust code: submit the folder that "
    'holds it, with the property to prove in the prompt:\n  aristotle submit "<prompt>" --project-dir <folder>'
)
RETIRED_COMMANDS = {
    "ask": ASK_REPLACED_MESSAGE,
    "verilog": VERILOG_REMOVED_MESSAGE,
    "formalize": FORMALIZE_REMOVED_MESSAGE,
}


class CLIParser(argparse.ArgumentParser):
    """An ArgumentParser that takes ``--x_y`` for its ``--x-y`` and reports unknown options itself, with a suggestion.

    ``parse_known_args`` rejects leftover arguments instead of returning them, so a subcommand's parser reports them.
    """

    _unknown: tuple[str, ...] = ()
    """Options this parser lacks in the arguments it is parsing; while set, an error names them instead."""

    _parsing: ClassVar[list["CLIParser"]] = []
    """The parsers parsing now, outermost first: a subcommand's parser runs inside its parent's parse."""

    def parse_known_args(self, args: Iterable[str] | None = None, namespace: Any = None) -> tuple[Any, list[str]]:
        respelled, self._unknown = self._read(sys.argv[1:] if args is None else list(args))
        CLIParser._parsing.append(self)
        try:
            namespace, extras = super().parse_known_args(respelled, namespace)
            if extras:
                message = f"unrecognized arguments: {' '.join(extras)}"
                # A command's parser names the command that takes them with set_defaults(unrecognized_hint=...).
                hint = self.get_default("unrecognized_hint")
                self.error(f"{message}. {hint}" if hint else message)
        finally:
            CLIParser._parsing.remove(self)
            self._unknown = ()
        return namespace, extras

    def error(self, message: str) -> NoReturn:
        if not self._unknown:
            # An unknown option before the subcommand is the message, from the parser that rejected it.
            for parser in reversed(CLIParser._parsing):
                if parser is not self and parser._unknown:
                    parser.error(message)
        if self._unknown:
            named = [self._with_suggestion(name) for name in dict.fromkeys(self._unknown)]
            message = f"unknown option{'s' if len(named) > 1 else ''} {', '.join(named)}"
        message = printable(message)
        if context.detect_system() == context.CISystem.GITHUB:
            # Else a step that stops on its options shows only in its log. On stderr: stdout may be JSON a step reads.
            github.write_error_annotations(self.prog, [message], stream=sys.stderr)
        super().error(message)

    def _match_arguments_partial(self, actions: Sequence[argparse.Action], arg_strings_pattern: str) -> list[int]:
        """Match positionals as Python 3.12.7 and 3.13.1 do (gh-59317): those at the end that match nothing just before
        an option wait for the strings after it. Older Pythons, and 3.13.0, gave continue's optional prompt nothing
        there, so `continue <id> --wait "<prompt>"` rejected the prompt as unrecognized."""
        for i in range(len(actions), 0, -1):
            pattern = "".join(self._get_nargs_pattern(action) for action in actions[:i])
            match = re.match(pattern, arg_strings_pattern)
            if match is not None:
                counts = [len(group) for group in match.groups()]
                if match.end() < len(arg_strings_pattern) and arg_strings_pattern[match.end()] == "O":
                    while counts and not counts[-1]:
                        del counts[-1]
                return counts
        return []

    def _read(self, args: list[str]) -> tuple[list[str], tuple[str, ...]]:
        """``args`` with this parser's own ``--x_y`` and ``--x_y=v`` spelled ``--x-y``, and the options it lacks.

        Its own arguments end at ``--`` and, in a parser with subcommands, at the subcommand.
        """
        respelled = list(args)
        unknown: list[str] = []
        value_next = False
        values_continue = False
        for index, arg in enumerate(args):
            if arg == "--":
                break
            name, equals, value = arg.partition("=")
            # argparse reads a token with a space as the preceding option's value unless it names an option before "=".
            if not self._option_like(arg, name) or (value_next and " " in arg and self._find(name) is None):
                if self._subparsers is not None and not value_next:
                    break
                value_next = value_next and values_continue
                continue
            hyphenated = name.replace("_", "-")
            if name not in self._option_string_actions and hyphenated in self._option_string_actions:
                name = hyphenated
                respelled[index] = name + equals + value
            action = self._find(name)
            if action is None:
                unknown.append(name)
            value_next = action is not None and action.nargs != 0 and not equals
            values_continue = action is not None and action.nargs in ("*", "+")
        return respelled, tuple(unknown)

    def _option_like(self, arg: str, name: str) -> bool:
        """Whether argparse reads ``arg``, ``name`` being its part before any ``=``, as an option, known or not."""
        if not name or name[0] not in self.prefix_chars or len(arg) < 2 or " " in name:
            return False
        return not (self._negative_number_matcher.match(arg) and not self._has_negative_number_optionals)

    def _find(self, name: str) -> argparse.Action | None:
        """The action ``name`` selects, exactly or as an abbreviation of a long option."""
        if name in self._option_string_actions:
            return self._option_string_actions[name]
        if name.startswith("--") and self.allow_abbrev:
            return next((a for s, a in self._option_string_actions.items() if s.startswith(name)), None)
        return None

    def _with_suggestion(self, name: str) -> str:
        """``name``, followed by the closest long option in this parser's help, if any is close."""
        options = {
            option[2:]: option
            for action in self._actions
            if action.help != argparse.SUPPRESS
            for option in action.option_strings
            if option.startswith("--")
        }
        close = difflib.get_close_matches(name.lstrip("-").replace("_", "-").lower(), options, n=1)
        return f"{name} (did you mean {options[close[0]]}?)" if close else name


def _retired_command_message(argv: list[str]) -> str | None:
    """Return the replacement message if argv invokes a retired command, else None."""
    args = iter(argv[1:])
    for arg in args:
        if arg.replace("_", "-") == "--api-key":
            next(args, None)
            continue
        if arg.startswith("--api-key="):
            continue
        if arg.startswith("-"):
            continue
        return RETIRED_COMMANDS.get(arg)
    return None


def action_classes() -> list[type[APIAction]]:
    """Discover all concrete APIAction subclasses from aristotlelib.cli.command.

    Returns classes sorted by ascending ``priority()`` so that ``--help``
    lists commands in a predictable sequence.
    """
    classes: list[type[APIAction]] = []
    for info in pkgutil.iter_modules(_command_pkg.__path__):
        mod = importlib.import_module(f"{_command_pkg.__name__}.{info.name}")
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, APIAction)
                and obj is not APIAction
                and not inspect.isabstract(obj)
            ):
                classes.append(obj)
    classes.sort(key=lambda cls: cls.priority())
    return classes


def _tolerate_unencodable_output() -> None:
    """Escape what the output encoding cannot represent: Lean's → ∀ ≤ would crash a cp1252 Windows redirect."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


class _LogFormatter(logging.Formatter):
    """A record's message, prefixed with ``LEVEL - `` from ERROR up unless it already starts with ``Error:``."""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR and not record.getMessage().startswith("Error:"):
            return f"{record.levelname} - {record.getMessage()}"
        return record.getMessage()


class _AfterStdoutHandler(logging.StreamHandler):
    """Log to stderr after flushing stdout, so a message follows what was printed before it where both go to one pipe
    or file, as with ``2>&1``: stdout is block-buffered there, and stderr isn't."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if sys.stdout is not None:
                sys.stdout.flush()
        except (OSError, ValueError):
            # A closed pipe or stream fails again at the next flush, where it is reported.
            pass
        super().emit(record)


def _configure_logging() -> None:
    """Log from INFO up to stderr through _LogFormatter unless the root logger has a handler; mute httpx, httpcore."""
    handler = _AfterStdoutHandler()
    handler.setFormatter(_LogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True


def _discard_output() -> None:
    """Point stdout and stderr at the null device, so the interpreter's flush at exit has no closed pipe to fail on."""
    for stream in (sys.stdout, sys.stderr):
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
        except (AttributeError, OSError, ValueError):
            # No file descriptor behind the stream, as under a test's capture.
            pass


def _flush_output() -> None:
    """Flush stdout and stderr; a closed pipe raises BrokenPipeError, other failures are left to the exit flush."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.flush()
        except BrokenPipeError:
            raise
        except (OSError, ValueError):
            pass


def main():
    """Main entry point for the Aristotle CLI; output to a pipe nothing reads any more ends it quietly with 141."""
    try:
        try:
            return _main()
        finally:
            # Output still buffered fails here, rather than in the interpreter's flush at exit; logging keeps what it
            # could not write to stderr.
            _flush_output()
    except BrokenPipeError:
        _discard_output()
        return EXIT_BROKEN_PIPE


def _main() -> int:
    """Parse the arguments and run the command; returns the exit code."""
    set_surface("cli")
    _tolerate_unencodable_output()
    _configure_logging()
    retired = _retired_command_message(sys.argv)
    if retired is not None:
        print(retired, file=sys.stderr)
        return EXIT_USAGE_ERROR

    if sys.argv[1:] == ["--version"]:
        print(f"aristotlelib {get_version()}")
        return 0

    try:
        web_url = get_web_url()
    except ValueError as e:
        print(f"aristotle: error: {e}", file=sys.stderr)
        return 2

    parser = CLIParser(
        prog="aristotle",
        description="Aristotle SDK - formally verify your code from Python or the command line",
        epilog=f"For more information, visit {web_url}",
    )

    parser.add_argument("--version", action="version", version=f"aristotlelib {get_version()}")
    parser.add_argument("--verbose", action="store_true", help=VERBOSE_HELP)
    parser.add_argument(
        "--api-key",
        type=str,
        help=(
            "Aristotle API key for this command only, in place of the key `aristotle login` stored or the "
            "ARISTOTLE_API_KEY environment variable. With `login`, the key to check and store."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")
    for cls in action_classes():
        action = cls(subparsers)
        action.add_arguments()

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    http_logger = logging.getLogger("aristotle.http")
    previous_level = http_logger.level
    http_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return args.func(args)
    except BrokenPipeError:
        raise
    except Exception:
        # Exit 1 means a verification failed, so an unexpected error keeps its traceback and exits 3.
        sys.stderr.write(printable(traceback.format_exc()))
        return EXIT_INFRASTRUCTURE_ERROR
    finally:
        http_logger.setLevel(previous_level)


if __name__ == "__main__":
    sys.exit(main())
