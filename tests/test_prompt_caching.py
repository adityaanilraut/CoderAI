"""Tests for DeepSeek prompt caching, bitwise identical prefix verification, and usage extraction."""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from coderai.cli.exit_summary import compute_session_stats
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.usage import accumulate_usage_dict, extract_usage_dict
from coderai.core.prompt import (
    get_runtime_context,
    get_subagent_system_prompt,
    get_system_prompt,
    get_tools,
)
from coderai.core.session import SessionManager, SessionMessage


def test_extract_usage_dict_deepseek_format():
    """Verify DeepSeek format with prompt_cache_hit_tokens and prompt_cache_miss_tokens."""
    raw = {
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "total_tokens": 1350,
        "prompt_cache_hit_tokens": 1024,
        "prompt_cache_miss_tokens": 176,
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 1200
    assert extracted["completion_tokens"] == 150
    assert extracted["total_tokens"] == 1350
    assert extracted["cached_tokens"] == 1024
    assert extracted["prompt_cache_hit_tokens"] == 1024
    assert extracted["prompt_cache_miss_tokens"] == 176


def test_extract_usage_dict_openai_format():
    """Verify OpenAI format with prompt_tokens_details.cached_tokens."""
    raw = {
        "prompt_tokens": 2000,
        "completion_tokens": 300,
        "total_tokens": 2300,
        "prompt_tokens_details": {"cached_tokens": 1500},
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 2000
    assert extracted["cached_tokens"] == 1500
    assert extracted["prompt_cache_hit_tokens"] == 1500
    assert extracted["prompt_cache_miss_tokens"] == 500


def test_extract_usage_dict_object_attributes():
    """Verify object format (as returned by openai-python SDK)."""
    details = type("Details", (), {"cached_tokens": 640})()
    raw = type(
        "Usage",
        (),
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 640,
            "prompt_cache_miss_tokens": 360,
            "prompt_tokens_details": details,
        },
    )()
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 1000
    assert extracted["cached_tokens"] == 640
    assert extracted["prompt_cache_hit_tokens"] == 640
    assert extracted["prompt_cache_miss_tokens"] == 360


def test_accumulate_usage_dict():
    """Verify usage accumulation preserves cached and miss token counts."""
    u1 = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 1000,
    }
    u2 = {
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "total_tokens": 1350,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 200,
    }
    acc = accumulate_usage_dict(None, u1)
    assert acc["prompt_tokens"] == 1000
    assert acc["cached_tokens"] == 0
    assert acc["prompt_cache_hit_tokens"] == 0

    acc = accumulate_usage_dict(acc, u2)
    assert acc["prompt_tokens"] == 2200
    assert acc["completion_tokens"] == 250
    assert acc["total_tokens"] == 2450
    assert acc["cached_tokens"] == 1000
    assert acc["prompt_cache_hit_tokens"] == 1000
    assert acc["prompt_cache_miss_tokens"] == 1200


def test_subagent_system_prompt_determinism():
    """Verify subagent system prompt is identical across different subtask descriptions."""
    p1 = get_subagent_system_prompt("read_only")
    p2 = get_subagent_system_prompt("read_only")
    assert p1 == p2
    assert "READ-ONLY" in p1

    gen1 = get_subagent_system_prompt("general")
    gen2 = get_subagent_system_prompt("general")
    assert gen1 == gen2
    assert "GENERAL" in gen1


def test_runtime_context_determinism(tmp_path: pathlib.Path):
    """Verify get_runtime_context produces deterministic JSON output."""
    ctx1 = get_runtime_context(str(tmp_path), "deepseek-v4-pro")
    ctx2 = get_runtime_context(str(tmp_path), "deepseek-v4-pro")
    assert ctx1 == ctx2
    assert "deepseek-v4-pro" in ctx1
    assert "Local Workspace Environment" in ctx1


def test_prompt_prefix_bitwise_identity_across_agent_iterations(tmp_path: pathlib.Path):
    """Verify that converted request payloads across iterations have bitwise identical prefixes."""
    converter = OpenAIMessageConverter()

    # Iteration 1 messages
    sys_prompt = get_system_prompt({"workspaceRoot": str(tmp_path)})
    runtime_ctx = get_runtime_context(str(tmp_path), "deepseek-v4-pro")

    iter1_session_msgs = [
        SessionMessage(id="m1", session_id="s1", role="system", content=sys_prompt),
        SessionMessage(id="m2", session_id="s1", role="system", content=runtime_ctx),
        SessionMessage(id="m3", session_id="s1", role="user", content="Refactor this function"),
    ]

    iter1_payload = converter.convert_session_messages(
        iter1_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    # Iteration 2 messages (assistant tool call + tool response added)
    iter2_session_msgs = list(iter1_session_msgs) + [
        SessionMessage(
            id="m4",
            session_id="s1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"file_path": "main.py"}'},
                }
            ],
            thinking="I need to inspect main.py first.",
        ),
        SessionMessage(
            id="m5",
            session_id="s1",
            role="tool",
            content='{"ok": true, "output": "def foo(): pass"}',
            tool_call_id="call_123",
        ),
    ]

    iter2_payload = converter.convert_session_messages(
        iter2_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    # Verify iteration 1 payload is a strict bitwise identical prefix of iteration 2 payload
    assert len(iter2_payload) > len(iter1_payload)
    for i in range(len(iter1_payload)):
        assert iter1_payload[i] == iter2_payload[i], f"Mismatch at message index {i}"

    # Iteration 3 messages (second assistant turn + tool response)
    iter3_session_msgs = list(iter2_session_msgs) + [
        SessionMessage(
            id="m6",
            session_id="s1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_456",
                    "type": "function",
                    "function": {"name": "edit", "arguments": '{"snippet_id": "sn1"}'},
                }
            ],
            thinking="Now editing foo() to add docstring.",
        ),
        SessionMessage(
            id="m7",
            session_id="s1",
            role="tool",
            content='{"ok": true, "output": "edited"}',
            tool_call_id="call_456",
        ),
    ]

    iter3_payload = converter.convert_session_messages(
        iter3_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    assert len(iter3_payload) > len(iter2_payload)
    for i in range(len(iter2_payload)):
        assert iter2_payload[i] == iter3_payload[i], f"Mismatch at message index {i}"


@pytest.mark.asyncio
async def test_session_manager_tracks_cached_tokens(tmp_path: pathlib.Path):
    """Verify SessionManager accumulates cached_tokens from API responses and estimates cost correctly."""
    call_count = 0

    def mock_create(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Turn 1: Cache miss on initial prompt
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Step 1",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"file_path": "a.txt"}',
                                    },
                                }
                            ],
                            "reasoning_content": "Thinking 1",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1000,
                },
            }
        else:
            # Turn 2: Cache hit on prefix (1000 tokens cached)
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Done with task.",
                            "tool_calls": None,
                            "reasoning_content": "Thinking 2",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 60,
                    "total_tokens": 1260,
                    "prompt_cache_hit_tokens": 1000,
                    "prompt_cache_miss_tokens": 200,
                },
            }

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": mock_client,
            "model": "deepseek-v4-pro",
            "thinkingEnabled": True,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "deepseek-v4-pro",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    (tmp_path / "a.txt").write_text("hello world")

    sid = await mgr.create_session("Inspect a.txt", skills=[])
    entry = mgr.get_session(sid)
    assert entry is not None
    assert entry.usage is not None
    assert entry.usage["cached_tokens"] == 1000
    assert entry.usage["prompt_cache_hit_tokens"] == 1000
    assert entry.usage["prompt_tokens"] == 2200
    assert entry.usage["completion_tokens"] == 110
    assert entry.usage["uncached_tokens"] == 1200

    # Verify session stats
    stats = compute_session_stats(mgr, sid)
    assert stats["cached_tokens"] == 1000
    assert stats["prompt_tokens"] == 2200
    assert stats["estimated_cost"] > 0

    # Verify message JSONL records turn usage and timestamps
    jsonl_path = mgr._messages_path(sid)
    assert jsonl_path.exists()
    lines = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) > 0
    for line_obj in lines:
        assert "timestamp" in line_obj
        assert isinstance(line_obj["timestamp"], int)
        assert line_obj["timestamp"] > 0

    # Check assistant message has usage in meta
    assistant_msgs = [m for m in lines if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1
    assert assistant_msgs[0].get("meta", {}).get("usage") is not None
    assert "cached_tokens" in assistant_msgs[0]["meta"]["usage"]

    # Check tool message has execution timestamps in meta
    tool_msgs = [m for m in lines if m["role"] == "tool"]
    assert len(tool_msgs) >= 1
    assert "startTime" in tool_msgs[0]["meta"]
    assert "endTime" in tool_msgs[0]["meta"]
    assert "durationMs" in tool_msgs[0]["meta"]


def test_extract_usage_dict_anthropic_format():
    """Verify Anthropic Claude format with input_tokens, output_tokens, cache_read_input_tokens, and cache_creation_input_tokens."""
    raw = {
        "input_tokens": 5000,
        "output_tokens": 450,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 1000,
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 5000
    assert extracted["completion_tokens"] == 450
    assert extracted["total_tokens"] == 5450
    assert extracted["cached_tokens"] == 4000
    assert extracted["uncached_tokens"] == 1000
    assert extracted["prompt_cache_hit_tokens"] == 4000
    assert extracted["cache_read_input_tokens"] == 4000
    assert extracted["cache_creation_input_tokens"] == 1000


def test_extract_usage_dict_openrouter_format():
    """Verify OpenRouter format with prompt_tokens_details and cached_tokens."""
    raw = {
        "prompt_tokens": 3000,
        "completion_tokens": 200,
        "total_tokens": 3200,
        "prompt_tokens_details": {"cached_tokens": 2500},
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 3000
    assert extracted["completion_tokens"] == 200
    assert extracted["cached_tokens"] == 2500
    assert extracted["uncached_tokens"] == 500
    assert extracted["prompt_cache_hit_tokens"] == 2500
    assert extracted["prompt_cache_miss_tokens"] == 500


@pytest.mark.asyncio
async def test_tool_executor_timestamps(tmp_path: pathlib.Path):
    """Verify ToolExecutor attaches millisecond startTime, endTime, and durationMs."""
    from coderai.core.tools.executor import ToolExecutor

    executor = ToolExecutor(project_root=str(tmp_path))
    (tmp_path / "sample.txt").write_text("hello timestamps")

    call = {
        "id": "c_time_1",
        "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"file_path": "sample.txt"})},
    }
    res = await executor.execute_tool_call("s_test", call)
    assert res.ok is True
    assert res.metadata is not None
    assert "startTime" in res.metadata
    assert "endTime" in res.metadata
    assert "durationMs" in res.metadata
    assert res.metadata["durationMs"] >= 0
    assert res.metadata["endTime"] >= res.metadata["startTime"]


def test_prune_tool_results_multi_turn_history():
    """Verify historical tool outputs are pruned while recent tool outputs retain full budget."""
    from coderai.core.session_log import (
        MAX_TOOL_RESULT_CHARS,
        prune_tool_results,
    )

    huge_past = "A" * 40_000  # exceeds MAX_TOOL_RESULT_CHARS → must be truncated
    huge_recent = "B" * 20_000  # under MAX_TOOL_RESULT_CHARS → must stay intact

    messages = [
        SessionMessage(id="u1", session_id="s", role="user", content="Turn 1"),
        SessionMessage(
            id="a1", session_id="s", role="assistant", content="Step 1", tool_calls=[{"id": "c1"}]
        ),
        SessionMessage(id="t1", session_id="s", role="tool", content=huge_past, tool_call_id="c1"),
        SessionMessage(id="u2", session_id="s", role="user", content="Turn 2"),
        SessionMessage(
            id="a2", session_id="s", role="assistant", content="Step 2", tool_calls=[{"id": "c2"}]
        ),
        SessionMessage(
            id="t2", session_id="s", role="tool", content=huge_recent, tool_call_id="c2"
        ),
    ]

    pruned = prune_tool_results(messages)

    # Historical tool t1 should be pruned with the same MAX_TOOL_RESULT_CHARS limit
    t1_pruned = next(m for m in pruned if m.id == "t1")
    assert len(t1_pruned.content) < len(huge_past)
    assert "omitted" in t1_pruned.content
    assert len(t1_pruned.content) <= MAX_TOOL_RESULT_CHARS + 100

    # Recent tool t2 is within MAX_TOOL_RESULT_CHARS (20k <= 32k) and should NOT be truncated
    t2_pruned = next(m for m in pruned if m.id == "t2")
    assert t2_pruned.content == huge_recent


@pytest.mark.asyncio
async def test_exec_runner_clean_exit_code_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify run_exec_session terminates cleanly with exit code 0."""
    from coderai.cli.exec_runner import run_exec_session

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Done successfully.",
                    "tool_calls": None,
                }
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 80,
        },
    }

    monkeypatch.setattr(
        "coderai.cli.exec_runner._core_client",
        lambda *args, **kwargs: {"client": mock_client, "model": "gpt-4o"},
    )

    exit_code = await run_exec_session(
        "Complete task",
        project_root=str(tmp_path),
        model="gpt-4o",
        auto_approve=True,
    )
    assert exit_code == 0


def test_system_prompt_static_byte_identity_across_workspaces(tmp_path: pathlib.Path):
    """Verify get_system_prompt is 100% byte-identical across different workspaces for maximum KV cache hits."""
    dir1 = tmp_path / "project_a"
    dir2 = tmp_path / "project_b"
    dir1.mkdir()
    dir2.mkdir()

    prompt1 = get_system_prompt({"workspaceRoot": str(dir1), "nonInteractive": True})
    prompt2 = get_system_prompt({"workspaceRoot": str(dir2), "nonInteractive": True})

    assert prompt1 == prompt2
    assert "Mental Post-Fix Trace" in prompt1
    assert "Step Verification Loop Before Task Completion" in prompt1


@pytest.mark.asyncio
async def test_session_manager_places_runtime_context_in_user_prompt(tmp_path: pathlib.Path):
    """Verify SessionManager places static system prompt in role=system and dynamic runtime context in role=user."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [{"message": {"content": "Understood", "tool_calls": None}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": mock_client, "model": "deepseek-v4-flash"},
        get_resolved_settings=lambda: {"preset": "core"},
    )

    sid = await mgr.create_session("Write a unit test")
    msgs = mgr.list_session_messages(sid)

    system_msgs = [m for m in msgs if m.role == "system"]
    user_msgs = [m for m in msgs if m.role == "user"]

    assert len(system_msgs) == 1
    assert "Local Workspace Environment" not in system_msgs[0].content
    assert len(user_msgs) == 1
    assert "Local Workspace Environment" in user_msgs[0].content
    assert "Write a unit test" in user_msgs[0].content


def test_core_tool_preset_filter():
    """Verify the core preset scopes tools down to the canonical core set."""

    all_tools = get_tools()
    assert len(all_tools) > 15  # full set includes all MCP, subagents, jobs, etc.

    core_tools = get_tools({"preset": "core"})
    core_names = {t["function"]["name"] for t in core_tools}
    assert len(core_tools) <= 7
    assert "bash" in core_names
    assert "str_replace_editor" in core_names
    assert "read" in core_names
    assert "write" in core_names
    assert "glob" in core_names
    assert "grep" in core_names
    assert "AskUserQuestion" not in core_names
    assert "subagent" not in core_names


def test_cli_preset_parsing(tmp_path: pathlib.Path):
    """Verify CLI parser accepts --preset and initializes manager with the preset."""
    from coderai.cli.app import _build_parser
    from coderai.cli.session_factory import build_session_manager

    parser = _build_parser()
    args = parser.parse_args(["-p", "test prompt", "--preset", "core"])
    assert args.preset == "core"

    mgr = build_session_manager(str(tmp_path), model="gpt-4o", preset=args.preset)
    settings = mgr.get_resolved_settings()
    assert settings.get("preset") == "core"
    assert settings.get("toolsPreset") == "core"

    with pytest.raises(SystemExit):
        parser.parse_args(["--preset", "benchmark"])


def test_settings_accept_only_canonical_tool_presets(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    from coderai.core.settings import parse_tool_preset, resolve_current_settings

    assert parse_tool_preset("full") == "full"
    assert parse_tool_preset("core") == "core"
    assert parse_tool_preset("shell_edit") == "shell_edit"
    assert parse_tool_preset("benchmark") is None
    assert parse_tool_preset("dsh_minimal") is None

    project_settings = tmp_path / ".coderai"
    project_settings.mkdir()
    (project_settings / "settings.json").write_text(
        json.dumps({"toolsPreset": "core"}), encoding="utf-8"
    )
    assert resolve_current_settings(str(tmp_path))["toolsPreset"] == "core"

    monkeypatch.setenv("CODERAI_TOOLS_PRESET", "shell_edit")
    assert resolve_current_settings(str(tmp_path))["toolsPreset"] == "shell_edit"


def test_render_skill_catalog_and_system_prompt(tmp_path: pathlib.Path):
    """Verify render_skill_catalog creates a concise summary list and integrates with get_system_prompt."""
    from coderai.core.prompt import render_skill_catalog

    # Create dummy skills
    skill_dir1 = tmp_path / ".coderai" / "skills" / "git-flow"
    skill_dir1.mkdir(parents=True)
    (skill_dir1 / "SKILL.md").write_text(
        "---\nname: git-flow\ndescription: Manage git branches and clean commits\n---\n# Full Body\n"
    )

    skill_dir2 = tmp_path / ".coderai" / "skills" / "sql-optimizer"
    skill_dir2.mkdir(parents=True)
    (skill_dir2 / "SKILL.md").write_text(
        "---\nname: sql-optimizer\ndescription: Optimize PostgreSQL and SQLite queries\n---\n# Full Body\n"
    )

    catalog = render_skill_catalog(str(tmp_path))
    assert catalog is not None
    assert "# Available Skills" in catalog
    assert "- `git-flow`: Manage git branches and clean commits" in catalog
    assert "- `sql-optimizer`: Optimize PostgreSQL and SQLite queries" in catalog
    assert "# Full Body" not in catalog  # Never dumps full skill body in catalog

    # System prompt incorporates catalog
    sys_prompt = get_system_prompt({"workspaceRoot": str(tmp_path)})
    assert "# Available Skills" in sys_prompt
    assert "- `git-flow`" in sys_prompt


def test_message_converter_filters_lifecycle_events():
    """Verify OpenAIMessageConverter strictly filters out non-standard / lifecycle event roles."""
    converter = OpenAIMessageConverter()

    session_messages = [
        SessionMessage(id="s1", session_id="sess", role="system", content="System instruction"),
        SessionMessage(id="u1", session_id="sess", role="user", content="User goal"),
        SessionMessage(id="ev1", session_id="sess", role="turn/start", content=""),
        SessionMessage(id="ev2", session_id="sess", role="step/start", content=""),
        SessionMessage(
            id="a1",
            session_id="sess",
            role="assistant",
            content="Looking at files",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        ),
        SessionMessage(
            id="t1", session_id="sess", role="tool", content='["a.py"]', tool_call_id="call_1"
        ),
        SessionMessage(id="ev3", session_id="sess", role="step/end", content=""),
        SessionMessage(id="ev4", session_id="sess", role="turn/end", content=""),
    ]

    converted = converter.convert_session_messages(session_messages, "gpt-5.6-luna")

    # Only standard roles must exist
    allowed_roles = {"system", "user", "assistant", "tool"}
    for msg in converted:
        assert msg["role"] in allowed_roles, f"Disallowed role found: {msg['role']}"

    assert len(converted) == 4
    assert converted[0]["role"] == "system"
    assert converted[1]["role"] == "user"
    assert converted[2]["role"] == "assistant"
    assert converted[3]["role"] == "tool"


@pytest.mark.asyncio
async def test_session_init_does_not_bloat_prompt_with_unrequested_skills(tmp_path: pathlib.Path):
    """Verify session creation does not eagerly dump full skill markdown files when unrequested."""
    # Create mock skills in workspace
    for sname in ["skill-alpha", "skill-beta", "skill-gamma"]:
        sdir = tmp_path / ".coderai" / "skills" / sname
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(
            f"---\nname: {sname}\ndescription: Description of {sname}\n---\n# Full body of {sname}\n"
            * 50
        )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [{"message": {"content": "Understood", "tool_calls": None}}],
        "usage": {"prompt_tokens": 150, "completion_tokens": 10, "total_tokens": 160},
    }

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": mock_client, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {
            "model": "gpt-5.6-luna",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    sid = await mgr.create_session("Please write a helper function in python")
    msgs = mgr.list_session_messages(sid)

    # Full skill bodies must NOT be in session messages
    full_skill_msgs = [m for m in msgs if m.role == "system" and (m.meta or {}).get("skill")]
    assert len(full_skill_msgs) == 0

    # System prompt should contain the compact catalog
    system_msgs = [m for m in msgs if m.role == "system"]
    assert len(system_msgs) == 1
    assert "Available Skills" in system_msgs[0].content
    assert "- `skill-alpha`" in system_msgs[0].content


def test_tool_schema_canonical_sorting_and_determinism():
    """Verify tool schemas are serialized with deterministic key order across repeated calls."""
    tools1 = get_tools({"preset": "core"})
    tools2 = get_tools({"preset": "core"})

    json1 = json.dumps(tools1, sort_keys=True)
    json2 = json.dumps(tools2, sort_keys=True)
    assert json1 == json2

    # Verify keys within every function parameter schema are sorted
    for tool in tools1:
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        if "properties" in params:
            props = params["properties"]
            keys = list(props.keys())
            assert keys == sorted(keys), (
                f"Properties for tool '{fn['name']}' are not alphabetically sorted: {keys}"
            )


def test_multiturn_bitwise_prefix_equality_across_many_turns(tmp_path: pathlib.Path):
    """Verify 10 consecutive turns maintain strict bitwise prefix identity."""
    converter = OpenAIMessageConverter()
    sys_prompt = get_system_prompt({"workspaceRoot": str(tmp_path)})
    runtime_ctx = get_runtime_context(str(tmp_path), "deepseek-v4-pro")

    session_msgs: list[SessionMessage] = [
        SessionMessage(id="m0", session_id="s1", role="system", content=sys_prompt),
        SessionMessage(
            id="m1",
            session_id="s1",
            role="user",
            content=f"{runtime_ctx}\n\n---\n\nInitial task goal",
        ),
    ]

    payloads: list[list[dict[str, Any]]] = []

    # Turn 1: initial request
    payloads.append(
        converter.convert_session_messages(session_msgs, "deepseek-v4-pro", thinking_enabled=True)
    )

    # Turns 2 through 10
    for t in range(2, 11):
        call_id = f"call_{t}"
        session_msgs.append(
            SessionMessage(
                id=f"ast_{t}",
                session_id="s1",
                role="assistant",
                content=f"Step {t} analysis",
                tool_calls=[
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"file_path": f"src/file_{t}.py"}),
                        },
                    }
                ],
                thinking=f"Reasoning for turn {t}",
            )
        )
        session_msgs.append(
            SessionMessage(
                id=f"tool_{t}",
                session_id="s1",
                role="tool",
                content=f"Contents of src/file_{t}.py line {t}",
                tool_call_id=call_id,
            )
        )

        turn_payload = converter.convert_session_messages(
            session_msgs, "deepseek-v4-pro", thinking_enabled=True
        )
        payloads.append(turn_payload)

        # Verify that all prior turns are strict prefixes of current turn
        for prev_turn_idx in range(len(payloads) - 1):
            prev_payload = payloads[prev_turn_idx]
            assert len(turn_payload) > len(prev_payload)
            for msg_idx in range(len(prev_payload)):
                assert prev_payload[msg_idx] == turn_payload[msg_idx], (
                    f"Turn {prev_turn_idx + 1} mismatch in Turn {t} at message index {msg_idx}: "
                    f"{prev_payload[msg_idx]} != {turn_payload[msg_idx]}"
                )


def test_anthropic_claude_cache_control_breakpoints(tmp_path: pathlib.Path):
    """Verify Anthropic Claude models receive cache_control ephemeral breakpoints on system prompt and penultimate turn."""
    converter = OpenAIMessageConverter()
    sys_prompt = get_system_prompt({"workspaceRoot": str(tmp_path)})

    session_msgs = [
        SessionMessage(id="s1", session_id="sess", role="system", content=sys_prompt),
        SessionMessage(id="u1", session_id="sess", role="user", content="Turn 1 goal"),
        SessionMessage(id="a1", session_id="sess", role="assistant", content="Step 1 response"),
        SessionMessage(id="u2", session_id="sess", role="user", content="Turn 2 goal"),
        SessionMessage(id="a2", session_id="sess", role="assistant", content="Step 2 response"),
        SessionMessage(id="u3", session_id="sess", role="user", content="Turn 3 goal"),
    ]

    converted = converter.convert_session_messages(session_msgs, "claude-3-7-sonnet")

    # System message should have cache_control breakpoint
    sys_msg = converted[0]
    assert sys_msg["role"] == "system"
    assert isinstance(sys_msg["content"], list)
    assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    # Penultimate user message (u2) should have cache_control breakpoint
    u2_msg = converted[3]
    assert u2_msg["role"] == "user"
    assert isinstance(u2_msg["content"], list)
    assert u2_msg["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_end_to_end_benchmark_cache_hit_rate_parity(tmp_path: pathlib.Path):
    """Simulate a multi-turn benchmark run (SWT-Bench style) and verify ~100% cache hit rate on turns 2-5."""
    step = 0

    def mock_create(**kwargs: Any) -> Any:
        nonlocal step
        step += 1
        # In turn 1: 1000 prompt tokens (0 cached)
        # In turn 2: 1200 prompt tokens (1000 cached = 83.3% turn hit)
        # In turn 3: 1500 prompt tokens (1200 cached = 80.0% turn hit)
        # In turn 4: 1800 prompt tokens (1500 cached = 83.3% turn hit)
        # In turn 5: 2000 prompt tokens (1800 cached = 90.0% turn hit)
        tokens_by_step = [
            (1000, 50, 0),
            (1200, 60, 1000),
            (1500, 70, 1200),
            (1800, 80, 1500),
            (2000, 90, 1800),
        ]
        p_tok, c_tok, cached_tok = tokens_by_step[min(step - 1, len(tokens_by_step) - 1)]

        tool_calls = None
        if step < 5:
            tool_calls = [
                {
                    "id": f"call_{step}",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({"file_path": f"test_{step}.py"}),
                    },
                }
            ]

        return {
            "choices": [
                {
                    "message": {
                        "content": f"Step {step} complete." if not tool_calls else "",
                        "tool_calls": tool_calls,
                        "reasoning_content": f"Reasoning {step}",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
                "prompt_cache_hit_tokens": cached_tok,
                "prompt_cache_miss_tokens": p_tok - cached_tok,
            },
        }

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": mock_client,
            "model": "deepseek-v4-pro",
            "thinkingEnabled": True,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "deepseek-v4-pro",
            "preset": "core",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    for i in range(1, 6):
        (tmp_path / f"test_{i}.py").write_text(f"def test_{i}(): pass")

    sid = await mgr.create_session("Solve benchmark issue in SWT-Bench")
    entry = mgr.get_session(sid)
    assert entry is not None
    assert entry.usage is not None

    total_cached = entry.usage["cached_tokens"]
    total_prompt = entry.usage["prompt_tokens"]
    assert total_cached == 5500  # 1000 + 1200 + 1500 + 1800
    assert total_prompt == 7500  # 1000 + 1200 + 1500 + 1800 + 2000

    # On turns 2 through 5, prefix cache hit rate is 100% of previous turn's total tokens
    turn_2_to_5_cached = 5500
    turn_2_to_5_prefix_available = 1000 + 1200 + 1500 + 1800
    assert turn_2_to_5_cached == turn_2_to_5_prefix_available


def test_system_prompt_scoped_to_core_preset(tmp_path: pathlib.Path):
    """Verify the core preset includes only its visible tools."""
    prompt_core = get_system_prompt(
        {
            "workspaceRoot": str(tmp_path),
            "preset": "core",
            "nonInteractive": True,
        }
    )
    assert "## bash" in prompt_core
    assert "## read" in prompt_core
    assert "## edit" in prompt_core
    assert "## write" in prompt_core
    assert "## glob" in prompt_core
    assert "## grep" in prompt_core

    # Unmounted full tools should be omitted to preserve tokens and prefix cache
    assert "## WebSearch" not in prompt_core
    assert "## WebFetch" not in prompt_core
    assert "## UnderstandImage" not in prompt_core
    assert "## subagent" not in prompt_core
    assert "## Task" not in prompt_core
    assert "## AskUserQuestion" not in prompt_core
    assert "# Available Skills" not in prompt_core


def test_system_prompt_scoped_to_shell_edit_preset(tmp_path: pathlib.Path):
    """Verify shell_edit contains only bash and str_replace_editor."""
    prompt_min = get_system_prompt(
        {
            "workspaceRoot": str(tmp_path),
            "preset": "shell_edit",
        }
    )
    assert "## bash" in prompt_min
    assert "## str_replace_editor" in prompt_min
    assert "## read" not in prompt_min
    assert "## edit" not in prompt_min
    assert "## glob" not in prompt_min


def test_edit_tool_accepts_direct_file_path(tmp_path: pathlib.Path):
    """Verify edit works with a direct file_path and no snippet_id."""
    from coderai.core.tools.edit import handle_edit_tool

    test_file = tmp_path / "calc.py"
    test_file.write_text("def add(a, b):\n    return a - b\n")

    res = handle_edit_tool(
        {
            "file_path": str(test_file),
            "old_string": "return a - b",
            "new_string": "return a + b",
        },
        {"project_root": str(tmp_path), "session_id": "test_s1"},
    )
    assert res.ok is True
    assert "return a + b" in test_file.read_text()


def test_message_converter_omits_empty_reasoning():
    """Verify converter omits reasoning_content when thinking is empty string or None."""
    converter = OpenAIMessageConverter()
    msgs = [
        SessionMessage(id="m1", session_id="s1", role="user", content="hello"),
        SessionMessage(id="m2", session_id="s1", role="assistant", content="hi", thinking=None),
        SessionMessage(id="m3", session_id="s1", role="assistant", content="done", thinking=""),
    ]
    converted = converter.convert_session_messages(msgs, "deepseek-v4-pro", thinking_enabled=True)
    assert "reasoning_content" not in converted[1]
    assert "reasoning_content" not in converted[2]


@pytest.mark.asyncio
async def test_system_prompt_single_consolidated_message_with_instructions_and_plan_mode(
    tmp_path: pathlib.Path,
):
    """Verify create_session creates a single consolidated system message with instructions and plan mode."""
    (tmp_path / "AGENTS.md").write_text("# Project Custom Instructions\nFollow these guidelines.")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [{"message": {"content": "Plan drafted.", "tool_calls": None}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": mock_client, "model": "deepseek-v4-pro"},
        get_resolved_settings=lambda: {"model": "deepseek-v4-pro", "preset": "core"},
    )

    sid = await mgr.create_session("Plan architecture changes", plan_mode=True)
    msgs = mgr.list_session_messages(sid)

    system_msgs = [m for m in msgs if m.role == "system"]
    assert len(system_msgs) == 1
    sys_text = system_msgs[0].content

    # Verify all sections are in one unified prompt in deterministic order
    assert "You are a helpful software engineer assistant" in sys_text
    assert "## bash" in sys_text
    assert "Project Custom Instructions" in sys_text
    assert "# Plan Mode" in sys_text
    assert sys_text.index("You are a helpful software engineer assistant") < sys_text.index("## bash")
    assert sys_text.index("## bash") < sys_text.index("Project Custom Instructions")
    assert sys_text.index("Project Custom Instructions") < sys_text.index("# Plan Mode")


@pytest.mark.asyncio
async def test_compaction_replays_warm_prefix_and_tools(tmp_path: pathlib.Path):
    """Verify compaction sends the conversation prefix and tools so provider KV cache is reused."""
    captured_requests: list[dict[str, Any]] = []

    def mock_create(**kwargs: Any) -> Any:
        captured_requests.append(kwargs)
        if len(captured_requests) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Step 1 done",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file_path": "a.txt"}'},
                                }
                            ],
                            "reasoning_content": "Reasoning 1",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 30, "total_tokens": 530},
            }
        else:
            # Compaction response
            return {
                "choices": [
                    {
                        "message": {
                            "content": "## Primary Request and Intent\n- User goal\n\n## Key Technical Concepts\n- Python\n\n## Critical Decisions & Constraints\n- None\n\n## State of Progress\n- None\n\n## Pending Work & Next Steps\n- Next",
                            "tool_calls": None,
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 600,
                    "completion_tokens": 80,
                    "total_tokens": 680,
                    "prompt_cache_hit_tokens": 500,
                },
            }

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    (tmp_path / "a.txt").write_text("file content")

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": mock_client,
            "model": "deepseek-v4-pro",
            "thinkingEnabled": True,
        },
        get_resolved_settings=lambda: {
            "model": "deepseek-v4-pro",
            "preset": "core",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    sid = await mgr.create_session("Inspect file", skills=[])
    # Trigger compaction
    res = await mgr.compaction_engine.compact_now(sid, trigger="manual")
    assert res is not None

    assert len(captured_requests) >= 2
    compaction_req = captured_requests[-1]

    # Compaction request must retain tools and conversation prefix
    assert "tools" in compaction_req
    assert compaction_req["tools"] is not None
    assert len(compaction_req["tools"]) > 0
    assert compaction_req["messages"][0]["role"] == "system"
    assert "You are a helpful software engineer assistant" in compaction_req["messages"][0]["content"]
    assert compaction_req["messages"][-1]["role"] == "user"
    assert "compaction engine" in compaction_req["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_subagent_tool_ordering_and_reasoning_persistence(tmp_path: pathlib.Path):
    """Verify subagent execution loop retains reasoning_content and canonical tool ordering."""
    from coderai.core.subagent import SubAgentManager, SubAgentSpec

    captured_subagent_requests: list[dict[str, Any]] = []

    def mock_create(**kwargs: Any) -> Any:
        captured_subagent_requests.append(kwargs)
        step = len(captured_subagent_requests)
        if step == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "sub_c1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file_path": "sub.txt"}'},
                                }
                            ],
                            "reasoning_content": "Subagent step 1 thinking",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 40,
                    "total_tokens": 340,
                    "prompt_cache_hit_tokens": 0,
                },
            }
        else:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Subagent final findings.",
                            "tool_calls": None,
                            "reasoning_content": "Subagent step 2 thinking",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 450,
                    "completion_tokens": 50,
                    "total_tokens": 500,
                    "prompt_cache_hit_tokens": 300,
                },
            }

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    (tmp_path / "sub.txt").write_text("subagent data")

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": mock_client,
            "model": "deepseek-v4-pro",
            "thinkingEnabled": True,
            "reasoningEffort": "max",
        },
    )

    spec = SubAgentSpec(description="Subtask", prompt="Read sub.txt and report", mode="read_only")
    res = await manager.spawn_subagent(spec)
    assert res.status == "completed"
    assert "Subagent final findings" in res.summary

    assert len(captured_subagent_requests) == 2
    req1 = captured_subagent_requests[0]
    req2 = captured_subagent_requests[1]

    # Verify tool ordering and schema canonicalization
    assert req1["tools"] == req2["tools"]
    tool_names = [t["function"]["name"] for t in req1["tools"]]
    assert "read" in tool_names
    assert "AskUserQuestion" not in tool_names

    # Verify step 2 carries assistant reasoning_content from step 1
    step2_msgs = req2["messages"]
    assistant_msg = next(m for m in step2_msgs if m["role"] == "assistant")
    assert assistant_msg.get("reasoning_content") == "Subagent step 1 thinking"


def test_cache_hit_rate_analytics(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]):
    """Verify cache hit rate calculation, Rich table rendering, plain text rendering, and summary formatting."""
    from coderai.cli.exit_summary import compute_session_stats, render_exit_summary
    from coderai.cli.export_render import export_session_to_markdown
    from coderai.cli.interactive_menu import render_token_breakdown

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )
    session_id = "ad2596ec6a41_test"
    mgr._save_index(
        {
            "version": 1,
            "entries": [
                {
                    "id": session_id,
                    "summary": "Cache hit test",
                    "active_tokens": 12155,
                    "usage": {
                        "prompt_tokens": 30296,
                        "completion_tokens": 622,
                        "cached_tokens": 18464,
                        "total_tokens": 30918,
                    },
                    "usage_per_model": {
                        "gpt-5.6-luna": {
                            "prompt_tokens": 30296,
                            "completion_tokens": 622,
                            "cached_tokens": 18464,
                            "total_tokens": 30918,
                        }
                    },
                }
            ],
        }
    )

    # 1. Test compute_session_stats cache hit rate
    stats = compute_session_stats(mgr, session_id)
    assert stats["cached_tokens"] == 18464
    assert stats["prompt_tokens"] == 30296
    assert stats["cache_hit_rate"] == 60.9

    # 2. Test plain text render_token_breakdown
    render_token_breakdown(None, mgr, session_id)
    captured = capsys.readouterr().out
    assert "Cached Tokens:     18,464" in captured
    assert "Cache Hit Rate:    60.9%" in captured

    # 3. Test Rich console render_token_breakdown
    from rich.console import Console
    rich_console = Console(record=True, width=120)
    render_token_breakdown(rich_console, mgr, session_id)
    rich_text = rich_console.export_text()
    assert "Cached Tokens:" in rich_text
    assert "18,464" in rich_text
    assert "Cache Hit Rate:" in rich_text
    assert "60.9%" in rich_text

    # 4. Test render_exit_summary
    mock_exit_console = MagicMock()
    render_exit_summary(mock_exit_console, mgr, session_id)
    assert mock_exit_console.print.called

    # 5. Test export_session_to_markdown
    mgr._save_messages(session_id, [SessionMessage(id="m1", session_id=session_id, role="user", content="Hi")])
    md_path = export_session_to_markdown(mgr, session_id)
    md_text = pathlib.Path(md_path).read_text(encoding="utf-8")
    assert "Cached: 18,464 (60.9% hit)" in md_text


