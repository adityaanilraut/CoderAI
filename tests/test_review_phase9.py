"""Phase 9 regression test suite: Consolidation, file splits, and dead-code sweep."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ============================================================================
# 1. UI-B2: Unified Slash Command Registry & Canonical Names
# ============================================================================


def test_ui_b2_slash_command_unified_export():
    """UI-B2: SlashCommand is exported from both utils.slashcmd and ui.shell.slash."""
    from coderai.ui.shell.slash import SlashCommand as ShellSlashCommand
    from coderai.utils.slashcmd import SlashCommand as UtilsSlashCommand

    assert ShellSlashCommand is UtilsSlashCommand

    # Verify display name and callable/str support
    cmd1 = ShellSlashCommand(name="test_cmd", description="test description", func=lambda: None)
    assert cmd1.name == "test_cmd"
    assert cmd1.description == "test description"
    assert str(cmd1.display_name) == "/test_cmd"
    assert cmd1.display_name() == "/test_cmd"

    cmd2 = ShellSlashCommand(name="dynamic_cmd", aliases=["dyn"], func=lambda: None)
    assert cmd2.name == "dynamic_cmd"
    assert "/dyn" in str(cmd2.display_name)
    assert cmd2.display_name("dyn") == "/dynamic_cmd (dyn)"


def test_ui_b2_canonical_and_alias_names():
    """UI-B2: 'rename' is canonical (alias 'title'), 'sessions' is canonical (alias 'resume')."""
    from coderai.ui.shell.dispatch import registry
    from coderai.ui.shell.slash import COMMAND_ALIASES, COMMAND_CATALOG, resolve_command

    # Check COMMAND_CATALOG and COMMAND_ALIASES in slash.py
    assert "rename" in COMMAND_CATALOG
    assert COMMAND_ALIASES.get("title") == "rename"
    assert "sessions" in COMMAND_CATALOG
    assert COMMAND_ALIASES.get("resume") == "sessions"

    # resolve_command finds them both
    cmd_rename = resolve_command("rename")
    assert cmd_rename is not None
    assert cmd_rename.name == "rename"

    cmd_title = resolve_command("title")
    assert cmd_title is not None
    assert cmd_title.name == "rename"

    cmd_sessions = resolve_command("sessions")
    assert cmd_sessions is not None
    assert cmd_sessions.name == "sessions"

    cmd_resume = resolve_command("resume")
    assert cmd_resume is not None
    assert cmd_resume.name == "sessions"

    # Check dispatch registry in dispatch.py
    dispatch_rename = registry.find_command("rename")
    dispatch_title = registry.find_command("title")
    assert dispatch_rename is not None
    assert dispatch_title is not None
    assert dispatch_rename == dispatch_title

    dispatch_sessions = registry.find_command("sessions")
    dispatch_resume = registry.find_command("resume")
    assert dispatch_sessions is not None
    assert dispatch_resume is not None
    assert dispatch_sessions == dispatch_resume


def test_ui_b2_help_coverage_for_missing_commands():
    """UI-B2: COMMAND_HELP_DETAILS and HELP_GROUPS include newly documented commands."""
    from coderai.ui.shell.slash import COMMAND_HELP_DETAILS, HELP_GROUPS

    documented_cmds = ["reset", "yolo", "afk", "add-dir", "context", "import", "web-vis"]
    for name in documented_cmds:
        assert name in COMMAND_HELP_DETAILS, f"Missing help detail for {name}"

    # Ensure they appear in at least one help group (formatted as /<cmd>)
    all_grouped_cmds = [
        cmd.split(",")[0].strip() for _, group_items in HELP_GROUPS for cmd, _, _ in group_items
    ]
    for name in documented_cmds:
        expected = f"/{name}"
        assert expected in all_grouped_cmds, (
            f"Command {expected} not in any HELP_GROUPS: {all_grouped_cmds}"
        )


def test_ui_b2_cmd_reset_clears_session_state():
    """UI-B2: /reset clears session state via clear_session_state."""
    from coderai.state import create_snippet, get_snippet
    from coderai.ui.shell.dispatch import ShellContext, cmd_reset

    # Create dummy state with session_id as first argument
    session_id = "test-session-reset-123"
    snippet = create_snippet(session_id, "test.py", 1, 10, "print('hello')")
    assert snippet is not None
    assert get_snippet(session_id, snippet.id) is not None

    ctx = MagicMock(spec=ShellContext)
    ctx.session_id = session_id
    ctx.session = MagicMock()
    ctx.session_manager = MagicMock()
    ctx.print_info = MagicMock()

    cmd_reset(ctx, "")

    # Verify session state was wiped
    assert get_snippet(session_id, snippet.id) is None


# ============================================================================
# 2. IN-D1: Configuration Module Splitting & Backward Compatibility
# ============================================================================


def test_in_d1_typed_config_exports_and_reexports():
    """IN-D1: typed_config classes are importable from typed_config and config."""
    import coderai.config as cfg
    import coderai.typed_config as tcfg

    # Check core classes exist in typed_config
    assert hasattr(tcfg, "TypedConfig")
    assert hasattr(tcfg, "Config")
    assert hasattr(tcfg, "LLMProvider")
    assert hasattr(tcfg, "LLMModel")
    assert hasattr(tcfg, "LoopControl")
    assert hasattr(tcfg, "MCPClientConfig")
    assert hasattr(tcfg, "load_typed_config")
    assert hasattr(tcfg, "save_typed_config")

    # Check identical re-exports in config.py
    assert cfg.TypedConfig is tcfg.TypedConfig
    assert cfg.Config is tcfg.Config
    assert cfg.LLMProvider is tcfg.LLMProvider
    assert cfg.LLMModel is tcfg.LLMModel
    assert cfg.LoopControl is tcfg.LoopControl
    assert cfg.MCPClientConfig is tcfg.MCPClientConfig
    assert cfg.load_typed_config is tcfg.load_typed_config
    assert cfg.save_typed_config is tcfg.save_typed_config


def test_in_d1_provider_registry_exports_and_reexports():
    """IN-D1: provider_registry entries are importable from provider_registry and config."""
    import coderai.config as cfg
    import coderai.provider_registry as preg

    assert hasattr(preg, "KNOWN_PROVIDERS")
    assert hasattr(preg, "PROVIDER_REGISTRY")
    assert hasattr(preg, "DEFAULT_MODEL")
    assert hasattr(preg, "get_configured_provider_keys")
    assert hasattr(preg, "save_provider_api_key")

    assert cfg.KNOWN_PROVIDERS is preg.KNOWN_PROVIDERS
    assert cfg.PROVIDER_REGISTRY is preg.PROVIDER_REGISTRY
    assert cfg.DEFAULT_MODEL is preg.DEFAULT_MODEL
    assert cfg.get_configured_provider_keys is preg.get_configured_provider_keys
    assert cfg.save_provider_api_key is preg.save_provider_api_key


# ============================================================================
# 3. UI-D1: Prompt Completer Splitting & Backward Compatibility
# ============================================================================


def test_ui_d1_prompt_completers_exports_and_reexports():
    """UI-D1: Completers are importable from prompt_completers and prompt."""
    import coderai.ui.shell.prompt as prompt_mod
    import coderai.ui.shell.prompt_completers as comp_mod

    assert hasattr(comp_mod, "SlashCommandCompleter")
    assert hasattr(comp_mod, "FileMentionCompleter")
    assert hasattr(comp_mod, "LocalFileMentionCompleter")
    assert hasattr(comp_mod, "fuzzy_filter")
    assert hasattr(comp_mod, "fuzzy_score")

    assert prompt_mod.SlashCommandCompleter is comp_mod.SlashCommandCompleter
    assert prompt_mod.FileMentionCompleter is comp_mod.FileMentionCompleter
    assert prompt_mod.LocalFileMentionCompleter is comp_mod.LocalFileMentionCompleter
    assert prompt_mod.fuzzy_filter is comp_mod.fuzzy_filter
    assert prompt_mod.fuzzy_score is comp_mod.fuzzy_score


def test_ui_d1_fuzzy_score_logic():
    """UI-D1: fuzzy_score accurately scores prefix, word-boundary, and non-matches."""
    from coderai.ui.shell.prompt_completers import fuzzy_score

    matched, score = fuzzy_score("test", "test")
    assert matched is True
    assert score == 10000

    matched, score = fuzzy_score("te", "test")
    assert matched is True
    assert score > 0

    matched, score = fuzzy_score("xyz", "test")
    assert matched is False


# ============================================================================
# 4. AL-B2 & AL-B8: File Snippets and State Backward Compatibility
# ============================================================================


def test_al_b2_state_backward_compatibility():
    """AL-B2/AL-B8: coderai.state re-exports everything from coderai.file_snippets."""
    import coderai.file_snippets as fs
    import coderai.state as st

    assert st.FileSnippet is fs.FileSnippet
    assert st.FileState is fs.FileState
    assert st.SessionStateManager is fs.SessionStateManager
    assert st.create_snippet is fs.create_snippet
    assert st.record_file_state is fs.record_file_state
    assert st.clear_session_state is fs.clear_session_state


def test_al_b2_session_state_isolation():
    """AL-B2: SessionStateManager correctly isolates snippets across sessions."""
    from coderai.file_snippets import (
        clear_session_state,
        create_snippet,
        get_snippet,
    )

    s1 = "session-1"
    s2 = "session-2"

    snip1 = create_snippet(s1, "foo.py", 1, 5, "code1")
    snip2 = create_snippet(s2, "bar.py", 1, 5, "code2")

    assert snip1 is not None
    assert snip2 is not None

    # Both have independent registries; s1 contains foo.py and s2 contains bar.py
    s1_res = get_snippet(s1, snip1.id)
    assert s1_res is not None
    assert s1_res.file_path == "foo.py"

    s2_res = get_snippet(s2, snip2.id)
    assert s2_res is not None
    assert s2_res.file_path == "bar.py"

    clear_session_state(s1)
    assert get_snippet(s1, snip1.id) is None
    assert get_snippet(s2, snip2.id) is not None

    clear_session_state(s2)


# ============================================================================
# 5. WF-B1, WF-B2, WF-B3, WF-B4, WF-D4: Subagent & Hook Infrastructure
# ============================================================================


def test_wf_b1_build_spec_defaults():
    """WF-B1: build_spec applies unified subagent defaults from orchestration settings/env."""
    from coderai.subagents.builder import build_spec

    mock_context = MagicMock()
    mock_context.session_id = "sess-spec-test"
    mock_context.session_manager = None
    mock_context.settings = {
        "orchestration": {
            "timeoutSeconds": 45.0,
            "maxIterations": 15,
            "maxDepth": 2,
        }
    }

    spec = build_spec(
        mock_context,
        description="test task",
        prompt="do something",
    )
    assert spec.description == "test task"
    assert spec.prompt == "do something"
    assert spec.timeout_seconds == 45.0
    assert spec.max_iterations == 15
    assert spec.max_depth == 2
    assert spec.parent_session_id == "sess-spec-test"


def test_wf_d4_make_subagent_result():
    """WF-D4: make_subagent_result builds standardized SubAgentResult."""
    from coderai.subagents.builder import SubAgentSpec
    from coderai.subagents.runner import make_subagent_result

    spec = SubAgentSpec(description="desc", prompt="prompt", task_id="task-42")
    res = make_subagent_result(
        spec=spec,
        session_id="sub-sess-99",
        status="completed",
        summary="all done",
        total_prompt_tokens=100,
        total_completion_tokens=50,
        duration_seconds=3.5,
    )
    assert res.task_id == "task-42"
    assert res.session_id == "sub-sess-99"
    assert res.status == "completed"
    assert res.summary == "all done"
    assert res.total_tokens == 150
    assert res.duration_seconds == 3.5
    assert res.exit_code == 0


@pytest.mark.asyncio
async def test_wf_b2_hook_exit_code_2_blocks():
    """WF-B2: Hook runner treats exit code 2 as blocking error with stderr as reason."""
    from coderai.hooks.runner import run_hook

    # Run a command that exits with code 2
    cmd = "python3 -c \"import sys; sys.stderr.write('fatal hook failure'); sys.exit(2)\""
    result = await run_hook(
        command=cmd,
        input_data={"event": "test"},
        cwd=".",
        timeout=5,
    )
    assert result.action == "block"
    assert result.exit_code == 2
    assert "fatal hook failure" in result.reason


# ============================================================================
# 6. TL-B7 & TL-B8: File Writing & Legacy Executor
# ============================================================================


def test_tl_b7_atomic_write_preserves_file_permissions(tmp_path: Path):
    """TL-B7: atomic_write_text preserves explicit and existing file permissions."""
    from coderai.utils.io import atomic_write_text

    target = tmp_path / "secret.txt"

    # Write initial file with 0o600
    atomic_write_text(target, "first content", mode=0o600)
    current_mode = stat.S_IMODE(target.stat().st_mode)
    assert current_mode == 0o600

    # Overwrite without specifying mode -> should preserve 0o600
    atomic_write_text(target, "second content")
    preserved_mode = stat.S_IMODE(target.stat().st_mode)
    assert preserved_mode == 0o600


def test_tl_b8_sliding_window_rate_limiter():
    """TL-B8: SlidingWindowRateLimiter limits call frequency within window."""
    from coderai.tools.legacy.executor import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter()
    key = "test_tool"

    # Allow up to 2 calls in 1.0s window
    allowed1, retry1 = limiter.acquire(key, max_requests=2, window_seconds=1.0)
    assert allowed1 is True
    assert retry1 == 0.0

    allowed2, retry2 = limiter.acquire(key, max_requests=2, window_seconds=1.0)
    assert allowed2 is True
    assert retry2 == 0.0

    # 3rd call must be denied with positive retry_after
    allowed3, retry3 = limiter.acquire(key, max_requests=2, window_seconds=1.0)
    assert allowed3 is False
    assert retry3 > 0.0


# ============================================================================
# 7. IN-B7 & IN-D3: Subprocess Secret Scrubbing
# ============================================================================


def test_in_b7_scrub_subprocess_env():
    """IN-B7: scrub_subprocess_env strips sensitive tokens unless in preserve_keys."""
    from coderai.utils.subprocess_env import scrub_subprocess_env

    dirty_env = {
        "PATH": "/usr/bin:/bin",
        "USER": "developer",
        "OPENAI_API_KEY": "sk-secret-key-12345",
        "ANTHROPIC_API_KEY": "sk-ant-secret-67890",
        "CODERAI_SAFE_VAR": "visible_value",
    }

    clean_env = scrub_subprocess_env(dirty_env)
    assert "OPENAI_API_KEY" not in clean_env
    assert "ANTHROPIC_API_KEY" not in clean_env
    assert clean_env["PATH"] == "/usr/bin:/bin"
    assert clean_env["USER"] == "developer"
    assert clean_env["CODERAI_SAFE_VAR"] == "visible_value"

    # Preserved keys are kept
    preserved_env = scrub_subprocess_env(dirty_env, preserve_keys={"OPENAI_API_KEY"})
    assert "OPENAI_API_KEY" in preserved_env
    assert "ANTHROPIC_API_KEY" not in preserved_env
