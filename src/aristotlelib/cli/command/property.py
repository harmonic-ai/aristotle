import argparse
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from urllib.parse import quote

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleAPIError, get_web_url
from aristotlelib.ci.results import refuted
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, fetch_tasks, print_json
from aristotlelib.cli.command.properties import (
    ELLIPSIS,
    KINDS,
    created_by_label,
    evidence_label,
    location_label,
    project_not_found,
    shortened_name_pattern,
    status_label,
)
from aristotlelib.date_utils import format_relative_time
from aristotlelib.declaration import Declaration, DeclarationType
from aristotlelib.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE_ERROR
from aristotlelib.printable import print_line
from aristotlelib.project import Project, download_artifacts_hint
from aristotlelib.results import DeclarationRecord

LABEL_WIDTH = 14


def find_by_name(declarations: list[Declaration], name: str) -> list[Declaration]:
    """Match by id, then by exact name, then a name containing "..." as `properties` shortens names and any other
    name case-insensitively or as a dotted-name suffix; the first tier with a match wins."""
    by_id = [d for d in declarations if d.declaration_id == name]
    if by_id:
        return by_id
    exact = [d for d in declarations if d.name == name]
    if exact:
        return exact
    if ELLIPSIS in name:
        shortened = shortened_name_pattern(name)
        return [d for d in declarations if shortened.fullmatch(d.name.casefold())]
    wanted = name.casefold()
    return [d for d in declarations if d.name.casefold() == wanted or d.name.casefold().endswith(f".{wanted}")]


def kind_label(declaration: Declaration) -> str:
    return "property" if declaration.declaration_type == DeclarationType.PROPERTY else "definition"


def format_timestamp(dt: datetime) -> str:
    """``YYYY-MM-DD HH:MM UTC (relative)``; a naive datetime is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC ({format_relative_time(dt)})"


def format_record(declaration: Declaration, tasks: Sequence[AgentTask]) -> str:
    """The declaration in full, its status as status_label gives it for the project's ``tasks``."""
    fields = [
        ("Kind", kind_label(declaration)),
        ("Status", status_label(declaration, tasks)),
        ("Evidence", evidence_label(declaration)),
        ("Created by", created_by_label(declaration)),
        ("Created", format_timestamp(declaration.created_at)),
        ("Last updated", format_timestamp(declaration.last_updated)),
        ("Source", location_label(declaration)),
        ("Anchor", declaration.source_anchor or "-"),
        ("Generated", declaration.filename),
        ("Web", f"{get_web_url()}/projects/{quote(declaration.project_id, safe='')}"),
    ]
    lines = [declaration.name]
    lines.extend(f"{label + ':':<{LABEL_WIDTH}} {value}" for label, value in fields)
    if declaration.description:
        lines.extend(["", "Description:", f"  {declaration.description}"])
    else:
        lines.extend(["", "Description:   -"])
    # Lean code is whitespace-sensitive and is printed verbatim.
    lines.extend(["", "Lean statement:", declaration.lean_code or "-"])
    if (
        declaration.declaration_type == DeclarationType.PROPERTY
        and refuted(declaration)
        and declaration.evidence_message
    ):
        lines.extend(["", "Refutation evidence:", f"  {declaration.evidence_message}"])
    artifacts = f"\n{' ' * (LABEL_WIDTH + 1)}".join(declaration.artifact_paths) or "-"
    lines.extend(["", f"{'Artifacts:':<{LABEL_WIDTH}} {artifacts}"])
    return "\n".join(lines)


class PropertyAction(APIAction):
    @property
    def command_name(self) -> str:
        return "property"

    @property
    def description(self) -> str:
        return "Show one property in full: its statement, status and where it came from."

    @classmethod
    def priority(cls) -> int:
        return 64

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project the property belongs to: {PROJECT_REFERENCE_HELP}",
        )
        self.parser.add_argument(
            "name",
            type=str,
            help="The property's name, or its declaration id. A name `aristotle properties` shortened with ... can "
            "be given as shown, and case and any leading namespace can be left out when that is unambiguous.",
        )
        self.parser.add_argument(
            "--kind",
            choices=list(KINDS),
            help="Look only at properties, or only at definitions. Needed when one of each shares the name.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the full record as JSON.",
        )

    async def run_action(self, args: argparse.Namespace) -> None:
        # The server has no lookup by name; every declaration of the requested kind(s) is fetched and matched here.
        kind = None if args.kind is None else KINDS[args.kind]
        args.project_id = await Project.resolve_id(args.project_id)
        try:
            declarations = await Declaration.list_all_for_project(args.project_id, declaration_type=kind)
        except AristotleAPIError as e:
            if not project_not_found(e):
                raise
            logging.error(f"Project {args.project_id} not found or not accessible.")
            raise SystemExit(EXIT_NOT_FOUND)
        matches = find_by_name(declarations, args.name)

        if not matches:
            noun = args.kind or "property"
            narrowing = "" if args.kind is None else f" --kind {args.kind}"
            logging.error(
                f"No {noun} named '{args.name}' in project {args.project_id}. See what there is with:\n"
                f"  aristotle properties {args.project_id}{narrowing}"
            )
            raise SystemExit(EXIT_NOT_FOUND)
        if len(matches) > 1:
            # Names are not unique, even within a kind, so each match is listed with its kind and id.
            listing = "\n".join(f"  {d.name} ({kind_label(d)}, id {d.declaration_id})" for d in matches)
            logging.error(
                f"'{args.name}' matches {len(matches)} declarations. Give the full name or the id of "
                f"one, or narrow it with --kind:\n{listing}"
            )
            raise SystemExit(EXIT_USAGE_ERROR)

        declaration = matches[0]
        is_property = declaration.declaration_type == DeclarationType.PROPERTY
        # JSON states `unchecked`, so it fails rather than guess without the tasks.
        tasks = await fetch_tasks(args.project_id, [declaration], strict=args.json) if is_property else []
        if args.json:
            print_json(DeclarationRecord.of(declaration, tasks))
            return
        print_line(format_record(declaration, tasks))
        if hint := download_artifacts_hint(declaration.project_id, declaration.artifact_paths):
            # Separate the download hint from the record.
            logging.info(f"\n{hint}")
