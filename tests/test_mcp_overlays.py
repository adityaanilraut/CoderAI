"""MCP layers preserve precedence and distinct settings/overlay normalization."""

from __future__ import annotations

import json
import os

import pytest

from coderai.config import _merge_mcp_server_config
from coderai.mcp.files import collect_cli_mcp_overlays, merge_server_cfg


@pytest.mark.parametrize("via_env", [False, True])
def test_file_and_json_overlay_order_and_warnings(tmp_path, monkeypatch, via_env):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    bad = tmp_path / "bad.json"
    first.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "s": {
                        "command": "old",
                        "env": {"A": "a", "B": "b"},
                        "headers": {"X": "x"},
                        "custom": 1,
                    }
                }
            }
        )
    )
    second.write_text(
        json.dumps({"s": {"command": "new", "env": {"B": "override"}, "headers": {"Y": "y"}}})
    )
    bad.write_text("[]")
    raw = json.dumps({"s": {"url": "https://example.com", "env": {"C": "c"}, "args": []}})
    paths = [str(first), str(bad), str(second)]
    monkeypatch.delenv("CODERAI_MCP_CONFIG_FILES", raising=False)
    monkeypatch.delenv("CODERAI_MCP_CONFIG_JSON", raising=False)
    if via_env:
        monkeypatch.setenv("CODERAI_MCP_CONFIG_FILES", os.pathsep.join(paths))
        monkeypatch.setenv("CODERAI_MCP_CONFIG_JSON", raw)
        merged, warnings = collect_cli_mcp_overlays()
    else:
        monkeypatch.setenv("CODERAI_MCP_CONFIG_JSON", '{"ignored":{"command":"ignored"}}')
        merged, warnings = collect_cli_mcp_overlays(paths, [raw])
    assert merged == {
        "s": {
            "command": "new",
            "url": "https://example.com",
            "env": {"A": "a", "B": "override", "C": "c"},
            "headers": {"X": "x", "Y": "y"},
            "custom": 1,
            "args": [],
        }
    }
    assert warnings == [f"invalid MCP config in {bad}: top-level object required"]


def test_settings_and_overlays_preserve_different_empty_value_policies():
    base = {"command": " old ", "env": {"A": "a", "bad": 1}, "custom": 1, "args": ["x"]}
    over = {"command": " ", "env": {"B": "b"}, "args": [], "enabled": 0}
    assert _merge_mcp_server_config(base, over) == {
        "command": "old",
        "env": {"A": "a", "B": "b"},
        "args": [],
        "enabled": False,
    }
    assert merge_server_cfg(base, over) == {
        "command": " ",
        "env": {"A": "a", "bad": 1, "B": "b"},
        "custom": 1,
        "args": [],
        "enabled": 0,
    }
    assert base["env"] == {"A": "a", "bad": 1}
    assert _merge_mcp_server_config({}, {}) is None
