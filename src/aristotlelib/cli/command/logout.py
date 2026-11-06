import argparse
import logging

from aristotlelib import credentials
from aristotlelib.api_request import get_web_url
from aristotlelib.cli.api_action import APIAction
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR


class LogoutAction(APIAction):
    # Forgetting a key cannot be made to require one.
    local_only = True

    @property
    def command_name(self) -> str:
        return "logout"

    @property
    def description(self) -> str:
        return "Forget the API key stored on this machine."

    @classmethod
    def priority(cls) -> int:
        return 90

    def add_common_arguments(self) -> None:
        """No --api-key: forgetting the stored key uses no key."""

    def add_action_arguments(self) -> None:
        pass

    async def run_action(self, args: argparse.Namespace) -> int | None:
        try:
            removed = credentials.delete_credentials()
        except OSError as e:
            logging.error(
                f"Could not remove the stored API key at {credentials.credentials_path()}: {e.strerror or e}. "
                "Nothing was removed."
            )
            return EXIT_INFRASTRUCTURE_ERROR
        if not removed:
            logging.info("No stored API key to remove.")
            return None

        logging.info(f"Removed the stored API key ({credentials.credentials_path()}).")
        # Deleting the file does not revoke anything: the key still works for anyone who has
        # it, and only the web app can revoke it.
        logging.info(f"The key itself stays valid until you revoke it at {get_web_url()}/keys.")
        return None
