"""The update every record's refresh shares."""

import pydantic

from aristotlelib.aristotle_object import AristotleObject


class _Record(AristotleObject):
    endpoint_prefix = "record"
    record_id: str
    label: str

    @property
    def object_id(self) -> str:
        return self.record_id

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def shout(self) -> str:
        return self.label.upper()


def test_an_update_sets_the_stored_fields_and_the_computed_ones_follow():
    record = _Record(record_id="r", label="old")

    record._update_from_response({"record_id": "r", "label": "new", "shout": "IGNORED"})

    assert (record.label, record.shout) == ("new", "NEW")
