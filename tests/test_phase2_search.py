"""Phase 2 tests: glob/grep, bundled ripgrep, result caps, and spill-to-file."""

from __future__ import annotations

import os
import pathlib

import pytest

from coderai.core.permissions import describe_tool_permission_request
from coderai.core.prompt import TOOL_DOCS, get_tools
from coderai.core.spill import (
    apply_spill_policy,
    encode_segment,
    save_text,
    try_save_text,
    utf8_len,
)
from coderai.core.tools.registry import get_tool_registry
from coderai.core.tools.search import (
    GLOB_MAX_RESULTS,
    GREP_MAX_MATCHES,
    SearchError,
    build_glob_command,
    build_grep_command,
    format_grep_output,
    handle_glob_tool,
    handle_grep_tool,
    parse_glob_args,
    parse_grep_args,
    parse_grep_matches,
    render_glob_paths,
    resolve_rg_path,
    sample_across_top_level,
)
from coderai.core.tools.types import ToolExecutionContext


def _ctx(tmp_path: pathlib.Path, session_id: str = "s1") -> ToolExecutionContext:
    return ToolExecutionContext(session_id=session_id, project_root=str(tmp_path))


def _tree(tmp_path: pathlib.Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\n")
    (tmp_path / "src" / "b.ts").write_text("const beta = 2;\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "c.py").write_text("gamma = 3\nfindme = True\n")
    (tmp_path / ".hidden.py").write_text("hidden = True\n")
    (tmp_path / ".svn").mkdir()
    (tmp_path / ".svn" / "entries").write_text("12\n")


def test_registry_and_prompt_include_glob_grep():
    registry = get_tool_registry()
    assert registry.has_tool("glob")
    assert registry.has_tool("grep")
    assert not registry.has_tool("Glob")
    assert not registry.has_tool("Grep")
    names = {t["function"]["name"] for t in get_tools()}
    assert {"glob", "grep"} <= names
    assert "## glob" in TOOL_DOCS
    assert "## grep" in TOOL_DOCS
    assert "not shell find" in TOOL_DOCS
    assert "not shell grep or rg" in TOOL_DOCS


def test_glob_grep_permission_scopes(tmp_path: pathlib.Path):
    req = describe_tool_permission_request(
        session_id="s",
        project_root=str(tmp_path),
        tool_call={
            "id": "1",
            "function": {"name": "glob", "arguments": '{"pattern":"*.py"}'},
        },
    )
    assert req["scopes"] == ["read-in-cwd"]
    outside = describe_tool_permission_request(
        session_id="s",
        project_root=str(tmp_path),
        tool_call={
            "id": "2",
            "function": {
                "name": "grep",
                "arguments": '{"pattern":"x","path":"/tmp"}',
            },
        },
    )
    assert outside["scopes"] == ["read-out-cwd"]


def test_parse_glob_grep_args_reject_blank():
    with pytest.raises(ValueError, match="non-empty"):
        parse_glob_args({"pattern": "  "})
    with pytest.raises(ValueError, match="non-empty"):
        parse_grep_args({"pattern": ""})
    with pytest.raises(ValueError, match="positive glob"):
        parse_grep_args({"pattern": "x", "include": "!*.py"})
    with pytest.raises(ValueError, match="comma-separated"):
        parse_grep_args({"pattern": "x", "include": "*.py,*.ts"})
    parsed = parse_grep_args({"pattern": "x", "include": "*.{py,ts}"})
    assert parsed["include"] == "*.{py,ts}"


def test_build_glob_command_excludes_vcs_and_no_config_flags():
    argv = build_glob_command("*.py", "src")
    assert argv[0] == "--files"
    assert "--no-ignore" in argv
    assert "--hidden" in argv
    assert "--sort=modified" in argv
    assert "--glob=!**/.git" in argv
    assert argv[-2:] == ["--", "src"]
    grep_argv = build_grep_command("foo", include="*.py")
    assert grep_argv[0] == "--json"
    assert "--regexp=foo" in grep_argv
    assert "--glob=*.py" in grep_argv


def test_sample_across_top_level_round_robin():
    paths = [f"a/{i}.py" for i in range(5)] + [f"b/{i}.py" for i in range(5)] + ["c/only.py"]
    sample = sample_across_top_level(paths, 5)
    assert sample["shown"] == 3
    assert sample["total"] == 3
    assert len(sample["items"]) == 5
    # Grouped by top-level entry (dsh Map insertion order), not a flat interleaved list.
    assert all(
        p.startswith("a/") or p.startswith("b/") or p.startswith("c/") for p in sample["items"]
    )
    assert sample["items"][0].startswith("a/")
    assert any(p.startswith("b/") for p in sample["items"])
    assert any(p.startswith("c/") for p in sample["items"])


def test_glob_tool_finds_files_and_skips_vcs(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    _tree(tmp_path)
    result = handle_glob_tool({"pattern": "*.py"}, _ctx(tmp_path))
    assert result.ok
    assert "src/a.py" in (result.output or "")
    assert "lib/c.py" in (result.output or "")
    assert ".hidden.py" in (result.output or "")
    assert ".svn/entries" not in (result.output or "")
    assert "b.ts" not in (result.output or "")


def test_grep_tool_groups_matches(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    _tree(tmp_path)
    result = handle_grep_tool({"pattern": "findme", "include": "*.py"}, _ctx(tmp_path))
    assert result.ok
    assert "lib/c.py" in (result.output or "")
    assert "Line 2:" in (result.output or "")
    assert "Found 1 match" in (result.output or "")


def test_glob_spill_when_over_cap(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    for i in range(GLOB_MAX_RESULTS + 12):
        bucket = tmp_path / f"bucket{i % 4}"
        bucket.mkdir(exist_ok=True)
        (bucket / f"f{i}.txt").write_text("x\n")
    result = handle_glob_tool({"pattern": "*.txt"}, _ctx(tmp_path, "spill-glob"))
    assert result.ok
    assert "Showing" in (result.output or "")
    assert result.metadata and result.metadata.get("spill")
    locator = result.metadata["spill"]["locator"]
    assert pathlib.Path(locator).is_file()
    spilled = pathlib.Path(locator).read_text(encoding="utf-8")
    assert spilled.count("\n") + 1 >= GLOB_MAX_RESULTS + 12


def test_grep_spill_when_over_cap(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    lines = "\n".join(f"needle {i}" for i in range(GREP_MAX_MATCHES + 20))
    (tmp_path / "hits.py").write_text(lines + "\n")
    result = handle_grep_tool({"pattern": "needle"}, _ctx(tmp_path, "spill-grep"))
    assert result.ok
    assert "of" in (result.output or "")
    assert result.metadata and result.metadata.get("spill")
    locator = result.metadata["spill"]["locator"]
    full = pathlib.Path(locator).read_text(encoding="utf-8")
    assert f"Found {GREP_MAX_MATCHES + 20} matches" in full


def test_render_glob_empty_and_within_cap():
    assert render_glob_paths([]) == "No files found"
    assert render_glob_paths(["a.py", "b.py"]) == "a.py\nb.py"


def test_spill_store_private_and_safe_names(tmp_path: pathlib.Path):
    ref = save_text(
        session_id="owner",
        suggested_name="../etc/passwd",
        content="hello",
        root=tmp_path,
    )
    path = pathlib.Path(ref.locator)
    assert path.is_file()
    assert "/" not in path.name
    assert encode_segment("../x") == "..~002Fx"
    assert path.parent.name.startswith("session-")
    assert path.read_text(encoding="utf-8") == "hello"
    assert try_save_text(session_id="", suggested_name="x.txt", content="n") is None


def test_preview_head_tail_stays_within_budget():
    from coderai.core.spill import preview_head_tail

    text = "x" * 5000
    preview, omitted = preview_head_tail(text, 100)
    assert utf8_len(preview) <= 100
    assert omitted > 0
    assert "\n...\n" in preview
    empty, omitted_all = preview_head_tail(text, 0)
    assert empty == ""
    assert omitted_all == 5000


def test_apply_spill_policy_keeps_small_and_spills_large(tmp_path: pathlib.Path):
    small, ref = apply_spill_policy("tiny", session_id="s", tool_name="bash", max_inline_bytes=100)
    assert ref is None
    assert small == "tiny"
    payload = "x" * 5000
    preview, ref = apply_spill_policy(
        payload,
        session_id="s",
        tool_name="bash",
        max_inline_bytes=2000,
        suggested_name="bash.txt",
        root=tmp_path,
    )
    assert ref is not None
    assert utf8_len(preview) <= 2000
    assert "\n...\n" in preview
    assert "Full formatted result stored at:" in preview
    assert pathlib.Path(ref.locator).read_text(encoding="utf-8") == payload


def test_parse_grep_json_and_skip_non_match():
    stdout = (
        '{"type":"begin","data":{"path":{"text":"a.py"}}}\n'
        '{"type":"match","data":{"path":{"text":"a.py"},"line_number":3,"lines":{"text":"hello\\n"}}}\n'
        '{"type":"end","data":{}}\n'
    )
    matches = parse_grep_matches(stdout)
    assert len(matches) == 1
    assert matches[0].path == "a.py"
    assert matches[0].line_number == 3
    assert matches[0].line == "hello"
    with pytest.raises(SearchError) as exc:
        parse_grep_matches("not-json\n")
    assert exc.value.code == "SEARCH_FAILED"


def test_format_grep_empty():
    assert format_grep_output([], seen=0, truncated=False) == "No matches found"


def test_bundled_or_path_rg_resolves():
    path = resolve_rg_path()
    if path:
        assert os.path.isfile(path)
        assert os.access(path, os.X_OK)


@pytest.mark.skipif(resolve_rg_path() is None, reason="ripgrep binary not available")
def test_glob_via_bundled_rg(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)
    _tree(tmp_path)
    result = handle_glob_tool({"pattern": "*.py"}, _ctx(tmp_path))
    assert result.ok
    assert "src/a.py" in (result.output or "")
    assert ".svn/entries" not in (result.output or "")
