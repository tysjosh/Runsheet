"""``RESOURCE_ALREADY_EXISTS`` is the 409 a create-if-absent endpoint returns.

Pins the wire value, the explicit status-map entry (not the 500 fallback) and
the factory, so a rename or a missing map entry fails here rather than as a
500 on a duplicate create.
"""

from errors import exceptions
from errors.codes import ERROR_CODE_STATUS_MAP, ErrorCode, get_default_status_code


def test_code_value_matches_member():
    assert ErrorCode.RESOURCE_ALREADY_EXISTS.value == "RESOURCE_ALREADY_EXISTS"


def test_status_map_entry_is_409():
    assert ERROR_CODE_STATUS_MAP[ErrorCode.RESOURCE_ALREADY_EXISTS] == 409
    assert get_default_status_code(ErrorCode.RESOURCE_ALREADY_EXISTS) == 409


def test_factory_binds_code_status_message_and_details():
    exc = exceptions.already_exists("An asset with this id already exists", {"asset_id": "X"})

    assert exc.error_code is ErrorCode.RESOURCE_ALREADY_EXISTS
    assert exc.status_code == 409
    assert exc.message == "An asset with this id already exists"
    assert exc.details == {"asset_id": "X"}


def test_factory_details_default_to_empty():
    exc = exceptions.already_exists("A depot with this id already exists")

    assert exc.details in ({}, None)
