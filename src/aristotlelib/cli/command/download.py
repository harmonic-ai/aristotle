import argparse
import logging
import os
from contextlib import aclosing

from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, print_json, project_from_reference
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_NOT_FOUND, EXIT_USAGE_ERROR
from aristotlelib.project import (
    ARTIFACTS_FOLDER,
    Project,
    ProjectHasNoFilesError,
    saved_path,
    validate_destination,
)
from aristotlelib.results import ArtifactDownloadResult, DownloadResult, SavedArtifact


class DownloadAction(APIAction):
    @property
    def command_name(self) -> str:
        return "download"

    @property
    def description(self) -> str:
        return "Download a project's files."

    @classmethod
    def priority(cls) -> int:
        return 40

    def add_action_arguments(self) -> None:
        self.parser.add_argument("project_id", type=str, help=f"The project to download: {PROJECT_REFERENCE_HELP}")

        self.parser.add_argument(
            "--destination",
            # Expanded here, as a shell would: --destination=~/out reaches argparse with the ~ unexpanded.
            type=os.path.expanduser,
            help=(
                "Where to save the result archive. A directory, or a path ending in /, gets it under its default "
                "name; missing directories are made. Default: the current directory. With --artifact, the folder to "
                f"save the files in, at their paths in the project; default: {ARTIFACTS_FOLDER}, which must not be a link."
            ),
        )

        self.parser.add_argument(
            "--artifact",
            action="append",
            metavar="PATH",
            help=(
                "Save the project's file at PATH, its path in the project, such as one a property's artifact_paths "
                "name, instead of the archive; repeat it for more files. Each is saved at that path under "
                "--destination, keeping its folders, and replaces a file there."
            ),
        )

        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print where the archive, or each --artifact, was saved as JSON.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        try:
            if args.artifact:
                if args.destination is None and os.path.islink(ARTIFACTS_FOLDER):
                    try:
                        link = f"a link to '{os.readlink(ARTIFACTS_FOLDER)}'"
                    except OSError as e:
                        link = f"a link whose target can't be read ({e.strerror or e})"
                    raise ValueError(
                        f"Cannot save the files to {ARTIFACTS_FOLDER}: it is {link}. Name another folder with --destination."
                    )
                folder = ARTIFACTS_FOLDER if args.destination is None else args.destination
                validate_destination(folder, folder=True)
                for artifact in args.artifact:
                    try:
                        saved_path(folder, artifact)
                    except ValueError as e:
                        raise ValueError(f"--artifact '{artifact}': {e}") from None
            elif args.destination is not None:
                validate_destination(args.destination)
        except ValueError as e:
            self.parser.error(str(e))
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        project = await project_from_reference(args.project_id)
        if args.artifact:
            return await self._save_artifacts(project, args)
        try:
            path = await project.get_files(destination=args.destination)
        except ProjectHasNoFilesError as e:
            logging.error(str(e))
            return EXIT_NOT_FOUND
        except OSError as e:
            logging.error(f"The project's files were not saved: {e}")
            return EXIT_INFRASTRUCTURE_ERROR
        logging.info(f"Saved the project's files to {path}")
        if args.json:
            print_json(DownloadResult(project_id=project.project_id, result_path=str(path)))
        return None

    async def _save_artifacts(self, project: Project, args: argparse.Namespace) -> int | None:
        """Save each --artifact under --destination, going on past one the project has no file at; 4 if any was."""
        artifacts = list(dict.fromkeys(args.artifact))
        folder = ARTIFACTS_FOLDER if args.destination is None else args.destination
        saved: list[SavedArtifact] = []
        missing: list[str] = []
        try:
            async with aclosing(project.save_files(artifacts, folder)) as files:
                async for artifact, path in files:
                    if path is None:
                        logging.error(f"Project {project.project_id} has no file {artifact}.")
                        missing.append(artifact)
                    else:
                        logging.info(f"Saved {artifact} to {path}")
                        saved.append(SavedArtifact(artifact_path=artifact, saved_path=str(path)))
        except ValueError as e:
            # A link that appeared after preflight.
            logging.error(f"--artifact '{artifacts[len(saved) + len(missing)]}': {e}")
            return EXIT_USAGE_ERROR
        except OSError as e:
            logging.error(f"{artifacts[len(saved) + len(missing)]} was not saved: {e}")
            return EXIT_INFRASTRUCTURE_ERROR
        if args.json:
            print_json(ArtifactDownloadResult(project_id=project.project_id, files=saved, missing=missing))
        return EXIT_NOT_FOUND if missing else None
