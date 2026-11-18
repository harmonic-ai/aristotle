"""Read-only billing responses. Amounts are integer small credits."""

from datetime import datetime
from typing import Annotated, Literal

import pydantic

from aristotlelib.api_request import AristotleAPIError, AristotleRequestClient

CreditState = Literal["disabled", "available", "low", "out_of_credits", "unavailable"]
SmallCredits = Annotated[pydantic.StrictInt, pydantic.Field(ge=0)]


class CreditDisplayConfig(pydantic.BaseModel):
    """The API's resolved display unit; raw amounts remain small credits."""

    model_config = pydantic.ConfigDict(frozen=True)

    small_credits_per_credit: Annotated[pydantic.StrictInt, pydantic.Field(gt=0)]
    decimal_places: Annotated[pydantic.StrictInt, pydantic.Field(ge=0)]


class CreditBalance(pydantic.BaseModel):
    credits: SmallCredits
    paid_credits: SmallCredits
    free_credits: SmallCredits
    granted_credits: SmallCredits
    observed_at: datetime
    next_expiry_at: datetime | None
    credit_display_config: CreditDisplayConfig


class CreditStatus(pydantic.BaseModel):
    state: CreditState


class BalanceResult(pydantic.BaseModel):
    """CLI result; restricted and unavailable balances stay null, never zero."""

    status: Literal["available", "restricted", "unavailable"]
    unit: Literal["small_credits"] = "small_credits"
    balance: CreditBalance | None = None
    credit_status: CreditState | None = None
    error: str | None = None

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def small_credits_per_credit(self) -> int | None:
        return self.balance.credit_display_config.small_credits_per_credit if self.balance is not None else None

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def decimal_places(self) -> int | None:
        return self.balance.credit_display_config.decimal_places if self.balance is not None else None


async def get_credit_balance() -> CreditBalance:
    """Read the API key's acting organization. The API requires organization admin access."""
    async with AristotleRequestClient() as client:
        response = await client.get("/billing/balance")
    try:
        return CreditBalance.model_validate_json(response.content)
    except ValueError:
        raise AristotleAPIError("The API returned an invalid credit balance response.") from None


async def get_credit_status() -> CreditStatus:
    """Read a member-visible classification without an exact numeric balance."""
    async with AristotleRequestClient() as client:
        response = await client.get("/billing/credit-status")
    try:
        return CreditStatus.model_validate_json(response.content)
    except ValueError:
        raise AristotleAPIError("The API returned an invalid credit status response.") from None


def format_credits(small_credits: int, config: CreditDisplayConfig) -> str:
    """Truncate in the returned display unit, without rounding a balance upward."""
    places = config.decimal_places
    precision = 10**places
    displayed = small_credits * precision // config.small_credits_per_credit
    whole, fraction = divmod(displayed, precision)
    amount = f"{whole:,}"
    if places:
        amount += f".{fraction:0{places}d}"
    if displayed == 0 and small_credits > 0:
        return f"<0.{1:0{places}d}" if places else "<1"
    return amount
