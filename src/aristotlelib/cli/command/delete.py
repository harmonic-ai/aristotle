import argparse
import asyncio
import logging
import sys

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json, project_from_reference
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.printable import printable
from aristotlelib.project import Project
from aristotlelib.results import DeleteResult
from aristotlelib.shell import shell_hint


def _at_terminal() -> bool:
    """Whether someone at a terminal can answer a prompt: on stderr, so a piped stdout still shows it."""
    return sys.stdin is not None and sys.stderr is not None and sys.stdin.isatty() and sys.stderr.isatty()


class DeleteAction(APIAction):
    @property
    def command_name(self) -> str:
        return "delete"

    @property
    def description(self) -> str:
        return "Delete a project for good, after asking."

    @classmethod
    def priority(cls) -> int:
        return 86

    def add_action_arguments(self) -> None:
        self.parser.add_argument("project", type=str, help=f"The project to delete: {PROJECT_REFERENCE_HELP}")
        self.parser.add_argument(
            "--yes",
            "-y",
            action="store_true",
            help="Delete without asking first. Required without a terminal, and with --json.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help='Print {"project_id", "deleted"} as JSON.',
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if not args.yes and (args.json or not _at_terminal()):
            self.parser.error("deleting can't be undone: pass --yes to delete without being asked")
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        project = await project_from_reference(args.project)
        project_id = project.project_id
        if not args.yes:
            name = f'"{printable(project.description)}" ({project_id})' if project.description else project_id
            sys.stderr.write(
                f"Delete project {name}? Its runs and results go with it, and its shared link stops working. "
                "This can't be undone. [y/N] "
            )
            sys.stderr.flush()
            # Read in a thread: a read blocking the loop would hold Ctrl-C back until Enter (3.11+).
            answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
            if answer.strip().lower() not in ("y", "yes"):
                logging.info("Nothing was deleted.")
                return None
        try:
            await Project.delete_by_id(project_id)
        except AristotleAPIError as e:
            if e.status_code == 404:
                logging.error(f"Project {project_id} not found, or not yours to delete.")
                return EXIT_NOT_FOUND
            if e.status_code == 409:
                logging.error(
                    f"A run of project {project_id} is still going, so it can't be deleted until the run ends.\n"
                    + shell_hint("Stop the run with", ["aristotle", "cancel", project_id])
                )
                return EXIT_NOT_FOUND
            raise
        logging.info(f"Project {project_id} deleted.")
        if args.json:
            print_json(DeleteResult(project_id=project_id, deleted=True))
        return None
