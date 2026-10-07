import json

import pytest
from .fake_fusion import PNG_1X1, FakeFusion

from app.mcp import FusionClient, McpUnreachable, dialog_open, parse_result


def test_handshake_and_session_header(fusion):
    client = FusionClient(fusion.url)
    info = client.connect()
    assert info["serverInfo"] == {"name": "MCP Server Adapter", "version": "1.0.0"}
    assert info["protocolVersion"] == "2025-11-25"
    assert client.session_id and client.session_id in fusion.sessions
    methods = [message.get("method") for message in fusion.requests]
    assert methods == ["initialize", "notifications/initialized"]
    assert fusion.requests[0]["params"]["protocolVersion"] == "2025-11-25"
    assert "id" not in fusion.requests[1]                   # a notification carries no id
    assert [tool["name"] for tool in client.list_tools()] == [
        "fusion_mcp_electronics_read", "fusion_mcp_execute", "fusion_mcp_read", "fusion_mcp_update"]


def test_call_connects_by_itself_and_survives_a_restart(fusion):
    client = FusionClient(fusion.url)
    result = client.call({"name": "fusion_mcp_read", "arguments": {"queryType": "activeCommand"}})
    assert result["ok"] and dialog_open(result) is None
    first = client.session_id
    fusion.drop_sessions()                                  # Fusion restarted: old id is unknown
    again = client.call({"name": "fusion_mcp_read", "arguments": {"queryType": "activeCommand"}})
    assert again["ok"] and client.session_id != first
    assert client.alive()                                   # ping is "method not found": still an answer


def test_unreachable():
    client = FusionClient("http://127.0.0.1:9/mcp", timeout=2)
    with pytest.raises(McpUnreachable):
        client.connect()
    assert client.alive() is False


def test_sse_answers_are_read():
    fake = FakeFusion(sse=True).start()
    try:
        client = FusionClient(fake.url)
        result = client.call({"name": "fusion_mcp_update", "arguments": {"featureType": "undo"}})
        assert result["ok"] is False and result["error"] == "Nothing to undo"
    finally:
        fake.stop()


@pytest.mark.parametrize("blocks", [True, False])
def test_images_in_both_shapes(blocks):
    fake = FakeFusion(image_blocks=blocks).start()
    try:
        result = FusionClient(fake.url).call(
            {"name": "fusion_mcp_read", "arguments": {"queryType": "screenshot", "direction": "front"}})
        assert result["ok"] and result["images"] == [{"mime": "image/png", "data": PNG_1X1}]
        assert PNG_1X1 not in result["text"]
    finally:
        fake.stop()


def _answer(payload, **extra):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}], **extra}}


def test_failure_shapes():
    assert parse_result(_answer({"message": "{\"tool\": \"extrude\"}\n", "success": True}))["text"] == '{"tool": "extrude"}'
    assert parse_result(_answer({"success": False, "error": "Nothing to undo"}))["error"] == "Nothing to undo"
    flagged = parse_result({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "boom"}],
                                                                  "isError": True}})
    assert flagged["ok"] is False and flagged["error"] == "boom"
    rpc = parse_result({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "bad"}})
    assert rpc["ok"] is False and rpc["error"] == "bad"


def test_dialog_shapes():
    assert dialog_open(parse_result(_answer({"activeCommand": None}))) is None
    assert dialog_open(parse_result(_answer({"commandId": "SelectCommand", "commandName": "Select",
                                             "isDefaultCommand": True}))) is None
    assert dialog_open(parse_result(_answer({"commandId": "FilletCommand", "commandName": "Fillet",
                                             "isDefaultCommand": False}))) == "Fillet"
    assert dialog_open(parse_result(_answer({"activeCommand": {"id": "Extrude", "name": "Extrude"}}))) == "Extrude"
    assert dialog_open(parse_result(_answer({"activeCommand": {"name": "Select", "isDefaultCommand": True}}))) is None
