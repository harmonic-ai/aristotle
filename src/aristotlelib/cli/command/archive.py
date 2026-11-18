import argparse
import logging

from aristotlelib.api_request import AristotleAPIError, _IntermediaryError
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.project import Project
from aristotlelib.shell import shell_hint


class ArchiveAction(APIAction):
    @property
    def command_name(self) -> str:
        return "archive"

    @property
    def description(self) -> str:
        return "Archive a project, taking it out of `list`."

    @classmethod
    def priority(cls) -> int:
        return 87

    def add_action_arguments(self) -> None:
        self.parser.add_argument("project", type=str, help=f"The project: {PROJECT_REFERENCE_HELP}")
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the archived project as JSON.",
        )

    async def run_action(self, args: argparse.Namespace) -> int | None:
        project_id = await Project.resolve_id(args.project)
        try:
            project = await Project.archive_by_id(project_id)
        except AristotleAPIError as e:
            if isinstance(e, _IntermediaryError):
                raise
            if e.status_code == 404:
                logging.error(f"Project {project_id} not found, or not yours to archive.")
                return EXIT_NOT_FOUND
            if e.status_code == 409:
                logging.error(
                    f"A run of project {project_id} is still going, so it can't be archived until the run ends.\n"
                    + shell_hint("Wait for it to finish, or stop it with", ["aristotle", "cancel", project_id])
                )
                return EXIT_NOT_FOUND
            raise
        logging.info(
            f"Project {project.project_id} archived.\n"
            + shell_hint("Unarchive it with", ["aristotle", "unarchive", project.project_id])
        )
        if args.json:
            print_json(project)
        return None
