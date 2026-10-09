"""Dispatch Board models and ``parse_body`` (plan task 7; design "External input validation").

One test (or parametrised case) per row of the validation table, plus the
three tray filter params and the no-echo rule for 422s.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.dispatch_board_models import (
    AttemptLoad,
    AttemptRelink,
    BoardDraft,
    CommandBody,
    HistoryQuery,
    Lane,
    LanePublish,
    PublishBody,
    PublishPreviewBody,
    RedispatchAttempt,
    RejectSuggestionBody,
    ValidateBody,
    draft_doc_id,
    is_in_recovery,
    parse_body,
    parse_filter_params,
    parse_lanes_param,
    publish_body_model,
)

CID = "11111111-1111-4111-8111-111111111111"


def _cmd(**fields):
    body = {"client_command_id": CID, "expected_lane_versions": {"T1": 1}, **fields}
    return body


def _err(model, body) -> AppException:
    with pytest.raises(AppException) as info:
        parse_body(model, body)
    return info.value


def test_draft_round_trips_with_every_field():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    draft = BoardDraft.model_validate(
        {
            "tenant_id": "t",
            "service_date": "2026-10-08",
            "timezone": "America/Chicago",
            "draft_version": 3,
            "lanes": {"T1": {"truck_id": "T1", "version": 2}},
            "order_index": {"o1": "T1"},
            "order_ids": ["o1"],
            "acknowledged": {"abcd": {"reason": "ok go", "actor_user_id": "u", "at": now, "truck_id": "T1"}},
            "applied_commands": {CID: {"draft_version": 3, "payload_hash": "h", "at": now}},
            "dismissed_suggestions": {"plan-1": {"actor_user_id": "u", "at": now}},
            "publishes": {CID: "pub-1"},
        }
    )
    again = BoardDraft.model_validate(json.loads(draft.model_dump_json()))
    assert again == draft
    assert draft_doc_id("t", date(2026, 10, 8)) == "t:2026-10-08"


def test_models_forbid_extra_fields():
    with pytest.raises(Exception):
        Lane.model_validate({"truck_id": "T1", "version": 0, "surprise": 1})
    with pytest.raises(Exception):
        BoardDraft.model_validate({"tenant_id": "t", "service_date": "2026-10-08", "timezone": "z", "x": 1})


def test_redispatch_attempt_uses_wire_names_class_and_from():
    attempt = RedispatchAttempt.model_validate(
        {
            "attempt_id": "a1",
            "group_truck_ids": ["T1", "T2"],
            "phase": "stage_relink",
            "loads": {"L1": {"class": "new_revision", "plan_id": "bp-L1-r2", "route_id": "br-L1-r2", "run_id": "bp-L1-r2", "revision": 2}},
            "relinks": [{"order_id": "o1", "from": {"run_id": "r1", "truck_id": "T1", "driver_id": "d1"}, "to": {"run_id": "r2", "truck_id": "T2", "driver_id": "d2"}}],
            "notify_baseline": {"d1": ["o1"]},
            "recovery": "forward",
        }
    )
    dumped = attempt.model_dump(mode="json", by_alias=True)
    assert dumped["loads"]["L1"]["class"] == "new_revision"
    assert dumped["relinks"][0]["from"]["run_id"] == "r1"
    assert isinstance(attempt.loads["L1"], AttemptLoad) and isinstance(attempt.relinks[0], AttemptRelink)
    with pytest.raises(Exception):
        RedispatchAttempt.model_validate({"attempt_id": "a", "group_truck_ids": [], "phase": "bogus"})


def test_lane_in_recovery_rules():
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    attempt = RedispatchAttempt(attempt_id="a", group_truck_ids=["T1"])
    failed = Lane(truck_id="T1", version=1, publish=LanePublish(state="failed", attempt=attempt))
    expired = Lane(truck_id="T1", version=1, publish=LanePublish(state="publishing", attempt=attempt, lease_until=datetime(2026, 10, 8, 11, tzinfo=timezone.utc)))
    live = Lane(truck_id="T1", version=1, publish=LanePublish(state="publishing", attempt=attempt, lease_until=datetime(2026, 10, 8, 13, tzinfo=timezone.utc)))
    plain_failed = Lane(truck_id="T1", version=1, publish=LanePublish(state="failed"))
    assert is_in_recovery(failed, now) and is_in_recovery(expired, now)
    assert not is_in_recovery(live, now) and not is_in_recovery(plain_failed, now)


# ---- the command union --------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "add_lane", "truck_id": "T1"},
        {"type": "remove_lane", "truck_id": "T1"},
        {"type": "pair_driver", "truck_id": "T1", "driver_id": "d1"},
        {"type": "pair_driver", "truck_id": "T1", "driver_id": None},
        {"type": "assign_orders", "order_ids": ["o1", "o2"], "truck_id": "T1", "target": {"load_id": "new", "index": None}},
        {"type": "move_stops", "order_ids": ["o1"], "truck_id": "T1", "target": {"load_id": "L1", "index": 0}},
        {"type": "unassign_orders", "order_ids": ["o1"]},
        {"type": "move_load", "load_id": "L1", "truck_id": "T2", "index": 0},
        {"type": "set_terminal", "load_id": "L1", "terminal_id": None},
        {"type": "set_allocation", "load_id": "L1", "order_id": "o1", "shares": [{"compartment_id": "C1", "liters": 100}]},
        {"type": "set_allocation", "load_id": "L1", "order_id": "o1", "shares": None},
        {"type": "set_load_shift", "load_id": "L1", "shift_id": "night"},
        {"type": "acknowledge_warning", "truck_id": "T1", "warning_id": "0123456789abcdef", "reason": "Customer agreed"},
        {"type": "accept_suggestion", "suggestion_id": "plan-1", "load_ids": ["L1"]},
        {"type": "discard_lane_changes", "truck_id": "T1"},
        {"type": "revert", "target_command_id": CID},
        {"type": "reapply", "target_command_id": CID},
    ],
)
def test_every_command_type_parses(fields):
    command = parse_body(CommandBody, _cmd(**fields)).root
    assert command.type == fields["type"]
    assert command.input_modality == "menu"  # default


def test_missing_client_command_id_is_missing_idempotency_key():
    body = {"type": "add_lane", "truck_id": "T1", "expected_lane_versions": {}}
    exc = _err(CommandBody, body)
    assert exc.error_code == ErrorCode.MISSING_IDEMPOTENCY_KEY and exc.status_code == 422


def test_malformed_client_command_id_is_validation_error():
    exc = _err(CommandBody, _cmd(type="add_lane", truck_id="T1") | {"client_command_id": "not-a-uuid"})
    assert exc.error_code == ErrorCode.VALIDATION_ERROR and exc.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        _cmd(type="add_lane", truck_id="bad id with spaces"),
        _cmd(type="add_lane", truck_id="x" * 129),
        _cmd(type="add_lane", truck_id=""),
        _cmd(type="assign_orders", order_ids=[], truck_id="T1"),
        _cmd(type="assign_orders", order_ids=[f"o{i}" for i in range(26)], truck_id="T1"),
        _cmd(type="assign_orders", order_ids=["o1", "o1"], truck_id="T1"),
        _cmd(type="assign_orders", order_ids=["o1"], truck_id="T1", target={"load_id": "L1", "index": -1}),
        _cmd(type="set_allocation", load_id="L1", order_id="o1", shares=[{"compartment_id": "C1", "liters": 0}]),
        _cmd(type="set_allocation", load_id="L1", order_id="o1", shares=[{"compartment_id": f"C{i}", "liters": 1} for i in range(13)]),
        _cmd(type="set_load_shift", load_id="L1", shift_id="evening"),
        _cmd(type="acknowledge_warning", truck_id="T1", warning_id="0123456789abcdef", reason="no"),
        _cmd(type="acknowledge_warning", truck_id="T1", warning_id="0123456789abcdef", reason="x" * 501),
        _cmd(type="acknowledge_warning", truck_id="T1", warning_id="0123456789abcdef", reason="bad\x07char"),
        _cmd(type="acknowledge_warning", truck_id="T1", warning_id="NOTHEX", reason="fine reason"),
        _cmd(type="add_lane", truck_id="T1", input_modality="telepathy"),
        _cmd(type="add_lane", truck_id="T1", surprise=True),
        {"client_command_id": CID, "type": "add_lane", "truck_id": "T1", "expected_lane_versions": {"T1": -1}},
        {"client_command_id": CID, "type": "add_lane", "truck_id": "T1", "expected_lane_versions": {f"T{i}": 0 for i in range(61)}},
        {"client_command_id": CID, "type": "no_such_command"},
        [1, 2, 3],
        "a string",
        42,
    ],
)
def test_invalid_command_bodies_are_422_validation_error(body):
    exc = _err(CommandBody, body)
    assert exc.error_code == ErrorCode.VALIDATION_ERROR
    assert exc.status_code == 422
    assert set(exc.details) == {"fields", "reason"}


def test_reason_is_trimmed_and_newlines_allowed():
    command = parse_body(
        CommandBody,
        _cmd(type="acknowledge_warning", truck_id="T1", warning_id="0123456789abcdef", reason="  line one\nline two  "),
    ).root
    assert command.reason == "line one\nline two"


def test_422_details_never_echo_submitted_values():
    secret = "SECRET customer note " * 30  # 600 chars, too long
    body = _cmd(type="acknowledge_warning", truck_id="T1", warning_id="0123456789abcdef", reason=secret)
    exc = _err(CommandBody, body)
    dumped = json.dumps(exc.to_dict())
    assert "SECRET" not in dumped
    assert exc.details["fields"] == ["acknowledge_warning.reason"]


# ---- validate, publish, reject, history ----------------------------------


def test_validate_body_rules():
    ok = parse_body(ValidateBody, {"item": {"kind": "order", "ids": ["o1"]}, "candidates": ["T1", "T2"], "position": {"load_id": "L1", "index": 2}})
    assert ok.position.index == 2
    for bad in (
        {"item": {"kind": "order", "ids": ["o1"]}, "candidates": []},
        {"item": {"kind": "order", "ids": ["o1"]}, "candidates": [f"T{i}" for i in range(61)]},
        {"item": {"kind": "planet", "ids": ["o1"]}, "candidates": ["T1"]},
        {"item": {"kind": "order", "ids": []}, "candidates": ["T1"]},
    ):
        assert _err(ValidateBody, bad).error_code == ErrorCode.VALIDATION_ERROR


def test_publish_body_requires_request_id_unless_dry_run():
    lanes = [{"truck_id": "T1", "expected_version": 3}]
    exc = _err(publish_body_model({"lanes": lanes}), {"lanes": lanes})
    assert exc.error_code == ErrorCode.MISSING_IDEMPOTENCY_KEY and exc.status_code == 422
    preview = {"dry_run": True, "lanes": lanes}
    assert publish_body_model(preview) is PublishPreviewBody
    assert parse_body(PublishPreviewBody, preview).dry_run is True
    real = parse_body(PublishBody, {"client_request_id": CID, "lanes": lanes, "warning_reasons": {"0123456789abcdef": "Called ahead"}})
    assert real.dry_run is False


def test_string_true_dry_run_is_a_real_publish():
    body = {"dry_run": "true", "lanes": [{"truck_id": "T1", "expected_version": 1}]}
    model = publish_body_model(body)
    assert model is PublishBody
    assert _err(model, body).error_code == ErrorCode.MISSING_IDEMPOTENCY_KEY


@pytest.mark.parametrize("body", [[{"dry_run": True}], "publish", 7, None])
def test_non_object_publish_body_is_validation_error(body):
    model = publish_body_model(body)
    assert model is PublishBody
    exc = _err(model, body)
    assert exc.error_code == ErrorCode.VALIDATION_ERROR and exc.status_code == 422


def test_publish_lane_limits_and_reasons():
    assert _err(PublishBody, {"client_request_id": CID, "lanes": []}).error_code == ErrorCode.VALIDATION_ERROR
    many = [{"truck_id": f"T{i}", "expected_version": 0} for i in range(61)]
    assert _err(PublishBody, {"client_request_id": CID, "lanes": many}).error_code == ErrorCode.VALIDATION_ERROR
    short = {"client_request_id": CID, "lanes": [{"truck_id": "T1", "expected_version": 0}], "warning_reasons": {"0123456789abcdef": "x"}}
    assert _err(PublishBody, short).error_code == ErrorCode.VALIDATION_ERROR


def test_reject_and_history_bodies():
    assert parse_body(RejectSuggestionBody, {}).reason is None
    assert _err(RejectSuggestionBody, {"reason": "x" * 501}).error_code == ErrorCode.VALIDATION_ERROR
    assert parse_body(HistoryQuery, {"size": "50"}).size == 50
    for bad in ({"size": "51"}, {"size": "0"}, {"truck_id": "bad id"}, {"cursor": "bad cursor|x"}):
        assert _err(HistoryQuery, bad).error_code == ErrorCode.VALIDATION_ERROR


# ---- query params --------------------------------------------------------


def test_lanes_param():
    assert parse_lanes_param(None) is None
    assert parse_lanes_param("T1,T2,T1") == ["T1", "T2"]
    for bad in ("", "T1,bad id", ",".join(f"T{i}" for i in range(61))):
        with pytest.raises(AppException) as info:
            parse_lanes_param(bad)
        assert info.value.status_code == 422


@pytest.mark.parametrize("call_type", ["keep_full", "auto_fill", "will_call", "one_off"])
def test_call_type_filter_accepts_the_four_call_types(call_type):
    assert parse_filter_params(call_type, None, None).call_type == call_type


@pytest.mark.parametrize(
    "call_type,product,window,field",
    [
        ("monthly", None, None, "call_type"),
        (None, "", None, "product"),
        (None, "x" * 33, None, "product"),
        (None, "DIESEL 2", None, "product"),
        (None, "DIESEL;DROP", None, "product"),
        (None, None, "tomorrow", "window"),
    ],
)
def test_bad_tray_filters_are_invalid_filter(call_type, product, window, field):
    with pytest.raises(AppException) as info:
        parse_filter_params(call_type, product, window)
    assert info.value.status_code == 422
    assert info.value.details == {"reason": "invalid_filter", "fields": [field]}


@pytest.mark.parametrize("window", ["overdue", "today", "later"])
def test_window_filter_values(window):
    query = parse_filter_params(None, "DIESEL_2", window)
    assert query.window == window and query.product == "DIESEL_2"
