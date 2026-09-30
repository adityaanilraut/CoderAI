"""Session ownership and immutable-policy reuse regressions for persistent PTYs."""

from pathlib import Path

import pytest

from coderai.acp.terminal import TerminalBridge
from coderai.terminal.manager import TerminalManager, TerminalSessionStatus
from coderai.tools.legacy import terminal as terminal_tools
from coderai.tools.shell import _execute_persistent_bash


class FakeTerminal:
    def __init__(
        self,
        session_id,
        command,
        name=None,
        cwd=None,
        env=None,
        sandbox_mode=None,
        workspace_root=None,
        owner_session_id=None,
        execution_root=None,
    ):
        from coderai.sandbox import parse_sandbox_mode

        self.session_id = session_id
        self.name = name or session_id
        self.cwd = cwd
        self.owner_session_id = owner_session_id
        self.workspace_root = str(Path(workspace_root or cwd).resolve())
        self.execution_root = str(Path(execution_root or self.workspace_root).resolve())
        self.sandbox_mode = parse_sandbox_mode(sandbox_mode) if sandbox_mode else None
        self.is_alive = True
        self.pid = 123
        self.exit_code = None
        self.process_type = "bash"
        self.commands = []
        self.signals = []
        self.pending = ""

    def send(self, text, submit=True):
        assert self.is_alive
        self.commands.append(text)
        if "__CODERAI_START_token__" in text:
            self.pending = f"__CODERAI_START_token__\nresult\n__CODERAI_END_token__:0:{self.cwd}\n"

    def read_available(self, timeout_s=0):
        return ""

    def read_unread(self):
        text, self.pending = self.pending, ""
        return text

    def send_signal(self, signal):
        self.signals.append(signal)

    def close(self):
        self.is_alive = False
        self.exit_code = 0

    def status(self):
        return TerminalSessionStatus(
            self.session_id, self.name, self.process_type, self.pid, self.is_alive, cwd=self.cwd
        )


@pytest.fixture
def terminals(monkeypatch):
    import coderai.terminal.manager as terminal_module
    import coderai.tools.shell as shell

    monkeypatch.setattr(terminal_module, "TerminalSession", FakeTerminal)
    manager = TerminalManager()
    monkeypatch.setattr(terminal_module, "_default_terminal_manager", manager)
    monkeypatch.setattr(terminal_tools, "get_terminal_manager", lambda: manager)
    monkeypatch.setattr(shell, "get_terminal_manager", lambda: manager)
    monkeypatch.setattr(shell.secrets, "token_hex", lambda count: "token")
    monkeypatch.setattr(shell.time, "sleep", lambda duration: None)
    return manager


def context(owner, root, **extra):
    return {"session_id": owner, "project_root": str(root), **extra}


@pytest.mark.parametrize(
    "operation,args",
    [
        (terminal_tools.handle_terminal_send_tool, {"text": "echo private"}),
        (terminal_tools.handle_terminal_read_tool, {}),
        (terminal_tools.handle_terminal_signal_tool, {"signal": "SIGTERM"}),
        (terminal_tools.handle_terminal_close_tool, {}),
    ],
)
@pytest.mark.parametrize("lookup", ["id", "name"])
def test_other_session_cannot_operate_on_terminal(terminals, tmp_path, operation, args, lookup):
    term = terminals.open_session(
        command="bash",
        name="shared-name",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        owner_session_id="owner",
    )
    result = operation(
        {"sessionId": term.session_id if lookup == "id" else term.name, **args},
        context("other", tmp_path),
    )
    assert not result.ok
    assert term.commands == [] and term.signals == [] and term.is_alive


def test_terminal_list_and_workspace_access_are_scoped(terminals, tmp_path):
    other_root = tmp_path / "other"
    other_root.mkdir()
    opened = terminal_tools.handle_terminal_open_tool(
        {"name": "private"}, context("owner", tmp_path)
    )
    assert opened.ok
    assert (
        terminal_tools.handle_terminal_list_tool({}, context("other", tmp_path)).metadata[
            "sessions"
        ]
        == []
    )
    assert (
        terminal_tools.handle_terminal_list_tool({}, context("owner", other_root)).metadata[
            "sessions"
        ]
        == []
    )
    assert (
        len(
            terminal_tools.handle_terminal_list_tool({}, context("owner", tmp_path)).metadata[
                "sessions"
            ]
        )
        == 1
    )
    assert not terminal_tools.handle_terminal_send_tool(
        {"sessionId": opened.metadata["sessionId"], "text": "echo forbidden"},
        context("owner", other_root),
    ).ok


def test_missing_owner_cannot_open_or_access_owned_terminal(terminals, tmp_path):
    assert not terminal_tools.handle_terminal_open_tool({}, {"project_root": str(tmp_path)}).ok
    term = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="owner"
    )
    assert terminals.get_session(term.session_id) is None
    assert not terminals.close_session(term.session_id)


def test_duplicate_names_resolve_only_within_owner(terminals, tmp_path):
    a = terminals.open_session(
        command="bash",
        name="same",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        owner_session_id="a",
    )
    b = terminals.open_session(
        command="bash",
        name="same",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        owner_session_id="b",
    )
    assert terminals.get_session("same", owner_session_id="a", workspace_root=str(tmp_path)) is a
    assert terminals.get_session("same", owner_session_id="b", workspace_root=str(tmp_path)) is b


@pytest.mark.parametrize(
    "before,after",
    [
        ("danger-full-access", "read-only"),
        ("workspace-write", "read-only"),
        (None, "workspace-write"),
    ],
)
def test_persistent_shell_recreated_before_command_on_policy_change(
    terminals, tmp_path, before, after
):
    ctx = context("owner", tmp_path, sandbox_mode=before)
    assert _execute_persistent_bash("echo first", "owner", str(tmp_path), ctx, {}).ok
    old = terminals.get_session("persistent_bash_owner", owner_session_id="owner")
    old_commands = list(old.commands)
    ctx["sandbox_mode"] = after
    assert _execute_persistent_bash("echo restricted", "owner", str(tmp_path), ctx, {}).ok
    new = terminals.get_session("persistent_bash_owner", owner_session_id="owner")
    assert new is not old and not old.is_alive
    assert old.commands == old_commands
    assert new.sandbox_mode == after
    assert "echo restricted" in new.commands[-1]


def test_persistent_shell_reuses_same_policy_and_root(terminals, tmp_path):
    ctx = context("owner", tmp_path, sandbox_mode="workspace-write")
    assert _execute_persistent_bash("echo first", "owner", str(tmp_path), ctx, {}).ok
    old = terminals.get_session("persistent_bash_owner", owner_session_id="owner")
    ctx["sandbox_mode"] = "workspace_write"
    assert _execute_persistent_bash("echo second", "owner", str(tmp_path), ctx, {}).ok
    assert terminals.get_session("persistent_bash_owner", owner_session_id="owner") is old


@pytest.mark.parametrize("change_workspace", [True, False])
def test_persistent_shell_recreated_on_workspace_or_isolation_change(
    terminals, tmp_path, change_workspace
):
    other = tmp_path / "other"
    other.mkdir()
    ctx = context("owner", tmp_path, sandbox_mode="workspace-write")
    assert _execute_persistent_bash("echo first", "owner", str(tmp_path), ctx, {}).ok
    old = terminals.get_session("persistent_bash_owner", owner_session_id="owner")
    ctx["project_root" if change_workspace else "isolated_cwd"] = str(other)
    assert _execute_persistent_bash("echo second", "owner", str(other), ctx, {}).ok
    new = terminals.get_session("persistent_bash_owner", owner_session_id="owner")
    assert new is not old and not old.is_alive and new.execution_root == str(other.resolve())


def test_manager_terminal_cleanup_preserves_sibling_owner_and_workspace(terminals, tmp_path):
    from coderai.soul.session.manager import SessionManager

    other = tmp_path / "other"
    other.mkdir()
    a = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="a"
    )
    b = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="b"
    )
    elsewhere = terminals.open_session(
        command="bash", cwd=str(other), workspace_root=str(other), owner_session_id="a"
    )
    child = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="child"
    )
    from coderai.subagents.core import AgentHandle, AgentRegistry

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    manager._active_session_id = "a"
    manager.agent_registry = AgentRegistry()
    manager.agent_registry.register(
        AgentHandle(
            id="ownership-child",
            parent_session_id="a",
            run_session_id="child",
            description="child",
            mode="continuable",
        )
    )
    try:
        manager.close_owned_terminals()
        assert not a.is_alive and not child.is_alive and b.is_alive and elsewhere.is_alive
    finally:
        manager.dispose()


def test_acp_bridge_applies_workspace_and_manager_owner_boundary(terminals, tmp_path):
    bridge = TerminalBridge("acp-owner", str(tmp_path), manager=terminals)
    opened = bridge.open_terminal(command="bash")
    assert "error" not in opened
    assert (
        terminals.get_session(
            opened["sessionId"], owner_session_id="acp-owner", workspace_root=str(tmp_path)
        )
        is not None
    )
    assert not terminal_tools.handle_terminal_send_tool(
        {"sessionId": opened["sessionId"], "text": "echo forbidden"},
        context("other", tmp_path),
    ).ok
    assert "error" in bridge.open_terminal(command="bash", cwd=str(tmp_path.parent))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_disposing_manager_closes_only_its_terminal(terminals, tmp_path, asynchronous):
    from coderai.cli.session_factory import close_session_manager
    from coderai.soul.session.manager import SessionManager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    manager._active_session_id = "a"
    a = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="a"
    )
    b = terminals.open_session(
        command="bash", cwd=str(tmp_path), workspace_root=str(tmp_path), owner_session_id="b"
    )
    if asynchronous:
        await close_session_manager(manager)
    else:
        manager.dispose()
    assert not a.is_alive and b.is_alive
