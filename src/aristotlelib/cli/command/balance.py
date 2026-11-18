import argparse
import logging

from aristotlelib.api_request import AristotleAPIError, _IntermediaryError
from aristotlelib.billing import (
    BalanceResult,
    format_credits,
    get_credit_balance,
    get_credit_status,
)
from aristotlelib.cli.api_action import APIAction, exit_code_for, print_json
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR
from aristotlelib.printable import print_line

RESTRICTED = "Exact credit balance is restricted to organization admins."
STATUS_MESSAGES = {
    "available": "Credits are available.",
    "low": "Credits are low.",
    "out_of_credits": "The organization is out of credits.",
    "disabled": "Credit consumption is not billed for this organization.",
    "unavailable": "Credit status is unavailable. Try again later.",
}


class BalanceAction(APIAction):
    @property
    def command_name(self) -> str:
        return "balance"

    @property
    def description(self) -> str:
        return "Read your organization's credit balance without starting a run."

    @classmethod
    def priority(cls) -> int:
        return 52

    def add_action_arguments(self) -> None:
        self.parser.add_argument("--json", action="store_true", help="Print the balance or credit status as JSON.")

    def validate(self, offer_login: bool = True) -> bool:
        # A balance read must not create a key through an automatic login.
        return super().validate(offer_login=False)

    async def run_action(self, args: argparse.Namespace) -> int | None:
        code = None
        try:
            balance = await get_credit_balance()
            result = BalanceResult(status="available", balance=balance)
        except AristotleAPIError as error:
            if error.status_code == 401:
                raise
            if error.status_code in (403, 404, 409) and not isinstance(error, _IntermediaryError):
                try:
                    credit_status = await get_credit_status()
                    restricted = error.status_code == 403
                    result = BalanceResult(
                        status="restricted" if restricted else "unavailable",
                        credit_status=credit_status.state,
                        error=RESTRICTED if restricted else None,
                    )
                    if credit_status.state == "unavailable":
                        code = EXIT_INFRASTRUCTURE_ERROR
                except AristotleAPIError as status_error:
                    if status_error.status_code == 401:
                        raise
                    result = BalanceResult(status="unavailable", error=f"Credit balance unavailable: {status_error}")
                    code = exit_code_for(status_error)
            else:
                result = BalanceResult(status="unavailable", error=f"Credit balance unavailable: {error}")
                code = exit_code_for(error)

        if code is not None:
            logging.error(STATUS_MESSAGES["unavailable"] if result.credit_status == "unavailable" else result.error)
        if args.json:
            print_json(result)
        elif code is None:
            self._print_result(result)
        return code

    @staticmethod
    def _print_result(result: BalanceResult) -> None:
        if result.balance is not None:
            balance = result.balance
            config = balance.credit_display_config
            print(f"Available balance: {format_credits(balance.credits, config)} Credits")
            print(f"  Free: {format_credits(balance.free_credits, config)} Credits")
            print(f"  Purchased: {format_credits(balance.paid_credits, config)} Credits")
            print(f"Granted (active grants): {format_credits(balance.granted_credits, config)} Credits")
            print(f"Credit unit: 1 Credit = {config.small_credits_per_credit:,} small credits")
            print(f"Observed at: {balance.observed_at.isoformat()}")
            if balance.next_expiry_at is not None:
                print(f"Next expiry: {balance.next_expiry_at.isoformat()}")
        if result.error:
            print_line(result.error)
        if result.credit_status is not None:
            print(STATUS_MESSAGES[result.credit_status])
