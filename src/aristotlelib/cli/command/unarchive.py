import argparse
import logging

from aristotlelib.api_request import AristotleAPIError, _IntermediaryError
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.project import Project


class UnarchiveAction(APIAction):
    @property
    def command_name(self) -> str:
        return "unarchive"

    @property
    def description(self) -> str:
        return "Unarchive a project, putting it back in `list`."

    @classmethod
    def priority(cls) -> int:
        return 88

    def add_action_arguments(self) -> None:
        self.parser.add_argument("project", type=str, help=f"The archived project: {PROJECT_REFERENCE_HELP}")
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the unarchived project as JSON.",
        )

    async def run_action(self, args: argparse.Namespace) -> int | None:
        project_id = await Project.resolve_id(args.project, archived=True)
        try:
            project = await Project.unarchive_by_id(project_id)
        except AristotleAPIError as e:
            if e.status_code != 404 or isinstance(e, _IntermediaryError):
                raise
            logging.error(f"Project {project_id} not found, or not yours to unarchive.")
            return EXIT_NOT_FOUND
        logging.info(f"Project {project.project_id} unarchived.")
        if args.json:
            print_json(project)
        return None
