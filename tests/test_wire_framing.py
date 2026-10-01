"""Wire adapters preserve JSON-RPC fields, including omitted versus null data."""

from __future__ import annotations

import pytest

from coderai.wire import jsonrpc, server
from coderai.wire.types import StatusUpdate, serialize_wire_message


@pytest.mark.parametrize("request_id", [None, 0, "request-1"])
@pytest.mark.parametrize("result", [None, {}, {"text": "é"}])
def test_success_frames_keep_nulls_and_ids(request_id, result):
    expected = {"jsonrpc": "2.0", "id": request_id, "result": result}
    assert jsonrpc.success_response(request_id, result) == expected
    assert server._success(request_id, result) == expected


@pytest.mark.parametrize("data", [None, False, {}, {"reason": "é"}])
def test_error_adapters_keep_their_null_policy(data):
    error = {"code": -32602, "message": "invalid"}
    canonical_error = {**error, **({"data": data} if data is not None else {})}
    assert jsonrpc.error_response(0, -32602, "invalid", data) == {
        "jsonrpc": "2.0",
        "id": 0,
        "error": canonical_error,
    }
    assert server._error(0, -32602, "invalid", data) == {
        "jsonrpc": "2.0",
        "id": 0,
        "error": {**error, "data": data},
    }


def test_event_and_request_adapters_serialize_models():
    message = StatusUpdate(plan_mode=True)
    payload = serialize_wire_message(message)
    assert server._event(message) == jsonrpc.event_notification(payload)
    assert server._request("id", message) == jsonrpc.request_message("id", payload)
    assert server.IN_METHODS == {
        "initialize",
        "prompt",
        "steer",
        "replay",
        "set_plan_mode",
        "cancel",
    }
    for method in server.IN_METHODS:
        assert jsonrpc.JSONRPCMessage(method=method).method_is_inbound()
