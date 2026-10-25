import argparse
import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from typing import Any

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleAPIError, _IntermediaryError
from aristotlelib.ci.results import FAILURE_REASON_LABELS, refuted
from aristotlelib.cli.api_action import (
    PROJECT_REFERENCE_HELP,
    APIAction,
    fetch_tasks,
    print_json,
)
from aristotlelib.date_utils import format_relative_time
from aristotlelib.declaration import (
    Declaration,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.printable import print_line
from aristotlelib.project import Project
from aristotlelib.results import DeclarationPage, DeclarationRecord, unchecked

#: The outcome words --status takes, as the table shows them, and the statuses each covers; PROVED and IN_PROGRESS
#: are words and status names both. NOT_VERIFIED is the old name for UNRESOLVED, still taken but no longer listed.
OUTCOME_STATUSES = {
    "UNRESOLVED": {DeclarationStatus.FAILED},
    "NOT_VERIFIED": {DeclarationStatus.FAILED},
}
LISTED_STATUS_CHOICES = [
    "PROVED",
    "REFUTED",
    "IN_PROGRESS",
    "UNRESOLVED",
    "FAILED",
    "ALL",
]
STATUS_CHOICES = [*LISTED_STATUS_CHOICES, "NOT_VERIFIED"]
KINDS = {"property": DeclarationType.PROPERTY, "definition": DeclarationType.DEFINITION}
CREATED_BY = {"you": DeclarationSource.USER, "aristotle": DeclarationSource.AGENT, "all": None}
# (sort key, reverse): timestamps newest first, names A to Z.
SORT_KEYS: dict[str, tuple[Callable[[Declaration], Any], bool]] = {
    "updated": (lambda d: d.last_updated, True),
    "created": (lambda d: d.created_at, True),
    "name": (lambda d: d.name.casefold(), False),
}

DEFAULT_LIMIT = 30

NAME_WIDTH = 40
"""The name column's preferred width; evidence and longer statuses can take space from it at a terminal."""
STATUS_WIDTH = 22
CREATED_BY_WIDTH = 10
UPDATED_WIDTH = 14
LOCATION_WIDTH = 30
"""The most of a terminal's width held for locations when the name column widens; a longer location runs past it."""
ELLIPSIS = "..."


def project_not_found(error: AristotleAPIError) -> bool:
    """The server answers an unknown project id with 403, so 403 and 404 are both reported as not found."""
    return error.status_code in (403, 404) and not isinstance(error, _IntermediaryError)


def created_by_label(declaration: Declaration) -> str:
    """The author as shown in the table: "you", "Aristotle" or "-"."""
    if declaration.source == DeclarationSource.USER:
        return "you"
    if declaration.source == DeclarationSource.AGENT:
        return "Aristotle"
    return "-"


#: A failure reason short enough for STATUS_WIDTH where its label isn't; `aristotle property` gives the label.
SHORT_FAILURE_REASONS = {DeclarationFailureReason.PROOF_CHECK_FAILED: "proof broken"}


#: The outcome each status comes to, as every command words it; any other status shows its name.
OUTCOME_LABELS = {
    DeclarationStatus.PROVED: "PROVED",
    DeclarationStatus.REFUTED: "REFUTED",
    DeclarationStatus.IN_PROGRESS: "IN PROGRESS",
    DeclarationStatus.FAILED: "UNRESOLVED",
}


def status_label(declaration: Declaration, tasks: Sequence[AgentTask], short: bool = False) -> str:
    """The outcome the status comes to, with why a property is unresolved, and "unchecked"
    for a verdict the project's ``tasks`` show Aristotle never checked (``short`` to fit the table); "-" for a
    definition, as on the web."""
    if declaration.declaration_type != DeclarationType.PROPERTY:
        # The API reports every definition as IN_PROGRESS forever; status is proof progress.
        return "-"
    label = OUTCOME_LABELS.get(declaration.status, declaration.status.name)
    details = []
    if declaration.status == DeclarationStatus.FAILED and (reason := declaration.failure_reason) is not None:
        reason_label = SHORT_FAILURE_REASONS.get(reason) if short else None
        details.append(reason_label or FAILURE_REASON_LABELS.get(reason) or reason.name.lower().replace("_", " "))
    elif unchecked(declaration, tasks):
        details.append(
            "unchecked" if short else "unchecked: the task that last updated it ended before Aristotle checked it"
        )
    return f"{label} ({', '.join(details)})" if details else label


def evidence_label(declaration: Declaration) -> str:
    """The reported evidence kind for a refuted property; a dash otherwise."""
    if (
        declaration.declaration_type == DeclarationType.PROPERTY
        and refuted(declaration)
        and declaration.evidence is not None
    ):
        return declaration.evidence.name
    return "-"


def location_label(declaration: Declaration) -> str:
    """``source_file:source_line``, or "-" when no location is recorded."""
    if declaration.source_file is None:
        return "-"
    if declaration.source_line is None:
        return declaration.source_file
    return f"{declaration.source_file}:{declaration.source_line}"


def truncate_middle(text: str, width: int) -> str:
    """Elide the middle of ``text`` to fit ``width``, keeping both ends of a dotted name."""
    if len(text) <= width:
        return text
    head = (width - len(ELLIPSIS)) // 2
    tail = width - len(ELLIPSIS) - head
    return f"{text[:head]}{ELLIPSIS}{text[-tail:]}"


def shared_namespaces(names: list[str]) -> dict[str, str]:
    """Each name's longest run of leading dotted components, final dot included, that another name starts with too."""
    unique = sorted(set(names))
    shared = {}
    for i, name in enumerate(unique):
        # In sorted order, the longest prefix a name shares with any other is the one it shares with a neighbor.
        neighbors = unique[max(i - 1, 0) : i] + unique[i + 1 : i + 2]
        prefix = max((os.path.commonprefix([name, other]) for other in neighbors), key=len, default="")
        shared[name] = prefix[: prefix.rfind(".") + 1]
    return shared


def shorten_name(name: str, width: int, namespace: str) -> str:
    """``name`` if it fits ``width``; else with ``namespace`` cut to an ellipsis, then elided in the middle to fit."""
    if len(name) <= width:
        return name
    if len(namespace) > len(ELLIPSIS) and name.startswith(namespace):
        name = ELLIPSIS + name[len(namespace) :]
    return truncate_middle(name, width)


CUT = re.compile(r"\.{3,}")
"""An ellipsis where a name was cut, with any dot of the name beside it."""


def shortened_name_pattern(name: str) -> re.Pattern[str]:
    """The casefolded names ``name`` can stand for: each cut is any text, and a leading one a whole namespace."""
    first, *rest = CUT.split(name.casefold())
    if not first and rest:
        return re.compile(r".*\." + ".*".join(map(re.escape, rest)))
    return re.compile(".*".join(map(re.escape, [first, *rest])))


ENDING = 8
"""How many final characters shown_names indexes names by; a match ends with the last uncut part of the shown text."""


def shown_names(names: list[str], width: int) -> list[str]:
    """``names`` as a column ``width`` wide shows them: each cut by the namespace it shares with another name and
    then in the middle, else in the middle alone, so that shortened_name_pattern matches no other name; else whole."""
    if all(len(name) <= width for name in names):
        return list(names)
    namespaces = shared_namespaces(names)
    folded = {name: name.casefold() for name in names}
    by_ending: dict[str, list[str]] = {}
    for name, fold in folded.items():
        by_ending.setdefault(fold[-ENDING:], []).append(name)

    def stands_for_only(text: str, name: str) -> bool:
        last = CUT.split(text.casefold())[-1]
        candidates = by_ending.get(last[-ENDING:], []) if len(last) >= ENDING else folded
        others = [other for other in candidates if other != name]
        if not others:
            return True
        pattern = shortened_name_pattern(text)
        return not any(pattern.fullmatch(folded[other]) for other in others)

    def show(name: str) -> str:
        if len(name) <= width:
            return name
        for text in (shorten_name(name, width, namespaces[name]), truncate_middle(name, width)):
            if stands_for_only(text, name):
                return text
        return name

    return [show(name) for name in names]


def terminal_width() -> int | None:
    """The width in columns of the terminal stdout writes to; None when stdout isn't a terminal."""
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty is None or not isatty():
        return None
    return shutil.get_terminal_size().columns


def _statuses(args: argparse.Namespace) -> set[DeclarationStatus]:
    """The --status filters; empty for ALL, the default."""
    if not args.status or "ALL" in args.status:
        return set()
    return {s for name in args.status for s in OUTCOME_STATUSES.get(name) or {DeclarationStatus[name]}}


class PropertiesAction(APIAction):
    @property
    def command_name(self) -> str:
        return "properties"

    @property
    def description(self) -> str:
        return "List what a project proves: its properties, most recently updated first."

    @classmethod
    def priority(cls) -> int:
        return 62

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project to list properties for: {PROJECT_REFERENCE_HELP}",
        )
        # Words left over, such as a property's name, are refused with a pointer at the command that shows one.
        self.parser.set_defaults(
            unrecognized_hint="properties lists a project's properties; show one property with:\n"
            "  aristotle property <project-id> <name>"
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            help=f"Maximum number of rows to show. Default: {DEFAULT_LIMIT}, and every match with --json.",
        )
        self.parser.add_argument(
            "--status",
            action="append",
            choices=STATUS_CHOICES,
            metavar="{" + ",".join(LISTED_STATUS_CHOICES) + "}",
            help=(
                "Only show properties with this status, as the table words it or as Aristotle reports it; repeat it "
                "for several, to show those with any of them. REFUTED includes counterexample evidence, a formal "
                "proof of the negation, or both. IN PROGRESS in the table is "
                "IN_PROGRESS, and UNRESOLVED is FAILED (NOT_VERIFIED, its old name, still works). A verdict Aristotle "
                "never checked is listed under its word, marked (unchecked), though show counts it as unchecked. "
                "Default: ALL"
            ),
        )
        self.parser.add_argument(
            "--kind",
            choices=list(KINDS),
            default="property",
            help=(
                "Show properties (what Aristotle was asked to establish) or definitions "
                "(supporting context, never proved or refuted). Default: property"
            ),
        )
        self.parser.add_argument(
            "--sort",
            choices=list(SORT_KEYS),
            default="updated",
            help="Order by last update (newest first), creation (newest first) or name. Default: updated",
        )
        self.parser.add_argument(
            "--created-by",
            choices=list(CREATED_BY),
            default="all",
            help="Only show properties you wrote (you) or ones Aristotle proposed (aristotle). Default: all",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help='Print {"project_id", "declarations", "pagination_key"} as JSON instead of a table.',
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if args.status and "ALL" in args.status and len(set(args.status)) > 1:
            self.parser.error("--status ALL can't be combined with another status.")
        if _statuses(args) and KINDS[args.kind] != DeclarationType.PROPERTY:
            self.parser.error("--status applies to properties; definitions are never proved or refuted.")
        if args.limit is not None and args.limit < 1:
            self.parser.error("--limit must be at least 1")
        return None

    async def run_action(self, args: argparse.Namespace) -> None:
        kind = KINDS[args.kind]
        statuses = _statuses(args)
        source = CREATED_BY[args.created_by]
        sort_key, newest_first = SORT_KEYS[args.sort]
        limit = DEFAULT_LIMIT if args.limit is None else args.limit

        args.project_id = await Project.resolve_id(args.project_id)
        try:
            everything = await Declaration.list_all_for_project(args.project_id, declaration_type=kind)
        except AristotleAPIError as e:
            if not project_not_found(e):
                raise
            logging.error(f"Project {args.project_id} not found or not accessible.")
            raise SystemExit(EXIT_NOT_FOUND)
        matches = [
            d for d in everything if (not statuses or d.status in statuses) and (source is None or d.source == source)
        ]
        matches.sort(key=sort_key, reverse=newest_first)
        # JSON prints every match unless --limit was given; the "more not shown" note on stderr is invisible to a
        # pipeline.
        if args.limit is None and args.json:
            limit = len(matches)
        declarations = matches[:limit]
        hidden = len(matches) - len(declarations)

        # Only properties have verdicts. JSON states `unchecked` for each, so it fails rather than guess without tasks.
        tasks = (
            await fetch_tasks(args.project_id, declarations, strict=args.json)
            if kind == DeclarationType.PROPERTY
            else []
        )
        if args.json:
            records = [DeclarationRecord.of(declaration, tasks) for declaration in declarations]
            print_json(DeclarationPage(project_id=args.project_id, declarations=records, pagination_key=None))
        elif not declarations:
            noun = "properties" if kind == DeclarationType.PROPERTY else "definitions"
            logging.info(f"No {noun} found.")
            return
        else:
            if self._print_table(declarations, tasks):
                logging.info(
                    "\nSome names are shortened to fit the terminal. `aristotle property` takes them as shown, and "
                    "piped output and --json print them in full."
                )

        if hidden:
            logging.info(f"\n{hidden} more not shown. Raise --limit to see more.")

    @staticmethod
    def _print_table(declarations: list[Declaration], tasks: Sequence[AgentTask]) -> bool:
        """Print the table, with the verdicts the project's ``tasks`` show Aristotle never checked marked; return
        whether a name was shortened to fit the terminal."""
        names = [declaration.name for declaration in declarations]
        statuses = [status_label(declaration, tasks, short=True) for declaration in declarations]
        status_width = max(STATUS_WIDTH, *map(len, statuses))
        evidence = [evidence_label(declaration) for declaration in declarations]
        evidence_width = max(len("EVIDENCE"), *map(len, evidence))
        locations = [location_label(declaration) for declaration in declarations]
        location_width = min(LOCATION_WIDTH, max(len(location) for location in [*locations, "LOCATION"]))
        others = status_width + evidence_width + CREATED_BY_WIDTH + UPDATED_WIDTH + location_width + 5
        name_width = max(NAME_WIDTH, *map(len, names))
        if (columns := terminal_width()) is not None:
            # Evidence and a wider status take space from names so the table keeps its fit.
            least = NAME_WIDTH - (status_width - STATUS_WIDTH) - evidence_width - 1
            name_width = max(least, min(name_width, columns - others))
        shown = shown_names(names, name_width)

        print(
            f"{'NAME':<{name_width}} {'STATUS':<{status_width}} {'EVIDENCE':<{evidence_width}} "
            f"{'CREATED BY':<{CREATED_BY_WIDTH}} "
            f"{'UPDATED':<{UPDATED_WIDTH}} LOCATION"
        )
        print("-" * (name_width + others))
        for declaration, name, status, kind, location in zip(declarations, shown, statuses, evidence, locations):
            updated = format_relative_time(declaration.last_updated)
            print_line(
                f"{name:<{name_width}} {status:<{status_width}} {kind:<{evidence_width}} "
                f"{created_by_label(declaration):<{CREATED_BY_WIDTH}} {updated:<{UPDATED_WIDTH}} {location}"
            )
        return shown != names
