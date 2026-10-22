"""Declarations (properties and definitions) registered against a project."""

import asyncio
import html
import html.entities
import re
import time
import uuid
from typing import Any

import pydantic

from aristotlelib import api_request
from aristotlelib.aristotle_object import SafeIntEnum
from aristotlelib.date_utils import UTCDatetime

MAX_PAGE_SIZE = 100

_PAGE_INTERVAL = 0.5
"""Seconds from the start of one page to the start of the next when reading every page: at most two a second."""

# Only complete `;`-terminated entities are decoded, as on the web (decodeHTMLStrict); html.unescape alone
# also applies HTML's legacy prefix rules, turning `&not_ready` and `&notready;` into `¬_ready` and `¬ready;`.
_HTML_ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _decode_entity(match: re.Match[str]) -> str:
    entity = match.group(1)
    if entity.startswith("#"):
        hexadecimal = entity[1] in "xX"
        digits = entity[2 if hexadecimal else 1 :].lstrip("0") or "0"
        # U+FFFD past U+10FFFF, as html.unescape gives, without its int() of a number past the digit limit.
        if len(digits) > (6 if hexadecimal else 7):
            return "\ufffd"
        return html.unescape(f"&#{'x' if hexadecimal else ''}{digits};")
    return html.entities.html5.get(f"{entity};", match.group(0))


def decode_html_entities(text: str) -> str:
    return _HTML_ENTITY.sub(_decode_entity, text)


class DeclarationType(SafeIntEnum):
    UNKNOWN = 0
    PROPERTY = 1
    DEFINITION = 2


class DeclarationSource(SafeIntEnum):
    UNKNOWN = 0
    AGENT = 1
    USER = 2


class DeclarationStatus(SafeIntEnum):
    UNKNOWN = 0
    IN_PROGRESS = 1
    NEGATED = 2
    PROVED = 3
    REMOVED = 4
    COUNTEREXAMPLE = 5
    FAILED = 6


class DeclarationFailureReason(SafeIntEnum):
    """Why a property is ``FAILED``: the run stopped first, or the proof it had no longer checks."""

    UNKNOWN = 0
    OUT_OF_BUDGET = 1
    SYSTEM_ERROR = 2
    PROOF_CHECK_FAILED = 3  # Marked proved, but at the run's end its proof was missing, didn't build or used `sorry`.
    INCOMPLETE = 4  # Still in progress when its run ended COMPLETE_WITH_ERRORS.
    CANCELED = 5  # Still in progress when its run was cancelled.


class Declaration(pydantic.BaseModel):
    """A property or definition registered against a project."""

    project_id: str
    declaration_id: str
    declaration_type: DeclarationType
    source: DeclarationSource
    name: str
    status: DeclarationStatus
    failure_reason: DeclarationFailureReason | None = None
    """Set only when :attr:`status` is ``FAILED``; null otherwise."""
    filename: str
    """Path of the artifact Aristotle generated, usually a Lean file."""
    source_file: str | None = None
    """Path in the caller's own tree that the declaration is about; null when there is no single input file."""
    source_line: int | None = None
    """1-based line within :attr:`source_file` as of the uploaded tree; prefer :attr:`source_anchor`."""
    source_anchor: str | None = None
    """Verbatim text of the line at :attr:`source_line`; null when no location was recorded."""
    lean_code: str | None = None
    description: str | None = None
    """Plain text; the server's HTML escaping is undone on validation."""
    artifact_paths: list[str] = []
    """Paths in the project of the files Aristotle linked to it, such as a counterexample's trace; a null reads as []."""
    created_at: UTCDatetime
    last_updated: UTCDatetime

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def declaration_type_name(self) -> str:
        """``declaration_type``'s name: ``PROPERTY``, ``DEFINITION`` or ``UNKNOWN``."""
        return self.declaration_type.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def source_name(self) -> str:
        """``source``'s name: ``AGENT`` for one Aristotle proposed, ``USER`` for one you asked for, or ``UNKNOWN``."""
        return self.source.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def status_name(self) -> str:
        """``status``'s name, such as ``PROVED``; ``UNKNOWN`` for one this version doesn't know."""
        return self.status.name

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def failure_reason_name(self) -> str | None:
        """``failure_reason``'s name, such as ``OUT_OF_BUDGET``; null when it is."""
        return None if self.failure_reason is None else self.failure_reason.name

    @pydantic.field_validator("description")
    @classmethod
    def _decode_description(cls, description: str | None) -> str | None:
        """Decode HTML entities in the description; ``lean_code`` is left as it arrived."""
        return None if description is None else decode_html_entities(description)

    @pydantic.field_validator("artifact_paths", mode="before")
    @classmethod
    def _no_artifacts_for_null(cls, value: Any) -> Any:
        """A null list of artifacts as an empty one."""
        return [] if value is None else value

    @classmethod
    async def list_for_project(
        cls,
        project_id: str,
        declaration_type: DeclarationType | None = None,
        limit: int = 30,
        pagination_key: str | None = None,
    ) -> tuple[list["Declaration"], str | None]:
        """List a project's declarations, most recently updated first.

        Args:
            project_id: The project to list declarations for.
            declaration_type: If given, only return declarations of this type.
            limit: Maximum number of declarations to return. Must be between 1 and 100.
            pagination_key: Key to start from when paginating.

        Returns:
            Tuple of the declarations and the new pagination key.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"Limit must be between 1 and {MAX_PAGE_SIZE}")
        project_uuid = _project_uuid(project_id)
        async with api_request.AristotleRequestClient() as client:
            return await cls._list_page(client, project_uuid, declaration_type, limit, pagination_key)

    @classmethod
    async def list_all_for_project(
        cls,
        project_id: str,
        declaration_type: DeclarationType | None = None,
    ) -> list["Declaration"]:
        """Page through and return every declaration for a project, over one connection, a page at most every 0.5 s."""
        project_uuid = _project_uuid(project_id)
        all_declarations: list[Declaration] = []
        pagination_key: str | None = None
        async with api_request.AristotleRequestClient() as client:
            started: float | None = None
            while True:
                if started is not None:
                    await asyncio.sleep(max(0.0, started + _PAGE_INTERVAL - time.monotonic()))
                started = time.monotonic()
                page, pagination_key = await cls._list_page(
                    client, project_uuid, declaration_type, MAX_PAGE_SIZE, pagination_key
                )
                all_declarations.extend(page)
                if pagination_key is None:
                    return all_declarations

    @classmethod
    async def _list_page(
        cls,
        client: api_request.AristotleRequestClient,
        project_uuid: uuid.UUID,
        declaration_type: DeclarationType | None,
        limit: int,
        pagination_key: str | None,
    ) -> tuple[list["Declaration"], str | None]:
        """One page of the project's declarations, and the key of the next, over an open client."""
        params: dict[str, Any] = {"limit": limit, "pagination_key": pagination_key}
        if declaration_type is not None:
            params["declaration_type"] = declaration_type.value
        response = await client.get(f"/project/{project_uuid}/declarations", params=params)
        response_data = response.json()
        declarations: list[Declaration] = [cls.model_validate(d) for d in response_data["declarations"]]
        new_pagination_key = response_data.get("pagination_key")
        if new_pagination_key is not None and not isinstance(new_pagination_key, str):
            raise TypeError("pagination_key must be a string or None")
        return declarations, new_pagination_key


def _project_uuid(project_id: str) -> uuid.UUID:
    """The project id as a UUID; anything else is reported as not found, without a request."""
    try:
        return uuid.UUID(project_id)
    except ValueError:
        raise api_request.AristotleAPIError(f"Project {project_id!r} not found", status_code=404) from None
