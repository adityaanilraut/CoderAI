"""UI ↔ agent NDJSON contract: TypeScript `protocol.ts` vs Python `ipc` emitters."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _events_from_typescript_protocol() -> set[str]:
    """Discriminated `event: \"name\"` strings from `ui/src/protocol.ts` (source of truth)."""
    text = (ROOT / "ui/src/protocol.ts").read_text(encoding="utf-8")
    return set(re.findall(r'event:\s*"([a-zA-Z0-9_]+)"', text))


def _emit_event_names_from_python() -> set[str]:
    """First argument to `emit("…")` in IPC code paths that write NDJSON to the UI."""
    names: set[str] = set()
    for rel in (
        "coderAI/ipc/jsonrpc_server.py",
        "coderAI/ipc/streaming.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(
            r'(?:self|server)\.emit\(\s*(?:\n\s*)?"([a-zA-Z0-9_]+)"', text, re.MULTILINE
        ):
            names.add(m.group(1))
    return names


def _agent_event_names_const_from_ts() -> set[str]:
    text = (ROOT / "ui/src/protocol.ts").read_text(encoding="utf-8")
    m = re.search(
        r"export const AGENT_EVENT_NAMES: readonly string\[] = \[(.*?)\];",
        text,
        re.DOTALL,
    )
    assert m, "AGENT_EVENT_NAMES const missing in protocol.ts"
    return set(re.findall(r'"([a-zA-Z0-9_]+)"', m.group(1)))


def test_python_emits_subset_of_typescript_protocol() -> None:
    py = _emit_event_names_from_python()
    ts = _events_from_typescript_protocol()
    const_names = _agent_event_names_const_from_ts()
    assert py, "expected to find emit() calls in ipc sources"
    assert ts, "expected event names in protocol.ts"
    assert const_names == ts, (
        "AGENT_EVENT_NAMES list must match every `event: \"…\"` in the AgentEvent union: "
        f"ts-only {sorted(ts - const_names)} const-only {sorted(const_names - ts)}"
    )
    assert py.issubset(
        ts
    ), f"Python IPC emits event names not declared in protocol.ts: {sorted(py - ts)}"


GOLDEN = ROOT / "tests/fixtures/ndjson_protocol_golden.jsonl"


def test_protocol_markdown_mentions_bespoke_events() -> None:
    """The phased / unified events get easy to overlook in code review.

    Make sure the doc explicitly mentions each one so a contributor looking
    for ``auto_approve_changed`` (the pre-rename name) finds the new
    ``session_patch`` mechanism instead.
    """
    text = (ROOT / "ui/PROTOCOL.md").read_text(encoding="utf-8")
    for ev in ("turn", "tool", "session_patch", "info", "warning", "success"):
        assert f"`{ev}`" in text, f"PROTOCOL.md should document `{ev}` for UI parity"


def test_golden_jsonl_parses_and_events_match_protocol() -> None:
    ts = _events_from_typescript_protocol()
    for line in GOLDEN.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        assert obj.get("v") == 1
        assert obj.get("kind") == "event", obj
        ev = obj.get("event")
        assert isinstance(ev, str) and ev in ts, f"Unknown event: {ev!r}"
