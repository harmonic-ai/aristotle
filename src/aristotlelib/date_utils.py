from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic.functional_serializers import PlainSerializer
from pydantic.functional_validators import AfterValidator


def format_relative_time(dt: datetime) -> str:
    """Format a datetime as relative time from now (e.g., '2 hours ago')."""
    now = datetime.now(timezone.utc)

    # Ensure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt
    seconds = int(delta.total_seconds())

    years = delta.days // 365
    if years > 0:
        return f"{years} year ago" if years == 1 else f"{years} years ago"

    weeks = delta.days // 7
    if weeks > 0:
        return f"{weeks} week ago" if weeks == 1 else f"{weeks} weeks ago"

    days = delta.days
    if days > 0:
        return f"{days} day ago" if days == 1 else f"{days} days ago"

    hours = seconds // 3600
    if hours > 0:
        return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"

    minutes = seconds // 60
    if minutes > 0:
        return f"{minutes} min ago" if minutes == 1 else f"{minutes} mins ago"

    if seconds <= 0:
        return "0 secs ago"
    return f"{seconds} sec ago" if seconds == 1 else f"{seconds} secs ago"


def format_elapsed_time(seconds: int, verb: str = "started") -> str:
    """Format elapsed time duration (e.g., 'started 2m 30s ago' or 'started 1h 15m ago')."""
    if seconds < 60:
        return f"{verb} {seconds}s ago"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{verb} {minutes}m {secs}s ago"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{verb} {hours}h {minutes}m ago"


def as_utc(moment: datetime) -> datetime:
    """``moment`` in UTC; the API's times carry no zone and are UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _naive_utc(moment: datetime) -> datetime:
    """``moment`` as the API sends times: in UTC, without a zone."""
    return moment if moment.tzinfo is None else moment.astimezone(timezone.utc).replace(tzinfo=None)


class _DateTimeJsonSchema:
    """A timestamp's JSON Schema, a date-time string, which the serializer's str would otherwise leave a bare string.
    pydantic's WithJsonSchema would do, but pydantic 2.0 can't hash it inside an Optional."""

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> dict[str, str]:
        return {"type": "string", "format": "date-time"}


UTCDatetime = Annotated[
    datetime,
    AfterValidator(_naive_utc),
    PlainSerializer(lambda moment: as_utc(moment).isoformat(), return_type=str, when_used="json"),
    _DateTimeJsonSchema(),
]
"""A time in UTC: the attribute is without a zone, as the API sends it, even when read back from the CLI's --json,
which writes it with its +00:00."""
