"""Focused tests for the coderai.core engine (no network)."""

import json
import pathlib

import pytest

from coderai.core.common.file_history import GitFileHistory
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.mcp.manager import McpManager
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    compute_tool_call_permissions,
    evaluate_permission_scopes,
    resolve_snippet_file_path,
)
from coderai.core.prompt import (
    build_skill_documents_prompt,
    extract_skill_frontmatter,
    get_plan_mode_prompt,
    get_runtime_context,
    get_system_prompt,
    get_tools,
    list_skill_resource_files,
    list_skills,
    load_agent_instructions,
    load_skill,
    match_skills_for_prompt,
    parse_skill_match_response,
    strip_skill_prompt_metadata,
)
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.state import (
    FileState,
    clear_session_state,
    create_snippet,
    get_snippet,
    has_snippet_outdated_file_version,
    record_file_state,
)
from coderai.core.tools.bash import handle as bash_handle
from coderai.core.tools.edit import (
    _normalize_loose_text,
    _parse_corrected_edit_strings,
    handle as edit_handle,
)
from coderai.core.tools.read import handle as read_handle
from coderai.core.tools.write import handle as write_handle


def _resp(content, tool_calls=None):
    message = type(
        "M",
        (),
        {
            "content": content,
            "tool_calls": tool_calls or None,
            "reasoning_content": None,
            "refusal": None,
        },
    )()
    usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": usage})()


def _tc(cid, name, args):
    return type(
        "TC",
        (),
        {"id": cid, "function": type("F", (), {"name": name, "arguments": json.dumps(args)})()},
    )()


def test_snippet_lifecycle_and_staleness():
    sid = "t1"
    clear_session_state(sid)
    sn = create_snippet(sid, "/tmp/a.py", 1, 10, "x")
    assert sn is not None and sn.id == "snippet_1"
    assert get_snippet(sid, "snippet_1") is sn
    assert not has_snippet_outdated_file_version(sid, sn)
    record_file_state(
        sid, FileState(file_path="/tmp/a.py", content="", timestamp=0), increment_version=True
    )
    assert has_snippet_outdated_file_version(sid, sn)


def test_permission_scopes():
    assert evaluate_permission_scopes(["read-in-cwd"]) == "allow"
    assert (
        evaluate_permission_scopes(
            ["write-in-cwd"],
            {"allow": [], "deny": [], "ask": ["write-in-cwd"], "defaultMode": "allowAll"},
        )
        == "ask"
    )
    assert (
        evaluate_permission_scopes(
            ["network"], {"allow": [], "deny": ["network"], "ask": [], "defaultMode": "allowAll"}
        )
        == "deny"
    )


def test_plan_mode_forced_permissions():
    assert "write-in-cwd" in PLAN_MODE_FORCE_ASK_SCOPES
    # Even under defaultMode: allowAll and empty ask list, force_ask_scopes turns write into ask
    decision = evaluate_permission_scopes(
        ["write-in-cwd"],
        settings={"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"},
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert decision == "ask"

    plan = compute_tool_call_permissions(
        session_id="s_plan",
        project_root="/tmp",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": json.dumps({"file_path": "/tmp/a.py", "content": "x"}),
                },
            }
        ],
        settings={"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"},
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert plan["askPermissions"]
    assert plan["askPermissions"][0]["toolCallId"] == "call_1"


def test_bash_side_effect_scopes():
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root="/tmp",
        tool_calls=[
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"command": "git status", "sideEffects": ["query-git-log"]}
                    ),
                },
            }
        ],
        settings={"allow": [], "deny": [], "ask": ["query-git-log"], "defaultMode": "allowAll"},
    )
    assert plan["askPermissions"]


def test_tool_definitions():
    names = {t["function"]["name"] for t in get_tools()}
    assert {
        "bash",
        "job_list",
        "job_output",
        "job_kill",
        "read",
        "write",
        "edit",
        "WebSearch",
        "UpdatePlan",
    } <= names


def test_read_edit_write(tmp_path: pathlib.Path):
    p = tmp_path / "hello.py"
    p.write_text("a=1\nb=2\nc=3\n")
    ctx = {"session_id": "tools", "project_root": str(tmp_path)}
    clear_session_state("tools")
    r = read_handle({"file_path": str(p)}, ctx)
    assert r.ok and r.metadata and "snippet" in r.metadata
    snippet_id = r.metadata["snippet"]["id"]
    e = edit_handle({"snippet_id": snippet_id, "old_string": "a=1", "new_string": "a=99"}, ctx)
    assert e.ok
    assert "a=99" in p.read_text()
    e2 = edit_handle({"snippet_id": snippet_id, "old_string": "b=2", "new_string": "b=99"}, ctx)
    assert e2.ok
    e3 = edit_handle({"snippet_id": snippet_id, "old_string": "a=1", "new_string": "a=0"}, ctx)
    assert not e3.ok and "changed" in e3.error.lower()
    w = write_handle({"file_path": str(tmp_path / "new.py"), "content": "x=1\n"}, ctx)
    assert w.ok


async def test_session_loop(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "app.py").write_text("print(1)")

    calls = {"n": 0}

    def script(calls, kwargs):
        # Handle skill matching queries during session creation
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                "I'll read it.", [_tc("call_1", "read", {"file_path": str(tmp_path / "app.py")})]
            )
        return _resp("Done.", [])

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": ["read-in-cwd", "write-in-cwd"],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
    )
    session_id = await mgr.create_session("hello")
    messages = mgr.list_session_messages(session_id)
    assert any(m.role == "tool" for m in messages)
    assert messages[-1].content == "Done."
    assert calls["n"] == 2


async def test_permission_ask_then_allow(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    calls = {"n": 0}

    def script(calls, kwargs):
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                "I'll write it.",
                [
                    _tc(
                        "call_1",
                        "write",
                        {"file_path": str(tmp_path / "out.py"), "content": "x = 1\n"},
                    )
                ],
            )
        return _resp("Done.", [])

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": [],
                "deny": [],
                "ask": ["write-in-cwd"],
                "defaultMode": "allowAll",
            },
        },
    )
    session_id = await mgr.create_session("write it")
    entry = mgr.get_session(session_id)
    assert entry is not None and entry.status == "ask_permission"
    assert entry.ask_permissions and entry.ask_permissions[0]["toolCallId"] == "call_1"
    assert not (tmp_path / "out.py").exists()

    await mgr.reply_session(
        session_id, None, permission_replies=[{"toolCallId": "call_1", "permission": "allow"}]
    )
    assert (tmp_path / "out.py").exists()
    assert (tmp_path / "out.py").read_text() == "x = 1\n"
    assert mgr.get_session(session_id).status == "completed"


async def test_permission_deny(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    calls = {"n": 0}

    def script(calls, kwargs):
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                "writing",
                [
                    _tc(
                        "call_1",
                        "write",
                        {"file_path": str(tmp_path / "out.py"), "content": "x=1\n"},
                    )
                ],
            )
        return _resp("Done.", [])

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": [],
                "deny": [],
                "ask": ["write-in-cwd"],
                "defaultMode": "allowAll",
            },
        },
    )
    session_id = await mgr.create_session("write it")
    assert mgr.get_session(session_id).status == "ask_permission"
    await mgr.reply_session(
        session_id, None, permission_replies=[{"toolCallId": "call_1", "permission": "deny"}]
    )
    assert not (tmp_path / "out.py").exists()
    assert mgr.get_session(session_id).status == "completed"


def test_bash_background_execution(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ctx = {"session_id": "bg-test", "project_root": str(tmp_path)}
    res = bash_handle({"command": "echo bg_output", "run_in_background": True}, ctx)
    assert res.ok
    assert "running in background" in res.output
    assert res.metadata and res.metadata.get("runInBackground") is True
    assert "backgroundTaskId" in res.metadata
    assert "stopCommand" in res.metadata
    out_path = pathlib.Path(res.metadata["outputPath"])
    assert out_path.is_file()


def test_git_file_history_lifecycle(tmp_path: pathlib.Path):
    git_dir = str(tmp_path / ".git_history")
    history = GitFileHistory(str(tmp_path), git_dir)

    sid = "sess-history-1"
    init_hash = history.ensure_session(sid)
    assert init_hash is not None

    test_file = tmp_path / "file1.txt"
    test_file.write_text("version 1")

    res1 = history.record_checkpoint(sid, [str(test_file)], "first version")
    assert res1.changed
    assert res1.checkpoint_hash is not None

    test_file.write_text("version 2")
    res2 = history.record_checkpoint(sid, [str(test_file)], "second version")
    assert res2.changed

    # Verify restoration to version 1
    assert history.can_restore(sid, res1.checkpoint_hash)
    history.restore(sid, res1.checkpoint_hash)
    assert test_file.read_text() == "version 1"

    # Fork session
    history.fork_session(sid, "sess-forked")
    assert history.get_current_checkpoint_hash(
        "sess-forked"
    ) == history.get_current_checkpoint_hash(sid)


async def test_session_undo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    target = tmp_path / "new_file.py"

    calls = {"n": 0}

    def script(calls, kwargs):
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                "writing",
                [_tc("call_1", "write", {"file_path": str(target), "content": "created file\n"})],
            )
        return _resp("Done.", [])

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": ["write-in-cwd", "read-in-cwd"],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
    )

    session_id = await mgr.create_session("create file")
    assert target.exists()
    assert target.read_text() == "created file\n"

    # Now perform undo
    undone = mgr.undo(session_id)
    assert undone
    assert not target.exists()


def test_llm_edit_correction_parsing():
    content = """
    ```xml
    <response>
      <corrected_old_string><![CDATA[foo = "bar"]]></corrected_old_string>
      <corrected_new_string><![CDATA[foo = "baz"]]></corrected_new_string>
    </response>
    ```
    """
    parsed = _parse_corrected_edit_strings(content)
    assert parsed is not None
    assert parsed["old_string"] == 'foo = "bar"'
    assert parsed["new_string"] == 'foo = "baz"'

    loose = _normalize_loose_text('  foo  =  \\"bar\\" \r\n')
    assert loose == 'foo = "bar"'


def test_skills_frontmatter_and_resources(tmp_path: pathlib.Path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: my-skill\ndescription: Test skill for unit tests\n---\n# My Skill\nUse this skill.\n"
    )
    res_file = skill_dir / "helper.py"
    res_file.write_text("def help(): pass\n")

    content = skill_file.read_text()
    meta = extract_skill_frontmatter(content)
    assert meta.get("name") == "my-skill"
    assert meta.get("description") == "Test skill for unit tests"

    body = strip_skill_prompt_metadata(content)
    assert body.startswith("# My Skill")

    files, truncated = list_skill_resource_files(str(skill_file))
    assert "helper.py" in files
    assert not truncated

    doc_prompt = build_skill_documents_prompt(
        [
            {
                "name": "my-skill",
                "content": content,
                "path": str(skill_file),
                "skillFilePath": str(skill_file),
            }
        ]
    )
    assert "<my-skill-skill" in doc_prompt
    assert "<skill_resources>" in doc_prompt
    assert "<file>helper.py</file>" in doc_prompt


def test_message_converter_tool_pairing_and_interrupted_recovery():
    converter = OpenAIMessageConverter()

    messages = [
        SessionMessage(id="1", session_id="s", role="system", content="System"),
        SessionMessage(id="2", session_id="s", role="user", content="User prompt"),
        SessionMessage(
            id="3",
            session_id="s",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "edit", "arguments": "{}"},
                },
            ],
        ),
        # Only call_1 has a corresponding tool response; call_2 was interrupted
        SessionMessage(
            id="4",
            session_id="s",
            role="tool",
            content='{"ok": true, "name": "read", "output": "line 1"}',
            tool_call_id="call_1",
        ),
        SessionMessage(id="5", session_id="s", role="user", content="Follow up"),
    ]

    converted = converter.convert_session_messages(messages, "gpt-4o")

    # Assert that tool message for call_1 is present
    call_1_tool = next((m for m in converted if m.get("tool_call_id") == "call_1"), None)
    assert call_1_tool is not None
    assert call_1_tool["role"] == "tool"

    # Assert that synthetic interrupted tool result was generated for call_2
    call_2_tool = next((m for m in converted if m.get("tool_call_id") == "call_2"), None)
    assert call_2_tool is not None
    assert call_2_tool["role"] == "tool"
    parsed_call_2 = json.loads(call_2_tool["content"])
    assert parsed_call_2["ok"] is False
    assert parsed_call_2["name"] == "edit"
    assert parsed_call_2["metadata"]["interrupted"] is True


def test_plan_mode_prompt_and_runtime_guidance(tmp_path: pathlib.Path):
    plan_prompt = get_plan_mode_prompt()
    assert "<proposed_plan>" in plan_prompt
    assert "Plan Mode" in plan_prompt

    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("Project specific coding instructions.")
    ctx = get_runtime_context(str(tmp_path), "gpt-4o")
    assert "gpt-4o" in ctx
    assert "Local Workspace Environment" in ctx
    assert "Project specific coding instructions" not in ctx
    instructions = load_agent_instructions(str(tmp_path))
    assert instructions is not None
    assert "Project specific coding instructions" in instructions

    interactive_prompt = get_system_prompt()
    assert "## AskUserQuestion" in interactive_prompt
    non_interactive_prompt = get_system_prompt({"nonInteractive": True})
    assert "## AskUserQuestion" not in non_interactive_prompt


def test_mcp_manager_basics():
    manager = McpManager()
    manager.prepare({"test-server": {"command": "echo", "args": ["1"]}})
    assert not manager.is_mcp_tool("unknown_tool")
    assert manager.get_mcp_tool_definitions() == []


def test_ask_user_question_tool():
    from coderai.core.tools.ask_user_question import handle as ask_handle

    ctx = {"session_id": "test_ask", "project_root": "/tmp"}

    # Invalid empty payload
    r_empty = ask_handle({}, ctx)
    assert not r_empty.ok
    assert "non-empty array" in r_empty.error

    # Valid questions payload
    payload = {
        "questions": [
            {
                "question": "Which framework?",
                "multiSelect": False,
                "options": [
                    {"label": "React", "description": "UI library"},
                    {"label": "Vue", "description": "Progressive framework"},
                ],
            },
            {
                "question": "Which features?",
                "multiSelect": True,
                "options": [
                    {"label": "Auth", "description": "User login"},
                    {"label": "Database", "description": "SQL storage"},
                ],
            },
        ]
    }
    r = ask_handle(payload, ctx)
    assert r.ok
    assert r.await_user_response is True
    assert "Waiting for user input." in r.output
    assert "1. Which framework?" in r.output
    assert "Mode: single-select" in r.output
    assert "- React" in r.output
    assert "2. Which features?" in r.output
    assert "Mode: multi-select" in r.output
    assert r.metadata is not None
    assert r.metadata.get("kind") == "ask_user_question"
    assert len(r.metadata.get("questions", [])) == 2


def test_update_plan_tool():
    from coderai.core.tools.update_plan import handle as plan_handle

    ctx = {"session_id": "test_plan", "project_root": "/tmp"}

    # Missing plan
    r_bad = plan_handle({}, ctx)
    assert not r_bad.ok
    assert "non-empty string" in r_bad.error

    # Valid plan
    plan_text = "# Task Plan\n- [x] Step 1: Read files\n- [ ] Step 2: Edit files\n"
    r_good = plan_handle({"plan": plan_text, "explanation": "Marked step 1 complete."}, ctx)
    assert r_good.ok
    assert r_good.output == "Plan updated."
    assert r_good.metadata is not None
    assert r_good.metadata["plan"] == plan_text
    assert r_good.metadata["explanation"] == "Marked step 1 complete."


def test_understand_image_tool(tmp_path: pathlib.Path):
    from coderai.core.tools.understand_image import handle as img_handle

    ctx = {"session_id": "test_img", "project_root": str(tmp_path)}

    # Missing prompt
    assert not img_handle({}, ctx).ok

    # Missing image_path
    assert not img_handle({"prompt": "describe"}, ctx).ok

    # Relative path
    assert not img_handle({"prompt": "describe", "image_path": "rel/path.png"}, ctx).ok

    # Unsupported format
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("not an image")
    assert not img_handle({"prompt": "describe", "image_path": str(txt_file)}, ctx).ok

    # Non-existent file
    assert not img_handle(
        {"prompt": "describe", "image_path": str(tmp_path / "missing.png")}, ctx
    ).ok

    # Valid image file with vision client
    png_file = tmp_path / "sample.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    class MockMsg:
        content = "Vision analysis of sample.png showing image details."

    class MockChoice:
        message = MockMsg()

    class MockResp:
        choices = [MockChoice()]

    class MockComp:
        def create(self, **kwargs):
            return MockResp()

    class MockChat:
        completions = MockComp()

    class MockClient:
        chat = MockChat()

    ctx_with_client = {
        "session_id": "test_img",
        "project_root": str(tmp_path),
        "create_openai_client": lambda: {"client": MockClient(), "model": "gpt-4o"},
    }
    r = img_handle({"prompt": "what is this", "image_path": str(png_file)}, ctx_with_client)
    assert r.ok
    assert "Vision analysis" in r.output
    assert r.metadata["imagePath"] == str(png_file.resolve())


def test_git_file_history_diff_and_checkpoints(tmp_path: pathlib.Path):
    git_dir = str(tmp_path / ".git_diff_test")
    history = GitFileHistory(str(tmp_path), git_dir)
    sid = "sess-diff-1"

    test_file = tmp_path / "module.py"
    test_file.write_text("def hello():\n    return 'world'\n")

    history.ensure_session(sid)
    history.record_checkpoint(sid, [str(test_file)], "Initial module.py")

    # Modify file
    test_file.write_text("def hello():\n    return 'coderai'\n")
    history.record_checkpoint(sid, [str(test_file)], "Updated greeting")

    diff = history.get_diff(sid)
    assert "-    return 'world'" in diff
    assert "+    return 'coderai'" in diff

    checkpoints = history.list_checkpoints(sid)
    assert len(checkpoints) >= 2
    assert any("Updated greeting" in c["message"] for c in checkpoints)


async def test_session_model_switching_and_diff(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    target = tmp_path / "app.py"
    target.write_text("v = 1\n")

    class Completions:
        def create(self, **kwargs):
            return _resp("Done.", [])

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": ["read-in-cwd", "write-in-cwd"],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
    )

    assert mgr.get_active_model() == "gpt-4o"
    mgr.set_model("claude-3-7-sonnet")
    assert mgr.get_active_model() == "claude-3-7-sonnet"

    sid = await mgr.create_session("inspect")
    target.write_text("v = 2\n")
    diff = mgr.get_diff(sid)
    assert "app.py" in diff or diff == ""


def test_cli_helpers():
    from coderai.cli.app import describe_scope, get_scope_color
    from coderai.cli.tool_card import parse_tool_message

    assert describe_scope("read-in-cwd") == "reads inside this workspace"
    assert describe_scope("network") == "network access"
    assert get_scope_color("read-in-cwd") == "green"
    assert get_scope_color("delete-in-cwd") == "red"

    msg = SessionMessage(
        id="1",
        session_id="s",
        role="tool",
        content=json.dumps(
            {"name": "Edit", "ok": True, "output": "Successfully edited file.py\nline2"}
        ),
    )
    name, summary, ok, meta = parse_tool_message(msg)
    assert name == "Edit"
    assert ok is True
    assert "Successfully edited file.py" in summary


def test_model_capabilities_and_badges():
    from coderai.core.common.model_capabilities import (
        DEEPSEEK_MODELS,
        DEEPSEEK_V4_MODELS,
        MULTIMODAL_MODELS,
        NON_MULTIMODAL_MODELS,
        THINKING_CAPABLE_MODELS,
        defaults_to_thinking_mode,
        format_capability_badges,
        get_model_badges,
        is_fast_model,
        supports_multimodal,
    )

    # Thinking capable checks
    assert "deepseek-v4-pro" in DEEPSEEK_MODELS
    assert "deepseek-v4-flash" in DEEPSEEK_MODELS
    assert DEEPSEEK_V4_MODELS == DEEPSEEK_MODELS
    assert "deepseek-v4-pro" in THINKING_CAPABLE_MODELS
    assert "gemini-3.7-flash" in THINKING_CAPABLE_MODELS
    assert "gpt-5.6-sol" in THINKING_CAPABLE_MODELS

    assert defaults_to_thinking_mode("gpt-5.6-sol") is True
    assert defaults_to_thinking_mode("gemini-3.7-flash") is True
    assert defaults_to_thinking_mode("deepseek-v4-pro") is True
    assert defaults_to_thinking_mode("gpt-5.6-terra") is False

    # Multimodal checks
    assert "gemini-3.7-flash" in MULTIMODAL_MODELS
    assert "gpt-5.6-luna" in MULTIMODAL_MODELS
    assert "gpt-5.6-sol" in MULTIMODAL_MODELS
    assert "gpt-5.6-terra" in MULTIMODAL_MODELS

    assert supports_multimodal("gpt-5.6-sol") is True
    assert supports_multimodal("gpt-5.6-terra") is True
    assert supports_multimodal("gemini-3.7-flash") is True
    assert supports_multimodal("gpt-5.6-luna") is True

    assert "deepseek-v4-pro" in NON_MULTIMODAL_MODELS
    assert "deepseek-v4-flash" in NON_MULTIMODAL_MODELS
    assert supports_multimodal("deepseek-v4-pro") is False
    assert supports_multimodal("deepseek-v4-flash") is False

    # Fast models
    assert is_fast_model("deepseek-v4-flash") is True
    assert is_fast_model("gemini-3.7-flash") is True
    assert is_fast_model("gpt-5.6-luna") is True

    # Badges formatting
    sol_badges = get_model_badges("gpt-5.6-sol")
    assert "Thinking" in sol_badges and "Multimodal" in sol_badges
    assert "[Thinking]" in format_capability_badges("gpt-5.6-sol")

    v4_badges = get_model_badges("deepseek-v4-flash")
    assert "Fast" in v4_badges


def test_openai_client_provider_routing(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    from coderai.core.openai_client import (
        create_openai_client,
        resolve_model_provider_routing,
    )

    # 1. DeepSeek routing
    url, key = resolve_model_provider_routing(
        "deepseek-v4-pro",
        env={"DEEPSEEK_API_KEY": "ds-key"},
    )
    assert "api.deepseek.com" in url
    assert key == "ds-key"

    # 2. Gemini routing
    url, key = resolve_model_provider_routing(
        "gemini-2.5-pro",
        env={"GEMINI_API_KEY": "gemini-key"},
    )
    assert "generativelanguage.googleapis.com" in url
    assert key == "gemini-key"

    # 3. Anthropic with OpenRouter or custom proxy
    url, key = resolve_model_provider_routing(
        "claude-3-7-sonnet",
        env={"OPENROUTER_API_KEY": "or-key"},
    )
    assert "openrouter.ai" in url
    assert key == "or-key"

    # 4. Explicit user custom baseURL takes priority
    url, key = resolve_model_provider_routing(
        "deepseek-v4-pro",
        explicit_base_url="https://custom.llm.proxy/v1",
        explicit_api_key="custom-key",
    )
    assert url == "https://custom.llm.proxy/v1"
    assert key == "custom-key"

    # 5. create_openai_client with model_override
    info = create_openai_client(str(tmp_path), model_override="gemini-3.7-flash")
    assert info["model"] == "gemini-3.7-flash"
    assert info["thinkingEnabled"] is True
    assert "generativelanguage.googleapis.com" in info["baseURL"]


def test_coderai_caps_settings_path(tmp_path: pathlib.Path):
    from coderai.core.settings import get_project_settings_path, read_project_settings

    caps_dir = tmp_path / ".coderAI"
    caps_dir.mkdir()
    (caps_dir / "settings.json").write_text(json.dumps({"model": "gpt-5.6-sol", "apiKey": "k1"}))

    settings_path = get_project_settings_path(str(tmp_path))
    assert ".coderAI" in settings_path

    data = read_project_settings(str(tmp_path))
    assert data is not None
    assert data.get("model") == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_session_max_iterations_bounded_termination(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Mock LLM that endlessly returns an update_plan tool call
    class EndlessCompletions:
        def create(self, **kwargs):
            return _resp("", [_tc("tc_plan", "UpdatePlan", {"plan": "Working..."})])

    class Chat:
        completions = EndlessCompletions()

    class Client:
        chat = Chat()

    assistant_messages = []
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": ["read-in-cwd", "write-in-cwd"],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
        on_assistant_message=lambda m, c: assistant_messages.append(m),
        max_iterations=10,
    )

    sid = await mgr.create_session("Endless task")
    session = mgr.get_session(sid)
    assert session is not None
    assert session.status == "completed"
    # Continuation message was sent to user
    assert any("hasn't reached a conclusion yet" in (m.content or "") for m in assistant_messages)


@pytest.mark.asyncio
async def test_session_per_model_usage_tracking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    class Chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                return _resp("Turn finished.")

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": Client(), "model": mgr.get_active_model()},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    # First turn on gpt-4o
    mgr.set_model("gpt-4o")
    sid = await mgr.create_session("Turn 1")
    s1 = mgr.get_session(sid)
    assert s1 is not None
    assert s1.usage_per_model is not None
    assert "gpt-4o" in s1.usage_per_model

    # Switch model to deepseek-v4-pro for turn 2
    mgr.set_model("deepseek-v4-pro")
    await mgr.reply_session(sid, "Turn 2")
    s2 = mgr.get_session(sid)
    assert s2 is not None
    assert "gpt-4o" in s2.usage_per_model
    assert "deepseek-v4-pro" in s2.usage_per_model


@pytest.mark.asyncio
async def test_session_deletion_and_forking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    class Chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                return _resp("Hello from origin.")

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": Client(), "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    sid = await mgr.create_session("Original session")
    assert mgr.get_session(sid) is not None

    # Fork session
    forked_id = mgr.fork_session(sid)
    assert forked_id is not None
    assert forked_id != sid
    forked_entry = mgr.get_session(forked_id)
    assert forked_entry is not None
    assert "[Fork]" in forked_entry.summary
    assert forked_entry.fork_of == sid

    # Verify forked messages copied
    orig_msgs = mgr.list_session_messages(sid)
    fork_msgs = mgr.list_session_messages(forked_id)
    assert len(orig_msgs) == len(fork_msgs)

    # Delete original session
    deleted = mgr.delete_session(sid)
    assert deleted is True
    assert mgr.get_session(sid) is None
    # Forked session still exists
    assert mgr.get_session(forked_id) is not None


@pytest.mark.asyncio
async def test_session_interruption_control(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    sid = "test_int_sess"
    mgr.interrupt_session(sid)
    assert mgr.is_interrupted(sid) is True
    entry = mgr.get_session(sid)
    assert entry is None or entry.status == "interrupted"


def test_skill_discovery_paths_and_enabled_filter(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    canonical = tmp_path / ".coderai" / "skills" / "tdd-workflow"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\nname: tdd-workflow\ndescription: Drive changes with tests first\n"
        "allow-implicit-invocation: true\n---\n# TDD\n"
    )
    legacy = tmp_path / ".coderAI" / "skills" / "security-audit"
    legacy.mkdir(parents=True)
    (legacy / "SKILLS.md").write_text(
        "---\nname: security-audit\ndescription: Audit code for security issues\n---\n# Audit\n"
    )
    manual = tmp_path / ".coderai" / "skills" / "manual-only"
    manual.mkdir()
    (manual / "SKILL.md").write_text(
        "---\nname: manual-only\ndescription: Manual skill\n"
        "allow-implicit-invocation: false\n---\n# Manual\n"
    )

    skills = list_skills(str(tmp_path))
    names = {s["name"] for s in skills}
    assert {"tdd-workflow", "security-audit", "manual-only"} <= names
    locations = {s["name"]: s["location"] for s in skills}
    assert locations["tdd-workflow"].endswith("SKILL.md")
    assert locations["security-audit"].endswith("SKILLS.md")

    filtered = list_skills(str(tmp_path), enabled_skills={"manual-only": False})
    assert "manual-only" not in {s["name"] for s in filtered}

    matched = match_skills_for_prompt("please use tdd-workflow on this repo", str(tmp_path))
    assert any(s["name"] == "tdd-workflow" for s in matched)
    assert all(s["name"] != "manual-only" for s in matched)

    parsed = parse_skill_match_response(
        '{"skillNames": ["tdd-workflow", "missing"]}',
        {"tdd-workflow", "security-audit"},
    )
    assert parsed == ["tdd-workflow"]


def test_edit_permission_resolves_snippet_path(tmp_path: pathlib.Path):
    sid = "perm_snip"
    clear_session_state(sid)
    target = tmp_path / "inside.py"
    target.write_text("x = 1\n")
    sn = create_snippet(sid, str(target), 1, 1, "x = 1")
    assert sn is not None

    plan = compute_tool_call_permissions(
        session_id=sid,
        project_root=str(tmp_path),
        tool_calls=[
            {
                "id": "call_edit",
                "type": "function",
                "function": {
                    "name": "edit",
                    "arguments": json.dumps(
                        {
                            "snippet_id": sn.id,
                            "old_string": "x = 1",
                            "new_string": "x = 2",
                        }
                    ),
                },
            }
        ],
        settings={"allow": [], "deny": [], "ask": ["write-in-cwd"], "defaultMode": "allowAll"},
        resolve_snippet_path=resolve_snippet_file_path,
    )
    assert plan["askPermissions"]
    assert plan["askPermissions"][0]["toolCallId"] == "call_edit"
    assert plan["askPermissions"][0]["scopes"] == ["write-in-cwd"]


def test_task_general_forced_ask_in_plan_mode():
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root="/tmp",
        tool_calls=[
            {
                "id": "call_task",
                "type": "function",
                "function": {
                    "name": "Task",
                    "arguments": json.dumps(
                        {
                            "description": "edit files",
                            "prompt": "change code",
                            "mode": "general",
                        }
                    ),
                },
            }
        ],
        settings={"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"},
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert plan["askPermissions"]
    assert plan["askPermissions"][0]["toolCallId"] == "call_task"


def test_settings_enabled_skills_and_mcp_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    from coderai.core.settings import resolve_current_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".coderai").mkdir()
    (home / ".coderai" / "settings.json").write_text(
        json.dumps(
            {
                "enabledSkills": {"alpha": True, "beta": False},
                "mcpServers": {"echo": {"command": "echo", "env": {"USER_KEY": "u"}}},
            }
        )
    )
    project = tmp_path / "proj"
    (project / ".coderai").mkdir(parents=True)
    (project / ".coderai" / "settings.json").write_text(
        json.dumps(
            {
                "enabledSkills": {"beta": True, "gamma": False},
                "mcpServers": {"echo": {"command": "echo", "env": {"PROJ_KEY": "p"}}},
            }
        )
    )
    from coderai.core.settings import DEFAULT_MODEL

    assert DEFAULT_MODEL == "gpt-5.6-luna"
    settings = resolve_current_settings(str(project))
    assert settings["model"] == "gpt-5.6-luna"
    assert settings["enabledSkills"]["alpha"] is True
    assert settings["enabledSkills"]["beta"] is True
    assert settings["enabledSkills"]["gamma"] is False
    assert settings["mcpServers"]["echo"]["env"]["USER_KEY"] == "u"
    assert settings["mcpServers"]["echo"]["env"]["PROJ_KEY"] == "p"


@pytest.mark.asyncio
async def test_session_injects_matched_skills_after_user_turn(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    skill_dir = tmp_path / ".coderai" / "skills" / "tdd-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tdd-workflow\ndescription: Drive changes with tests first\n---\n# TDD body\n"
    )

    class Completions:
        def create(self, **kwargs):
            return _resp("Done.")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": [],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
            "enabledSkills": {},
        },
    )
    assert mgr.tool_executor.mcp_manager is mgr.mcp_manager

    sid = await mgr.create_session("please follow tdd-workflow for this change")
    messages = mgr.list_session_messages(sid)
    roles = [m.role for m in messages]
    assert roles[:2] == ["system", "system"]
    skill_msgs = [m for m in messages if m.role == "system" and (m.meta or {}).get("skill")]
    assert skill_msgs
    assert skill_msgs[0].meta["skill"]["name"] == "tdd-workflow"
    assert "<tdd-workflow-skill" in skill_msgs[0].content
    user_idx = next(i for i, m in enumerate(messages) if m.role == "user")
    skill_idx = next(
        i for i, m in enumerate(messages) if m.role == "system" and (m.meta or {}).get("skill")
    )
    assert skill_idx > user_idx

    loaded = load_skill("tdd-workflow", str(tmp_path))
    assert loaded is not None
    mgr.inject_skills(sid, ["tdd-workflow"])
    assert sum(1 for m in mgr.list_session_messages(sid) if (m.meta or {}).get("skill")) == 1


@pytest.mark.asyncio
async def test_compaction_preserves_prefix_and_records_tokens(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    calls = {"n": 0}

    def script(_calls, kwargs):
        _calls["n"] += 1
        messages = kwargs.get("messages") or []
        if messages and "Primary Request" in str(messages[0].get("content", "")):
            return _resp("<analysis>skip</analysis>\nCompact summary here.")
        return _resp("Done.")

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": [],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
    )
    sid = await mgr.create_session("start")
    for i in range(6):
        mgr._append_message(mgr._build_message(sid, "user", f"turn {i}"))
        mgr._append_message(mgr._build_assistant(sid, f"ack {i}", None))

    before = mgr.list_session_messages(sid)
    prefix_ids = [m.id for m in before if m.role == "system"][:2]
    await mgr._compact_session(sid)
    after = mgr.list_session_messages(sid)
    assert [m.id for m in after[:2]] == prefix_ids
    assert any((m.meta or {}).get("isSummary") for m in after)
    from coderai.core.session_log import derive_messages

    derived = derive_messages(after)
    assert len(derived) < len(after)
    assert any((m.meta or {}).get("isSummary") for m in derived)
    entry = mgr.get_session(sid)
    assert entry is not None
    assert entry.active_tokens == 2


def test_multiline_yaml_frontmatter_parsing():
    from coderai.core.prompt import extract_skill_frontmatter, _implicit_invocation_allowed

    content = """---
name: advanced-skill
description: >
  This is a multiline folded description
  that spans multiple lines and provides
  extensive details about the skill.
allow-implicit-invocation: false
metadata:
  version: 2.1
---
# Advanced Skill Body
"""
    meta = extract_skill_frontmatter(content)
    assert meta["name"] == "advanced-skill"
    assert "multiline folded description" in meta["description"]
    assert meta["allow-implicit-invocation"] is False
    assert _implicit_invocation_allowed(meta) is False


def test_bundled_skills_discovery_and_content():
    from coderai.core.prompt import list_skills, load_skill, get_bundled_skills_root

    bundled_root = get_bundled_skills_root()
    assert pathlib.Path(bundled_root).is_dir()

    skills = list_skills()
    skill_names = {s["name"] for s in skills}
    expected_bundled = {"coderai-self-refer", "image-generator", "skill-digester", "skill-writer"}
    assert expected_bundled.issubset(skill_names)

    self_refer = load_skill("coderai-self-refer")
    assert self_refer is not None
    assert "Answers questions about" in self_refer["description"]
    assert len(self_refer["content"]) > 100

    writer = load_skill("skill-writer")
    assert writer is not None
    assert "Guide users through creating" in writer["description"]


def test_session_is_continue_prompt():
    mgr = SessionManager(
        project_root=".",
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )
    assert mgr.is_continue_prompt("/continue") is True
    assert mgr.is_continue_prompt("continue") is True
    assert mgr.is_continue_prompt("Continue") is True
    assert mgr.is_continue_prompt("proceed") is True
    assert mgr.is_continue_prompt("go on") is True
    assert mgr.is_continue_prompt({"text": "/continue"}) is True
    assert mgr.is_continue_prompt("please write code") is False
    assert mgr.is_continue_prompt({"text": "/continue", "imageUrls": ["data:..."]}) is False
    assert mgr.is_continue_prompt(None) is False


def test_background_process_completion_and_failure_log_tail(tmp_path: pathlib.Path):
    from coderai.core.tools.types import BackgroundProcessCompletion

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )

    sid = "test-bg-completion-sess"
    log_file = tmp_path / "failing_task.log"
    log_file.write_text(
        "Traceback (most recent call last):\n  File 'app.py', line 10\nZeroDivisionError\n"
    )

    failure = BackgroundProcessCompletion(
        task_id="task-123",
        process_id=9999,
        command="python app.py",
        output_path=str(log_file),
        ok=False,
        exit_code=1,
        signal=None,
        error="exit code 1",
        cwd=str(tmp_path),
        shell_path="/bin/sh",
        started_at_ms=1000,
        completed_at_ms=3500,
    )

    mgr.add_background_process_completion_message(sid, failure)
    messages = mgr.list_session_messages(sid)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "system"
    assert 'Background command "python app.py"' in msg.content
    assert "failed with exit code 1" in msg.content
    assert "<background_task_failure_log" in msg.content
    assert "ZeroDivisionError" in msg.content


def test_session_process_tracking_and_kill(tmp_path: pathlib.Path):
    home = tmp_path / "home"
    home.mkdir()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )

    sid = "test-process-tracking-sess"
    # Ensure session entry exists
    index = mgr._load_index()
    index["entries"].append({"id": sid, "summary": "test", "processes": {}, "status": "pending"})
    mgr._save_index(index)

    mgr._track_process_start(sid, 12345, "npm run dev")
    entry = mgr.get_session(sid)
    assert entry is not None
    assert entry.processes and "12345" in entry.processes
    assert entry.processes["12345"]["command"] == "npm run dev"

    mgr._track_process_exit(sid, 12345)
    entry2 = mgr.get_session(sid)
    assert entry2 is not None
    assert "12345" not in (entry2.processes or {})
