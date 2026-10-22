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

    def _update_from_response(self, response_data: dict) -> None:
        updated_object = self.model_validate(response_data)
        # The stored fields only: a computed one, which a dump also carries, can't be set.
        for field_name in type(self).model_fields:
            setattr(self, field_name, getattr(updated_object, field_name))

    async def refresh(self) -> None:
        """Refreshes the object instance with the most up to date data."""
        async with api_request.AristotleRequestClient() as client:
            response = await client.get(f"/{self.endpoint_prefix}/{self.object_id}")
            response_data = response.json()
            self._update_from_response(response_data)

    @classmethod
    async def from_id(cls, object_id: str) -> Self:
        """Fetches by id; a 403, a 404, or an id that cannot be one path segment raises AristotleNotFoundError."""
        object_id = str(object_id)  # also accepts a uuid.UUID
        # The path is decoded and dot segments dropped before routing, so encoding cannot carry these.
        if object_id in ("", ".", "..") or "/" in object_id:
            raise api_request.AristotleNotFoundError(cls.endpoint_prefix, object_id)
        async with api_request.AristotleRequestClient() as client:
            try:
                response = await client.get(f"/{cls.endpoint_prefix}/{quote(object_id, safe='')}")
            except api_request.AristotleAPIError as e:
                if e.status_code in (403, 404) and not isinstance(e, api_request._IntermediaryError):
                    raise api_request.AristotleNotFoundError(cls.endpoint_prefix, object_id, e.status_code) from e
                raise
            response_data = response.json()
            return cls.model_validate(response_data)
