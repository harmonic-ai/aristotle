import argparse
import logging

from aristotlelib.agent_task import task_status_label
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import APIAction, print_json, raise_for_pagination_key
from aristotlelib.date_utils import format_relative_time
from aristotlelib.printable import print_line
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.results import ProjectPage


class ListAction(APIAction):
    @property
    def command_name(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "List your projects, newest first."

    @classmethod
    def priority(cls) -> int:
        return 50

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "--status",
            nargs="+",
            action="extend",
            choices=[s.name for s in ProjectStatus if s != ProjectStatus.UNKNOWN],
            help="Only list projects with these statuses.",
        )
        self.parser.add_argument(
            "--archived",
            action="store_true",
            help="List archived projects instead.",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="How many projects to list, 1 to 100 (default: 10).",
        )
        self.parser.add_argument(
            "--pagination-key",
            type=str,
            help="Pagination key to start from when retrieving projects.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the projects as JSON instead of a table.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if not 1 <= args.limit <= 100:
            self.parser.error("--limit must be between 1 and 100")
        return None

    async def run_action(self, args: argparse.Namespace) -> None:
        status_filter = None
        if args.status:
            try:
                status_filter = [ProjectStatus[s] for s in args.status]
            except ValueError:
                logging.error(f"Invalid statuses: {args.status}")
                return

        try:
            projects, pagination_key = await Project.list_projects(
                pagination_key=args.pagination_key,
                limit=args.limit,
                status=status_filter,
                archived=args.archived,
            )
        except AristotleAPIError as e:
            raise_for_pagination_key(self.parser, e, args.pagination_key)

        if args.json:
            print_json(ProjectPage(projects=projects, pagination_key=pagination_key))
        elif not projects:
            logging.info("No archived projects found." if args.archived else "No projects found.")
            return
        else:
            self._print_table(projects)

        if pagination_key:
            logging.info(f"\nPagination key for next page: {pagination_key}")

    @staticmethod
    def _print_table(projects: list[Project]) -> None:
        print(f"{'ID':<36} {'CREATED':<20} {'NAME':<30} {'STATUS':<7} {'LAST RUN':<20}")
        print("-" * 117)

        for project in projects:
            project_id = project.project_id
            created = format_relative_time(project.created_at)
            name = project.description or "-"
            if len(name) > 30:
                name = name[:27] + "..."
            status = project.status.name
            # How the latest run stands or ended, which STATUS, running or idle, doesn't tell.
            last_run = task_status_label(project.latest_run_status) if project.latest_run_status else "-"

            print_line(f"{project_id:<36} {created:<20} {name:<30} {status:<7} {last_run:<20}")
