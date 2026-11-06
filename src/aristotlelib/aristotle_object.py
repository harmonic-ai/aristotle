import logging
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import ClassVar
from urllib.parse import quote

import pydantic
from typing_extensions import Self

from aristotlelib import api_request

logger = logging.getLogger("aristotle")


class SafeIntEnum(IntEnum):
    @classmethod
    def _missing_(cls, value):
        logger.warning(f"Unknown {cls.__name__} value '{value}'; treating as UNKNOWN")
        return getattr(cls, "UNKNOWN")


class AristotleObject(ABC, pydantic.BaseModel):
    endpoint_prefix: ClassVar[str]

    @property
    @abstractmethod
    def object_id(self) -> str:
        raise NotImplementedError()

    def _adopt(self, updated: Self) -> None:
        # The stored fields only: a computed one can't be set.
        for field_name in type(self).model_fields:
            setattr(self, field_name, getattr(updated, field_name))

    async def refresh(self) -> None:
        """Refreshes the object instance with the most up to date data."""
        async with api_request.AristotleRequestClient() as client:
            path = f"/{self.endpoint_prefix}/{self.object_id}"
            updated = await client.get(path, read=api_request.json_as(type(self)))
        self._adopt(updated)

    @classmethod
    async def from_id(cls, object_id: str) -> Self:
        """Fetches by id; a 403, a 404, a deleted object's 410, or an id that cannot be one path segment raises
        AristotleNotFoundError."""
        object_id = str(object_id)  # also accepts a uuid.UUID
        # The path is decoded and dot segments dropped before routing, so encoding cannot carry these.
        if object_id in ("", ".", "..") or "/" in object_id:
            raise api_request.AristotleNotFoundError(cls.endpoint_prefix, object_id)
        async with api_request.AristotleRequestClient() as client:
            try:
                path = f"/{cls.endpoint_prefix}/{quote(object_id, safe='')}"
                return await client.get(path, read=api_request.json_as(cls))
            except api_request.AristotleAPIError as e:
                if e.status_code in (403, 404) and not isinstance(e, api_request._IntermediaryError):
                    raise api_request.AristotleNotFoundError(cls.endpoint_prefix, object_id, e.status_code) from e
                if e.status_code == 410 and not isinstance(e, api_request._IntermediaryError):
                    message = f"{cls.endpoint_prefix.capitalize()} {object_id} was deleted."
                    raise api_request.AristotleNotFoundError(cls.endpoint_prefix, object_id, 410, message) from e
                raise
