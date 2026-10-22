import argparse
import logging

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleNotFoundError
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json, project_from_reference
from aristotlelib.exit_codes import EXIT_NOT_FOUND
from aristotlelib.project import Project, validate_project_id


class CancelAction(APIAction):
    @property
    def command_name(self) -> str:
        return "cancel"

    @property
    def description(self) -> str:
        return "Cancel a queued or running task."

    @classmethod
    def priority(cls) -> int:
        return 80

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_or_task_id",
            type=str,
            nargs="?",
            help=f"A task id, or a project: {PROJECT_REFERENCE_HELP}",
        )
        # Hidden, and still taken: the positional argument takes a project or a task.
        self.parser.add_argument("--task-id", type=str, help=argparse.SUPPRESS)
        self.parser.add_argument("--project-id", type=str, help=argparse.SUPPRESS)
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the cancelled task as JSON.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        given = sum(name is not None for name in (args.project_or_task_id, args.task_id, args.project_id))
        if given == 0:
            self.parser.error("the project or task to cancel is required")
        if given > 1:
            self.parser.error("give one project or task to cancel")
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        project, task = None, None

        if args.task_id is not None:
            task = await AgentTask.from_id(args.task_id)
        elif args.project_id is not None:
            project = await project_from_reference(args.project_id)
        else:
            try:
                project_id = validate_project_id(args.project_or_task_id)
            except ValueError:
                # Not a UUID, so a project name or id prefix.
                project = await project_from_reference(args.project_or_task_id)
            else:
                # A UUID: try project first, then task.
                try:
                    project = await Project.from_id(project_id)
                except AristotleNotFoundError:
                    try:
                        task = await AgentTask.from_id(args.project_or_task_id)
                    except AristotleNotFoundError as e:
                        raise AristotleNotFoundError("project or task", args.project_or_task_id, e.status_code) from e

        if project is not None:
            tasks, _ = await project.get_tasks(limit=1)
            task = tasks[0] if tasks else None

        if task is None:
            logging.error("No task found")
            return EXIT_NOT_FOUND

        await task.cancel()
        if project:
            logging.info(f"Task on project {project.project_id} canceled.")
        else:
            logging.info(f"Task {task.agent_task_id} canceled.")
        if args.json:
            print_json(task)
        return None
