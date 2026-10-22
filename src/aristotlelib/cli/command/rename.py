import argparse
import logging

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.local_file_utils import upload_name
from aristotlelib.project import Project, validate_project_name


def project_name_argument(value: str) -> str:
    """argparse type for a project name; returns it without surrounding whitespace, and escaped where it isn't UTF-8."""
    try:
        return upload_name(validate_project_name(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


class RenameAction(APIAction):
    @property
    def command_name(self) -> str:
        return "rename"

    @property
    def description(self) -> str:
        return "Rename a project."

    @classmethod
    def priority(cls) -> int:
        return 85

    def add_action_arguments(self) -> None:
        self.parser.add_argument("project_id", type=str, help=f"The project to rename: {PROJECT_REFERENCE_HELP}")
        self.parser.add_argument(
            "name",
            type=project_name_argument,
            help="The new name. Quote it if it contains spaces; put -- before it if it starts with -.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the renamed project as JSON.",
        )

    async def run_action(self, args: argparse.Namespace) -> None:
        args.project_id = await Project.resolve_id(args.project_id)
        try:
            project = await Project.rename_by_id(args.project_id, args.name)
        except AristotleAPIError as e:
            if e.status_code != 404:
                raise
            logging.error(f"Project {args.project_id} not found, or not yours to rename.")
            raise SystemExit(EXIT_NOT_FOUND) from None
        logging.info(f'Project {project.project_id} renamed to "{project.description}".')
        if args.json:
            print_json(project)
