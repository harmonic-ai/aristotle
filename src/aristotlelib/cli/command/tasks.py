import argparse
import logging

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import (
    PROJECT_REFERENCE_HELP,
    APIAction,
    print_json,
    project_from_reference,
    raise_for_pagination_key,
)
from aristotlelib.date_utils import format_relative_time
from aristotlelib.printable import print_line
from aristotlelib.results import TaskPage


class TasksAction(APIAction):
    @property
    def command_name(self) -> str:
        return "tasks"

    @property
    def description(self) -> str:
        return "List tasks for a project, newest first."

    @classmethod
    def priority(cls) -> int:
        return 70

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project to list tasks for: {PROJECT_REFERENCE_HELP}",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum number of tasks to return (1-100). Default: 10",
        )
        self.parser.add_argument(
            "--pagination-key",
            type=str,
            help="Pagination key to start from when retrieving tasks.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help='Print {"project_id", "tasks", "pagination_key"} as JSON instead of a table.',
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if not 1 <= args.limit <= 100:
            self.parser.error("--limit must be between 1 and 100")
        return None

    async def run_action(self, args: argparse.Namespace) -> None:
        project = await project_from_reference(args.project_id)
        try:
            tasks, pagination_key = await project.get_tasks(
                pagination_key=args.pagination_key,
                limit=args.limit,
            )
        except AristotleAPIError as e:
            raise_for_pagination_key(self.parser, e, args.pagination_key)

        if args.json:
            print_json(TaskPage(project_id=project.project_id, tasks=tasks, pagination_key=pagination_key))
        elif not tasks:
            logging.info("No tasks found.")
            return
        else:
            self._print_table(tasks)

        if pagination_key:
            logging.info(f"\nPagination key for next page: {pagination_key}")

    @staticmethod
    def _print_table(tasks: list[AgentTask]) -> None:
        print(f"{'ID':<36} {'CREATED':<20} {'NAME':<30} {'STATUS':<15}")
        print("-" * 105)

        for task in tasks:
            task_id = task.agent_task_id
            created = format_relative_time(task.created_at)
            name = task.description or "-"
            if len(name) > 30:
                name = name[:27] + "..."
            status = task.status.name

            print_line(f"{task_id:<36} {created:<20} {name:<30} {status:<15}")
