"""Offline self-check for coderai.core — no network, no LLM needed."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, ".")

from coderai.core.common.file_history import GitFileHistory
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    compute_tool_call_permissions,
    evaluate_permission_scopes,
)
from coderai.core.prompt import (
    build_skill_documents_prompt,
    extract_skill_frontmatter,
    get_plan_mode_prompt,
    get_runtime_context,
    get_tools,
    list_skill_resource_files,
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
from coderai.core.tools.edit import handle as edit_handle
from coderai.core.tools.read import handle as read_handle
from coderai.core.tools.write import handle as write_handle


def assert_eq(a: Any, b: Any, msg: str = "") -> None:
    if a != b:
        raise AssertionError(f"{msg}: {a!r} != {b!r}")


def make_mock_client(script: Any) -> tuple[Any, dict[str, int]]:
    calls = {"n": 0}

    def create(**kwargs: Any) -> Any:
        calls["n"] += 1
        return script(calls, kwargs)

    class Completions:
        def create(self, **kwargs: Any) -> Any:
            return create(**kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    return Client(), calls


def resp(content: str, tool_calls: Any = None, usage: Any = None) -> Any:
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
    usage_obj = type("U", (), usage or {})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": usage_obj})()


def tc(cid: str, name: str, args: dict[str, Any]) -> Any:
    return type(
        "TC",
        (),
        {"id": cid, "function": type("F", (), {"name": name, "arguments": json.dumps(args)})()},
    )()


async def main() -> None:
    # 1. snippet lifecycle + staleness
    sid = "test-sess-1"
    clear_session_state(sid)
    sn = create_snippet(sid, "/tmp/a.py", 1, 10, "print(1)")
    assert sn and sn.id == "snippet_1"
    assert get_snippet(sid, "snippet_1") == sn
    assert not has_snippet_outdated_file_version(sid, sn)
    record_file_state(
        sid, FileState(file_path="/tmp/a.py", content="", timestamp=0), increment_version=True
    )
    assert has_snippet_outdated_file_version(sid, sn)
    print("✓ snippet lifecycle + staleness")

    # 2. permission scopes & plan mode forced ask scopes
    assert (
        evaluate_permission_scopes(
            ["read-in-cwd"],
            {"allow": ["read-in-cwd"], "deny": [], "ask": [], "defaultMode": "allowAll"},
        )
        == "allow"
    )
    assert (
        evaluate_permission_scopes(
            ["write-in-cwd"], {"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"}
        )
        == "allow"
    )
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
    assert (
        evaluate_permission_scopes(
            ["write-in-cwd"],
            {"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"},
            force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
        )
        == "ask"
    )
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
    assert plan["askPermissions"], "git command should ask"
    print("✓ permissions & plan mode forced scopes")

    # 3. tool definitions include the built-in surface
    names = {t["function"]["name"] for t in get_tools()}
    assert {"bash", "read", "write", "edit", "WebSearch", "UpdatePlan"} <= names
    assert "<proposed_plan>" in get_plan_mode_prompt()
    print("✓ tool definitions & plan mode prompt")

    # 4. tools on real temp files (snippet-scoped edit + staleness)
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "hello.py"
        p.write_text("a=1\nb=2\nc=3\n")
        ctx = {"session_id": "tools-sess", "project_root": tmp}
        clear_session_state("tools-sess")
        r = read_handle({"file_path": str(p)}, ctx)
        assert r.ok and r.metadata and "snippet" in r.metadata, r.error
        snippet_id = r.metadata["snippet"]["id"]
        e = edit_handle({"snippet_id": snippet_id, "old_string": "a=1", "new_string": "a=99"}, ctx)
        assert e.ok, e.error
        assert "a=99" in p.read_text()
        # same snippet still editable as long as old_string is found in scope
        e2 = edit_handle({"snippet_id": snippet_id, "old_string": "b=2", "new_string": "b=99"}, ctx)
        assert e2.ok, e2.error
        # stale snippet: old_string no longer in scope -> reject with re-read hint
        e3 = edit_handle({"snippet_id": snippet_id, "old_string": "a=1", "new_string": "a=0"}, ctx)
        assert not e3.ok and "changed" in e3.error.lower()
        w = write_handle({"file_path": str(pathlib.Path(tmp) / "new.py"), "content": "x=1\n"}, ctx)
        assert w.ok
        print("✓ snippet-scoped edit + staleness + write")

    # 5. Background bash tool execution
    with tempfile.TemporaryDirectory() as tmp:
        ctx = {"session_id": "bg-sess", "project_root": tmp}
        res = bash_handle({"command": "echo bg_check", "run_in_background": True}, ctx)
        assert res.ok, res.error
        assert res.metadata and res.metadata.get("runInBackground") is True
        time.sleep(0.2)
        assert pathlib.Path(res.metadata["outputPath"]).exists()
        print("✓ bash run_in_background")

    # 6. GitFileHistory checkpoint + restore + fork
    with tempfile.TemporaryDirectory() as tmp:
        history = GitFileHistory(tmp, str(pathlib.Path(tmp) / ".git_history"))
        sid = "history-sess"
        history.ensure_session(sid)
        tf = pathlib.Path(tmp) / "file.txt"
        tf.write_text("initial content")
        c1 = history.record_checkpoint(sid, [str(tf)], "c1")
        assert c1.changed and c1.checkpoint_hash
        tf.write_text("modified content")
        c2 = history.record_checkpoint(sid, [str(tf)], "c2")
        assert c2.changed
        history.restore(sid, c1.checkpoint_hash)
        assert tf.read_text() == "initial content"
        history.fork_session(sid, "forked-sess")
        assert history.get_current_checkpoint_hash("forked-sess") == c1.checkpoint_hash
        print("✓ GitFileHistory checkpoint + restore + fork")

    # 7. Skills frontmatter & resource scanning & AGENTS.md runtime guidance
    with tempfile.TemporaryDirectory() as tmp:
        sdir = pathlib.Path(tmp) / "sample-skill"
        sdir.mkdir()
        sfile = sdir / "SKILL.md"
        sfile.write_text(
            "---\nname: sample\ndescription: sample skill description\n---\n# Sample\nBody"
        )
        (sdir / "resource.txt").write_text("res")
        meta = extract_skill_frontmatter(sfile.read_text())
        assert meta.get("name") == "sample"
        assert strip_skill_prompt_metadata(sfile.read_text()).startswith("# Sample")
        rfiles, _ = list_skill_resource_files(str(sfile))
        assert "resource.txt" in rfiles
        doc_prompt = build_skill_documents_prompt(
            [{"name": "sample", "content": sfile.read_text(), "path": str(sfile)}]
        )
        assert "<sample-skill" in doc_prompt

        (pathlib.Path(tmp) / "AGENTS.md").write_text("Test AGENTS guidance")
        runtime_ctx = get_runtime_context(tmp, "gpt-4o")
        assert "Test AGENTS guidance" in runtime_ctx
        print("✓ skills frontmatter + resources + project guidance")

    # 8. MessageConverter tool pairing & synthetic interrupted tool recovery
    converter = OpenAIMessageConverter()
    msgs = [
        SessionMessage(id="1", session_id="s", role="system", content="System"),
        SessionMessage(id="2", session_id="s", role="user", content="User prompt"),
        SessionMessage(
            id="3",
            session_id="s",
            role="assistant",
            content="",
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "edit", "arguments": "{}"}},
            ],
        ),
        SessionMessage(
            id="4",
            session_id="s",
            role="tool",
            content='{"ok": true, "name": "read"}',
            tool_call_id="c1",
        ),
        SessionMessage(id="5", session_id="s", role="user", content="Next user turn"),
    ]
    converted = converter.convert_session_messages(msgs, "gpt-4o")
    interrupted_tool = next((m for m in converted if m.get("tool_call_id") == "c2"), None)
    assert interrupted_tool is not None
    assert json.loads(interrupted_tool["content"])["metadata"]["interrupted"] is True
    print("✓ message converter tool pairing + interrupted recovery")

    # 9. SessionManager bounded loop with mock client + undo
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp, "new_app.py")

        def script(calls: dict[str, int], kwargs: Any) -> Any:
            if calls["n"] == 1:
                return resp(
                    "I'll write it.",
                    [tc("call_1", "write", {"file_path": str(target), "content": "print(99)\n"})],
                )
            return resp("Done.", [])

        client, _ = make_mock_client(script)
        mgr = SessionManager(
            project_root=tmp,
            create_openai_client=lambda: {
                "client": client,
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
        session_id = await mgr.create_session("write it")
        assert (pathlib.Path.home() / ".coderai" / "projects").exists()
        messages = mgr.list_session_messages(session_id)
        assert any(m.role == "tool" for m in messages), "expected a tool result in the transcript"
        assert messages[-1].content == "Done."
        assert target.read_text() == "print(99)\n"

        # Test session undo
        undone = mgr.undo(session_id)
        assert undone
        assert not target.exists()
        print("✓ bounded loop + JSONL persistence + undo")

    # 10. AskUserQuestion & UpdatePlan & UnderstandImage tools
    with tempfile.TemporaryDirectory() as tmp:
        from coderai.core.tools.ask_user_question import handle as ask_handle
        from coderai.core.tools.update_plan import handle as plan_handle
        from coderai.core.tools.understand_image import handle as img_handle

        tctx = {"session_id": "sc_tools", "project_root": tmp}
        ask_res = ask_handle(
            {
                "questions": [
                    {
                        "question": "Choose mode",
                        "options": [{"label": "Fast"}, {"label": "Accurate"}],
                    }
                ]
            },
            tctx,
        )
        assert ask_res.ok and ask_res.await_user_response
        assert "Waiting for user input." in ask_res.output

        plan_res = plan_handle({"plan": "- [x] Done\n- [ ] Todo", "explanation": "Progress"}, tctx)
        assert plan_res.ok and plan_res.metadata and plan_res.metadata["plan"].startswith("- [x]")

        img_file = pathlib.Path(tmp) / "img.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        img_res = img_handle({"prompt": "describe", "image_path": str(img_file)}, tctx)
        assert img_res.ok
        print("✓ AskUserQuestion + UpdatePlan + UnderstandImage tools")

    # 11. GitFileHistory diff and checkpoints
    with tempfile.TemporaryDirectory() as tmp:
        history = GitFileHistory(tmp, str(pathlib.Path(tmp) / ".git_history"))
        sid = "diff-sess"
        history.ensure_session(sid)
        df_path = pathlib.Path(tmp) / "code.py"
        df_path.write_text("orig = 1\n")
        history.record_checkpoint(sid, [str(df_path)], "init")
        df_path.write_text("orig = 2\n")
        history.record_checkpoint(sid, [str(df_path)], "update")
        diff_str = history.get_diff(sid)
        assert "-orig = 1" in diff_str and "+orig = 2" in diff_str
        assert len(history.list_checkpoints(sid)) >= 2
        print("✓ GitFileHistory diff + checkpoint log")

    # 12. Dynamic model switching and CLI helpers
    from coderai.cli.app import describe_scope, get_scope_color
    from coderai.cli.tool_card import parse_tool_message
    from coderai.core.common.model_capabilities import (
        THINKING_CAPABLE_MODELS,
        defaults_to_thinking_mode,
        supports_multimodal,
    )
    from coderai.core.openai_client import resolve_model_provider_routing

    assert describe_scope("write-in-cwd") == "writes inside this workspace"
    assert get_scope_color("write-in-cwd") == "yellow"
    test_msg = SessionMessage(
        id="1", session_id="s", role="tool", content='{"name": "bash", "ok": true, "output": "ok"}'
    )
    tname, tsummary, tok, _ = parse_tool_message(test_msg)
    assert tname == "bash" and tok is True

    # Check frontier models matrix
    assert {
        "gpt-5.6-sol",
        "claude-3-7-sonnet",
        "gemini-2.5-pro",
        "deepseek-v4-pro",
    } <= THINKING_CAPABLE_MODELS
    assert defaults_to_thinking_mode("gpt-5.6-sol")
    assert defaults_to_thinking_mode("deepseek-r1")
    assert supports_multimodal("gpt-5.6-terra")
    assert not supports_multimodal("deepseek-v4-pro")

    # Check endpoint auto-routing
    ds_url, _ = resolve_model_provider_routing("deepseek-v4-pro")
    assert "api.deepseek.com" in ds_url
    gemini_url, _ = resolve_model_provider_routing("gemini-2.5-pro")
    assert "generativelanguage.googleapis.com" in gemini_url

    print("✓ dynamic model switching + CLI formatting helpers + frontier model routing")

    print("\nAll self-checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
