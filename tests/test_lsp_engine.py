"""Tests for LSP Engine, wire framing, connection, and fallback analyzer."""

from __future__ import annotations

import json
import pytest
from coderai.core.lsp.protocol import (
    LspFrameParser,
    encode_lsp_message,
    normalize_wire_hover,
    normalize_wire_location,
    file_to_uri,
    uri_to_file,
)
from coderai.core.lsp.client import LspClient, get_lsp_client
from coderai.core.tools.lsp import handle_lsp_tool


def test_lsp_framing_and_parser():
    parser = LspFrameParser()
    msg1 = {"jsonrpc": "2.0", "id": 1, "result": {"foo": "bar"}}
    msg2 = {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"message": "hello"}}

    raw = encode_lsp_message(msg1) + encode_lsp_message(msg2)
    parsed = parser.feed(raw)

    assert len(parsed) == 2
    assert parsed[0]["id"] == 1
    assert parsed[0]["result"]["foo"] == "bar"
    assert parsed[1]["method"] == "window/logMessage"


def test_lsp_partial_chunks():
    parser = LspFrameParser()
    msg = {"jsonrpc": "2.0", "id": 42, "result": {"data": [1, 2, 3]}}
    raw = encode_lsp_message(msg)

    # Feed in two chunks
    mid = len(raw) // 2
    res1 = parser.feed(raw[:mid])
    assert len(res1) == 0

    res2 = parser.feed(raw[mid:])
    assert len(res2) == 1
    assert res2[0]["id"] == 42
    assert res2[0]["result"]["data"] == [1, 2, 3]


def test_lsp_uri_conversion(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("x = 10", encoding="utf-8")
    uri = file_to_uri(str(f))
    assert uri.startswith("file://")
    restored = uri_to_file(uri)
    assert restored == str(f.resolve())


def test_lsp_location_normalization(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("def test_func():\n    return 42\n", encoding="utf-8")

    raw_loc = {
        "uri": file_to_uri(str(f)),
        "range": {
            "start": {"line": 0, "character": 4},
            "end": {"line": 0, "character": 13},
        },
    }

    norm = normalize_wire_location(raw_loc, project_root=str(tmp_path))
    assert norm is not None
    assert norm.line == 1  # 0-based to 1-based
    assert norm.character == 5
    assert norm.snippet == "def test_func():"


def test_lsp_hover_normalization():
    raw_hover = {
        "contents": {
            "kind": "markdown",
            "value": "```python\ndef test_func() -> int\n```\nDocumentation here",
        }
    }
    norm = normalize_wire_hover(raw_hover)
    assert norm is not None
    assert "def test_func()" in norm.contents
    assert "Documentation here" in norm.contents


def test_lsp_fallback_python_ast(tmp_path):
    src = """class Greeter:
    def greet(self, name: str) -> str:
        \"\"\"Greet a user by name.\"\"\"
        return f"Hello {name}"
"""
    f = tmp_path / "greeter.py"
    f.write_text(src, encoding="utf-8")

    client = LspClient(workspace_root=str(tmp_path))

    # Test goToDefinition
    res_def = client.query("goToDefinition", "greeter.py", 2, 9, project_root=str(tmp_path))
    assert res_def["ok"] is True
    assert len(res_def["locations"]) > 0
    assert res_def["locations"][0]["line"] == 2

    # Test hover
    res_hover = client.query("hover", "greeter.py", 2, 9, project_root=str(tmp_path))
    assert res_hover["ok"] is True
    assert res_hover["hover"] is not None
    assert "Greet a user by name" in res_hover["hover"]["contents"]

    # Test documentSymbol
    res_sym = client.query("documentSymbol", "greeter.py", 1, 1, project_root=str(tmp_path))
    assert res_sym["ok"] is True
    names = [s["name"] for s in res_sym["symbols"]]
    assert "Greeter" in names
    assert "greet" in names


def test_handle_lsp_tool_validation(tmp_path):
    context = type("Ctx", (), {"project_root": str(tmp_path)})()

    # Missing operation
    r1 = handle_lsp_tool({}, context)
    assert r1.ok is False
    assert "Missing required parameter" in r1.error

    # Invalid operation
    r2 = handle_lsp_tool({"operation": "nonExistent", "file_path": "a.py"}, context)
    assert r2.ok is False
    assert "Invalid operation" in r2.error

    # Missing file_path
    r3 = handle_lsp_tool({"operation": "goToDefinition"}, context)
    assert r3.ok is False
    assert "Missing required parameter `file_path`" in r3.error
