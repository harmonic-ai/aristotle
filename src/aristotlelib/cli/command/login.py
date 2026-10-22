import argparse
import asyncio
import sys

from aristotlelib.cli.api_action import EXIT_INTERRUPTED, APIAction, print_json
from aristotlelib.cli.login_flow import LoginError, login
from aristotlelib.printable import printable
from aristotlelib.results import LoginPending


def _print_pending(pending: LoginPending) -> None:
    """Print the code to approve as JSON at once, for a program that reads it while the login waits."""
    print_json(pending)
    sys.stdout.flush()


class LoginAction(APIAction):
    # Logging in is what a command does when it has no key, so it must not demand one.
    local_only = True

    @property
    def command_name(self) -> str:
        return "login"

    @property
    def description(self) -> str:
        return "Log in by approving a code in the browser, and store the API key on this machine."

    @classmethod
    def priority(cls) -> int:
        return 5

    def add_common_arguments(self) -> None:
        self.parser.add_argument(
            "--api-key",
            type=str,
            default=argparse.SUPPRESS,
            help=(
                "Check this API key and store it, instead of logging in in the browser. A key piped to stdin stays "
                "out of the process list and the shell history; one given here does not."
            ),
        )

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "--no-browser",
            action="store_true",
            help="Don't open the page in a browser. It is printed either way, to open yourself.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help=(
                "Print the page to open and the code to enter there as JSON while the login waits, and then where "
                "the key was stored."
            ),
        )

    async def run_action(self, args: argparse.Namespace) -> int | None:
        api_key = getattr(args, "api_key", None)
        try:
            result = await login(
                open_browser=not args.no_browser, api_key=api_key, on_pending=_print_pending if args.json else None
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # login() has said so. Returned, not raised as SystemExit: asyncio.run may be cancelling this task.
            self.interrupt_reported = True
            return EXIT_INTERRUPTED
        except LoginError as e:
            self.parser.exit(e.exit_code, printable(f"{e}\n"))

        if args.json:
            print_json(result)
        return None
