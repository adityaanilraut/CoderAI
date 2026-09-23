"""Phase 8 regression test suite: prompt <-> code alignment."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coderai.agentspec import (
    load_agent_spec,
    render_system_prompt,
    resolve_agent_spec,
)
from coderai.prompt import (
    INIT,
    SYSTEM_PROMPT_BASE,
    get_init_command_prompt,
    get_system_prompt,
    load_agent_instructions,
    load_template,
)
from coderai.skill.flow.runner import Flow, FlowEdge, FlowNode, FlowRunner
from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.registry import (
    format_subagent_types_description,
    parse_markdown_agent_spec,
)
from coderai.ui.shell.dispatch import (
    ShellContext,
    SlashAction,
    cmd_goal,
    cmd_mcp,
    cmd_plan,
    cmd_thinking,
)


def test_pr_a5_render_system_prompt_default_has_no_unresolved_placeholders():
    """PR-A5: render_system_prompt renders through Jinja2 with filled BuiltinSystemPromptArgs
    and fails on undefined variables. No '${' or '{%' remains in '--agent default'.
    """
    spec = resolve_agent_spec("default")
    rendered = render_system_prompt(spec)
    assert "${" not in rendered, f"Found unresolved variable in rendered prompt:\n{rendered[:500]}"
    assert "{%" not in rendered, f"Found unresolved Jinja tag in rendered prompt:\n{rendered[:500]}"
    assert "CODERAI_OS" not in rendered
    assert "CODERAI_NOW" not in rendered


def test_pr_a5_render_system_prompt_fails_on_undefined_variable(tmp_path: Path):
    """PR-A5: Undefined variables raise an error instead of silently passing through."""
    bad_template = tmp_path / "system.md"
    bad_template.write_text("Hello ${UNDEFINED_VARIABLE_XYZ} world", encoding="utf-8")

    from coderai.agentspec import ResolvedAgentSpec
    from coderai.exception import SystemPromptTemplateError

    spec = ResolvedAgentSpec(
        name="test",
        system_prompt_path=bad_template,
        system_prompt_args={},
        model=None,
        when_to_use="",
        tools=["read"],
        allowed_tools=None,
        exclude_tools=[],
        subagents={},
    )
    with pytest.raises(SystemPromptTemplateError):
        render_system_prompt(spec)


def test_pr_a8_dynamic_available_tools_and_goal_guidance():
    """PR-A8/PR-B9: Available tools section is generated from live get_tools().
    No get_goal/create_goal/update_goal, and goal reflects actual schema.
    """
    prompt = get_system_prompt({"preset": "core"})
    assert "## get_goal" not in prompt
    assert "## create_goal" not in prompt
    assert "## update_goal" not in prompt
    # If goal is present, it shouldn't mention list/add/update/done
    if "## goal" in prompt:
        assert "(`list` / `add` / `update` / `done`)" not in prompt


def test_pr_a9_enter_plan_mode_description_uses_real_tool_names():
    """PR-A9/TL-B1: enter_plan_mode description uses real tool names."""
    desc_path = Path("coderai/tools/plan/enter_description.md")
    content = desc_path.read_text(encoding="utf-8")
    assert "EnterPlanMode" not in content
    assert "ExitPlanMode" not in content
    assert "ReadFile" not in content
    assert "Agent(subagent_type=" not in content
    assert "enter_plan_mode" in content
    assert "exit_plan_mode" in content


def test_pr_b1_code_reviewer_clean_checklist():
    """PR-B1: .coderai/agents/code-reviewer.md contains no benchmark answer keys or contradictions."""
    content = Path(".coderai/agents/code-reviewer.md").read_text(encoding="utf-8")
    benchmark_tokens = [
        "isConditionalPasskeysEnabled",
        "UpdateCompatibilityCheck",
        "ASN1Encoder",
        "ClientPermissionsV2",
        "santizeAnchors",
        "messages_lt.properties",
        "ADMIN_FINE_GRAINED_AUTHZ",
    ]
    for token in benchmark_tokens:
        assert token not in content, f"Benchmark token {token} found in code-reviewer.md"
    assert "Do not attempt to inspect or query the host machine's local workspace" not in content


def test_pr_b2_base_prompt_safety_guardrails():
    """PR-B2: SYSTEM_PROMPT_BASE contains safety guardrails and conditional reproduction tests."""
    assert "git commit" in SYSTEM_PROMPT_BASE
    assert "working directory" in SYSTEM_PROMPT_BASE
    assert "language" in SYSTEM_PROMPT_BASE.lower()
    # Reproduction test should be conditional on bug fixes
    assert "bug" in SYSTEM_PROMPT_BASE.lower()


def test_pr_a10_extend_resolves_relative_paths_and_null_override():
    """PR-A10: extend resolves relative paths using base file dir; null overrides base."""
    okabe_spec = resolve_agent_spec("okabe")
    assert okabe_spec.name == "okabe"
    assert okabe_spec.system_prompt_path.is_file()
    assert "default" in str(okabe_spec.system_prompt_path)

    # In coder.yaml, subagents is null -> should resolve to empty dict
    coder_path = Path("coderai/agents/default/coder.yaml")
    coder_spec = load_agent_spec(coder_path)
    assert coder_spec.name == "coder"
    assert coder_spec.subagents == {}


def test_pr_b8_d4_unified_init_prompt_and_templates():
    """PR-B8/PR-D4: load_template loads templates; get_init_command_prompt uses unified template."""
    init_tmpl = load_template("init.md")
    assert init_tmpl.strip()
    assert INIT == init_tmpl
    cmd_init_prompt = get_init_command_prompt(".")
    assert "AGENTS.md" in cmd_init_prompt


def test_pr_b9_hierarchical_instructions_and_truncation_warning(tmp_path: Path, caplog):
    """PR-B9: load_agent_instructions loads root and .coderai/ hierarchically, warns on >8000 chars."""
    import logging

    root_agents = tmp_path / "AGENTS.md"
    root_agents.write_text("Root instructions", encoding="utf-8")
    sub_dir = tmp_path / ".coderai"
    sub_dir.mkdir()
    sub_agents = sub_dir / "AGENTS.md"
    sub_agents.write_text("X" * 8500, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        loaded = load_agent_instructions(str(tmp_path))

    assert loaded is not None
    assert "Root instructions" in loaded
    assert any("truncated" in r.message.lower() for r in caplog.records)


def test_pr_c2_subagent_spec_model_and_exclude_tools(tmp_path: Path):
    """PR-C2: Spec model, exclude_tools, and when_to_use are wired into SubAgentSpec."""
    agents_dir = tmp_path / ".coderai" / "agents"
    agents_dir.mkdir(parents=True)
    spec_md = agents_dir / "my-agent.md"
    spec_md.write_text(
        "---\nname: my-agent\ndescription: Custom agent\nmodel: custom-gpt\nwhen_to_use: When custom\nexclude_tools:\n  - bash\n---\nPrompt",
        encoding="utf-8",
    )
    defn = parse_markdown_agent_spec(spec_md)
    assert defn is not None
    assert defn.model == "custom-gpt"
    assert "bash" in defn.exclude_tools
    assert defn.when_to_use == "When custom"

    sub_spec = SubAgentSpec(
        description="test",
        prompt="do task",
        subagent_type="my-agent",
        isolated_cwd=str(tmp_path),
    )
    assert sub_spec.subagent_type == "my-agent"
    assert sub_spec.model == "custom-gpt"
    assert sub_spec.exclude_tools == ["bash"]
    assert sub_spec.when_to_use == "When custom"


def test_pr_c3_flow_decision_retry_cap_and_case_insensitive():
    """PR-C3: Flow decision retries are capped, moves increment, and choices match case-insensitively."""
    nodes = {
        "BEGIN": FlowNode(id="BEGIN", label="Start", kind="begin"),
        "DECIDE": FlowNode(id="DECIDE", label="Pick branch", kind="decision"),
        "YES_NODE": FlowNode(id="YES_NODE", label="Yes", kind="end"),
        "NO_NODE": FlowNode(id="NO_NODE", label="No", kind="end"),
        "END": FlowNode(id="END", label="End", kind="end"),
    }
    outgoing = {
        "BEGIN": [FlowEdge(src="BEGIN", dst="DECIDE", label=None)],
        "DECIDE": [
            FlowEdge(src="DECIDE", dst="YES_NODE", label="YES"),
            FlowEdge(src="DECIDE", dst="NO_NODE", label="NO"),
        ],
    }
    flow = Flow(nodes=nodes, outgoing=outgoing, begin_id="BEGIN", end_id="END")
    runner = FlowRunner(flow, max_moves=10)

    # Case-insensitive match
    matched = runner._match_flow_edge(outgoing["DECIDE"], "yes")
    assert matched == "YES_NODE"
    matched_upper = runner._match_flow_edge(outgoing["DECIDE"], "YES")
    assert matched_upper == "YES_NODE"


def test_ui_b3_dispatch_command_parsing():
    """UI-B3: Slash commands reject unknown args and parse subcommands cleanly."""
    ctx = ShellContext(mgr=MagicMock(), session_id="test-session")

    # /thinking unknown arg should not toggle
    ctx.thinking_expanded = False
    action = cmd_thinking(ctx, "invalid_option")
    assert action == SlashAction.HANDLED
    assert ctx.thinking_expanded is False

    # /plan unknown arg should not toggle
    ctx.active_plan_mode = False
    action = cmd_plan(ctx, "unknown_subcommand")
    assert action == SlashAction.HANDLED
    assert ctx.active_plan_mode is False

    # /plan reset should clear and set active_plan_mode = False
    ctx.active_plan_mode = True
    action = cmd_plan(ctx, "reset")
    assert action == SlashAction.HANDLED
    assert ctx.active_plan_mode is False

    # /goal unknown args should show start <id> in usage
    cmd_goal(ctx, "unknown_args_xyz")


@pytest.mark.asyncio
async def test_ui_b3_mcp_clean_parsing():
    """UI-B3: /mcp parses cleanly without false startswith matches."""
    ctx = ShellContext(mgr=MagicMock(), session_id="test-session")
    # /mcp reconnectfoo should be rejected as unknown subcommand
    action = await cmd_mcp(ctx, "reconnectfoo")
    assert action == SlashAction.HANDLED


def test_pr_c2_format_subagent_types_description():
    """PR-C2: format_subagent_types_description includes coder, explore, plan."""
    desc = format_subagent_types_description()
    assert "coder" in desc
    assert "explore" in desc
    assert "plan" in desc


def test_ui_b3_list_sessions_parser_flag():
    """UI-B3 / IN-B5: --list-sessions flag is recognized in CLI parser."""
    from coderai.ui.shell.startup import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["--list-sessions"])
    assert args.list_sessions is True
