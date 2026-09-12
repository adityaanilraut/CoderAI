# Ported from coderai/core/tools/edit.py - kimi structure (kimi_cli/tools/file/replace.py).
"""edit tool — snippet-scoped replacement with LLM correction fallback."""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re
from typing import Any

from coderai.utils.path import (
    build_diff_preview,
    has_file_changed_since_state,
    read_text_file_with_metadata,
)
from coderai.utils.common.openai_thinking import build_thinking_request_options
from coderai.utils.common.string_matcher import (
    find_occurrences as _find_occurrences,
    match_multistage,
    normalize_escaping as _normalize_escaping,
    normalize_loose_text as _normalize_loose_text,
    normalize_quotes as _normalize_quotes,
)
from coderai.utils.common.validate import execute_validated_tool, semantic_boolean, semantic_integer
from coderai.state import (
    FileSnippet,
    FileState,
    create_full_file_snippet,
    create_snippet,
    get_file_state,
    get_snippet,
    has_snippet_outdated_file_version,
    is_absolute_file_path,
    normalize_file_path,
    record_file_state,
)
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.tools.file.utils import check_file_write_access, write_file_with_callbacks

MAX_CANDIDATE_COUNT = 5
REPLACE_ALL_MATCH_THRESHOLD = 5
SHORT_REPLACE_ALL_LENGTH = 40
OUTDATED_SNIPPET_NOT_FOUND_ERROR = (
    "old_string was not found in this snippet scope. The file has changed since this snippet was created. "
    "Read the file again before editing."
)


def _validate_edit_schema(args: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    snippet_id = args.get("snippet_id")
    file_path = args.get("file_path") or args.get("path")
    if (not isinstance(snippet_id, str) or not snippet_id.strip()) and (
        not isinstance(file_path, str) or not file_path.strip()
    ):
        return False, {}, "Either file_path or snippet_id is required."

    old_string = args.get("old_string")
    new_string = args.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return False, {}, "old_string and new_string must be strings."

    replace_all = semantic_boolean(args.get("replace_all", False))
    exp_ok, expected_occurrences, exp_err = semantic_integer(
        args.get("expected_occurrences"), "expected_occurrences", min_val=1
    )
    if not exp_ok:
        return False, {}, exp_err

    validated = dict(args)
    if isinstance(snippet_id, str) and snippet_id.strip():
        validated["snippet_id"] = snippet_id.strip()
    if isinstance(file_path, str) and file_path.strip():
        validated["file_path"] = file_path.strip()
    validated["old_string"] = old_string
    validated["new_string"] = new_string
    validated["replace_all"] = replace_all
    validated["expected_occurrences"] = expected_occurrences
    return True, validated, None


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_edit_tool(args, context)


def handle_edit_tool(args: dict[str, Any], context: Any) -> ToolResult:
    def run(validated_args: dict[str, Any], ctx: Any) -> ToolResult:
        if isinstance(ctx, dict):
            session_id = ctx.get("session_id") or "default"
        else:
            session_id = getattr(ctx, "session_id", None) or "default"
        snippet_id = validated_args.get("snippet_id")
        file_path_arg = as_str(validated_args.get("file_path")).strip()

        if snippet_id:
            snippet = get_snippet(session_id, snippet_id)
            if not snippet:
                return ToolResult(ok=False, name="edit", error=f"Unknown snippet_id: {snippet_id}")
            file_path = normalize_file_path(file_path_arg if file_path_arg else snippet.file_path)
            if not is_absolute_file_path(file_path):
                project_root = (
                    ctx.get("project_root")
                    if isinstance(ctx, dict)
                    else getattr(ctx, "project_root", None)
                ) or "."
                file_path = normalize_file_path(str(pathlib.Path(project_root) / file_path))
            if snippet.file_path != file_path and not file_path.endswith(snippet.file_path):
                return ToolResult(
                    ok=False,
                    name="edit",
                    error="snippet_id does not belong to the provided file_path.",
                )
        else:
            if not file_path_arg:
                return ToolResult(
                    ok=False, name="edit", error="file_path is required when snippet_id is omitted."
                )
            iso_val = (
                ctx.get("isolated_cwd")
                if isinstance(ctx, dict)
                else getattr(ctx, "isolated_cwd", None)
            )
            isolated_cwd = (
                str(iso_val)
                if isinstance(iso_val, (str, pathlib.Path))
                and "MagicMock" not in str(type(iso_val))
                else None
            )

            pr_val = (
                ctx.get("project_root")
                if isinstance(ctx, dict)
                else getattr(ctx, "project_root", None)
            )
            project_root = (
                str(pr_val)
                if isinstance(pr_val, (str, pathlib.Path)) and "MagicMock" not in str(type(pr_val))
                else "."
            )
            effective_root = isolated_cwd or project_root
            file_path = (
                file_path_arg
                if is_absolute_file_path(file_path_arg)
                else str(pathlib.Path(effective_root) / file_path_arg)
            )

            file_path = normalize_file_path(file_path)
            p_check = pathlib.Path(file_path)
            if not p_check.exists():
                return ToolResult(ok=False, name="edit", error=f"File not found: {file_path}")

            if p_check.is_dir():
                return ToolResult(ok=False, name="edit", error="file_path points to a directory.")
            try:
                content_res = read_text_file_with_metadata(file_path)
                lines = content_res["content"].splitlines()
                total_lines = max(1, len(lines))
                snippet = create_full_file_snippet(
                    session_id, file_path, 1, total_lines, content_res["content"]
                )
                from coderai.state import mark_file_read
                from coderai.tools.legacy.observation import get_observation_tracker

                get_observation_tracker().record_observation(
                    session_id, file_path, content_res["content"]
                )
                mark_file_read(session_id, file_path, content_res)
                file_state = get_file_state(session_id, file_path)
            except Exception as e:
                return ToolResult(
                    ok=False, name="edit", error=f"Error reading file before editing: {e}"
                )

        if snippet is None:
            return ToolResult(ok=False, name="edit", error="Could not establish an edit scope.")

        old_string = validated_args["old_string"]
        new_string = validated_args["new_string"]

        if old_string == new_string:
            return ToolResult(
                ok=False, name="edit", error="new_string must differ from old_string."
            )

        p = pathlib.Path(file_path)
        if not p.exists():
            return ToolResult(ok=False, name="edit", error=f"File not found: {file_path}")
        if p.is_dir():
            return ToolResult(ok=False, name="edit", error="file_path points to a directory.")

        sandbox_error = check_file_write_access(ctx, file_path)
        if sandbox_error:
            return ToolResult(ok=False, name="edit", error=sandbox_error)

        file_state = get_file_state(session_id, file_path)
        if not file_state:
            return ToolResult(ok=False, name="edit", error="Must read file before editing.")

        if has_file_changed_since_state(file_path, file_state):
            return ToolResult(
                ok=False,
                name="edit",
                error="File has been modified since read. Read it again before editing.",
            )

        from coderai.tools.legacy.observation import get_observation_tracker

        allowed, obs_err = get_observation_tracker().check_mutation_allowed(session_id, file_path)
        if not allowed and obs_err:
            return ToolResult(
                ok=False,
                name="edit",
                error=obs_err,
                metadata={
                    "scope": _format_scope_metadata(
                        _build_search_scope(file_path, "", snippet), snippet
                    )
                },
            )

        try:
            metadata = read_text_file_with_metadata(file_path)
        except Exception as e:
            return ToolResult(ok=False, name="edit", error=str(e))

        raw = metadata["content"]
        scope = _build_search_scope(file_path, raw, snippet)
        scope_text = raw[scope["start_offset"] : scope["end_offset"]]

        replace_all = validated_args["replace_all"]
        replacement_old = old_string
        replacement_new = new_string
        matched_via = "exact"

        if old_string == "":
            if raw != "":
                return ToolResult(
                    ok=False,
                    name="edit",
                    error="old_string must not be empty unless the file is empty.",
                    metadata={"scope": _format_scope_metadata(scope, snippet)},
                )
            matches = [(0, 0)]
            matched_via = "empty_file"
        else:
            match_res = match_multistage(scope_text, old_string, new_string)
            matches = match_res.matches
            if matches:
                matched_via = match_res.matched_via
                replacement_old = match_res.replaced_old
                replacement_new = match_res.replaced_new

        # 2. LLM loose escape and quotation marks correction (last resort fallback)
        if not matches:
            loose_candidate = _find_loose_candidate(scope_text, old_string)
            if loose_candidate:
                corrected = _correct_escaped_strings_with_llm(
                    scope_text, old_string, new_string, loose_candidate, ctx
                )
                if corrected:
                    corr_matches = _find_occurrences(scope_text, corrected["old_string"])
                    if len(corr_matches) == 1:
                        matches = corr_matches
                        matched_via = "llm_escape_correction"
                        replacement_old = corrected["old_string"]
                        replacement_new = corrected["new_string"]

        # 3. Not found handling
        if not matches:
            if has_snippet_outdated_file_version(session_id, snippet):
                return ToolResult(
                    ok=False,
                    name="edit",
                    error=OUTDATED_SNIPPET_NOT_FOUND_ERROR,
                    metadata={"scope": _format_scope_metadata(scope, snippet)},
                )

            not_found_reason = _infer_old_string_not_found_reason_with_llm(
                raw, scope, old_string, new_string, ctx
            )
            error_msg = (
                f"old_string not found in file. {not_found_reason}"
                if not_found_reason
                else "old_string not found in file."
            )
            return ToolResult(
                ok=False,
                name="edit",
                error=error_msg,
                metadata={"scope": _format_scope_metadata(scope, snippet)},
            )

        # Uniqueness check when replace_all is False
        if not replace_all and len(matches) > 1:
            return ToolResult(
                ok=False,
                name="edit",
                error="old_string is not unique; use snippet_id, replace_all, or provide more context.",
                metadata={
                    "match_count": len(matches),
                    "scope": _format_scope_metadata(scope, snippet),
                    "candidates": _build_candidate_metadata(
                        session_id, file_path, raw, matches, scope
                    ),
                },
            )

        expected_occurrences = validated_args.get("expected_occurrences")
        guard_error = _validate_replace_all_guard(
            replace_all=replace_all,
            match_count=len(matches),
            old_string=replacement_old,
            expected_occurrences=expected_occurrences,
        )
        if guard_error:
            return ToolResult(
                ok=False,
                name="edit",
                error=guard_error,
                metadata={
                    "match_count": len(matches),
                    "scope": _format_scope_metadata(scope, snippet),
                    "candidates": _build_candidate_metadata(
                        session_id, file_path, raw, matches, scope
                    ),
                },
            )

        updated_content = _apply_replacement(
            raw, scope, matches, replacement_old, replacement_new, replace_all
        )
        diff_preview = build_diff_preview(file_path, raw, updated_content)

        from coderai.tools.file.utils import generate_virtual_patch, is_dry_run

        if is_dry_run(ctx, validated_args):
            patch = generate_virtual_patch(file_path, raw, updated_content)
            replaced_count = len(matches) if replace_all else 1
            return ToolResult(
                ok=True,
                name="edit",
                output=f"[dry-run] Virtual edit preview for {file_path}:\n{patch['diff']}"
                if patch["diff"]
                else f"[dry-run] Verified replacement in {file_path}.",
                metadata={
                    "file_path": file_path,
                    "replaced_count": replaced_count,
                    "dry_run": True,
                    "diff_preview": diff_preview or patch["diff"],
                    "virtual_patch": patch,
                    "lines_added": patch["lines_added"],
                    "lines_removed": patch["lines_removed"],
                    "read_scope_type": snippet.scope_type,
                    "scope": _format_scope_metadata(scope, snippet),
                    "matched_via": matched_via,
                },
            )

        try:
            write_file_with_callbacks(
                ctx, file_path, updated_content, metadata["encoding"], metadata["lineEndings"]
            )

            fresh_metadata = read_text_file_with_metadata(file_path)
            record_file_state(
                session_id,
                FileState(
                    file_path=file_path,
                    content=fresh_metadata["content"],
                    timestamp=fresh_metadata["timestamp"],
                    encoding=fresh_metadata["encoding"],
                    line_endings=fresh_metadata["lineEndings"],
                ),
                increment_version=True,
            )
            get_observation_tracker().record_observation(
                session_id, file_path, content=fresh_metadata["content"]
            )
        except Exception as e:
            return ToolResult(ok=False, name="edit", error=str(e))

        replaced_count = len(matches) if replace_all else 1
        return ToolResult(
            ok=True,
            name="edit",
            output=f"Replaced {replaced_count} occurrence(s) in {file_path}.",
            metadata={
                "file_path": file_path,
                "replaced_count": replaced_count,
                "cache_refreshed": True,
                "read_scope_type": snippet.scope_type,
                "line_endings": fresh_metadata["lineEndings"],
                "diff_preview": diff_preview,
                "scope": _format_scope_metadata(scope, snippet),
                "matched_via": matched_via,
            },
        )

    return execute_validated_tool("edit", args, context, run, validator=_validate_edit_schema)


def _build_search_scope(file_path: str, raw: str, snippet: FileSnippet) -> dict[str, Any]:
    lines = raw.split("\n")
    start_line = max(1, min(snippet.start_line, len(lines)))
    end_line = max(start_line, min(snippet.end_line, len(lines)))

    def offset_of(line: int) -> int:
        if line <= 1:
            return 0
        return len("\n".join(lines[: line - 1])) + 1

    start_offset = offset_of(start_line)
    end_offset = offset_of(end_line + 1) if end_line < len(lines) else len(raw)
    return {
        "start_offset": start_offset,
        "end_offset": end_offset,
        "start_line": start_line,
        "end_line": end_line,
        "file_path": file_path,
        "snippet_id": snippet.id,
    }


def _apply_replacement(
    raw: str,
    scope: dict[str, Any],
    matches: list[tuple[int, int]],
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> str:
    scope_start = scope["start_offset"]
    if not replace_all:
        s, e = matches[0]
        return raw[: scope_start + s] + new_string + raw[scope_start + e :]

    result: list[str] = []
    last_idx = 0
    for s, e in matches:
        abs_s = scope_start + s
        abs_e = scope_start + e
        result.append(raw[last_idx:abs_s])
        result.append(new_string)
        last_idx = abs_e
    result.append(raw[last_idx:])
    return "".join(result)


def _validate_replace_all_guard(
    replace_all: bool, match_count: int, old_string: str, expected_occurrences: int | None
) -> str | None:
    if not replace_all:
        if expected_occurrences is not None and expected_occurrences != 1:
            return "expected_occurrences can only be greater than 1 when replace_all is true."
        return None

    if expected_occurrences is not None and expected_occurrences != match_count:
        return (
            f"replace_all expected {expected_occurrences} occurrence(s), but found {match_count}."
        )

    is_short = len(old_string.strip()) < SHORT_REPLACE_ALL_LENGTH
    needs_count = expected_occurrences is None and (
        match_count > REPLACE_ALL_MATCH_THRESHOLD or (is_short and match_count > 1)
    )
    if needs_count:
        return f"replace_all would affect {match_count} occurrence(s); provide expected_occurrences to confirm this broader replacement."
    return None


def _format_scope_metadata(scope: dict[str, Any], snippet: FileSnippet) -> dict[str, Any]:
    return {
        "file_path": normalize_file_path(snippet.file_path),
        "start_line": scope["start_line"],
        "end_line": scope["end_line"],
        "snippet_id": snippet.id,
    }


def _build_candidate_metadata(
    session_id: str,
    file_path: str,
    raw: str,
    matches: list[tuple[int, int]],
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    lines = raw.split("\n")
    candidates: list[dict[str, Any]] = []
    scope_start = scope["start_offset"]

    for s, e in matches[:MAX_CANDIDATE_COUNT]:
        abs_s = scope_start + s
        abs_e = scope_start + e
        start_line = raw.count("\n", 0, abs_s) + 1
        end_line = raw.count("\n", 0, abs_e) + 1
        preview_lines = lines[start_line - 1 : end_line]
        preview = "\n".join(
            f"{str(start_line + idx).rjust(6)}\t{line}" for idx, line in enumerate(preview_lines)
        )
        snippet = create_snippet(session_id, file_path, start_line, end_line, preview)
        candidates.append(
            {
                "snippet_id": snippet.id if snippet else None,
                "start_line": start_line,
                "end_line": end_line,
                "preview": preview,
            }
        )
    return candidates


# --- LLM Self-Correction & Diagnosis ---


def _call_completions(client: Any, **kwargs) -> Any:
    res = client.chat.completions.create(**kwargs)
    if inspect.iscoroutine(res):
        try:
            asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, res).result()
        except RuntimeError:
            return asyncio.run(res)
    return res


def to_bigrams(value: str) -> list[str]:
    """Extract 2-character overlapping shingles for similarity scoring."""
    if len(value) < 2:
        return [value] if value else []
    return [value[i : i + 2] for i in range(len(value) - 1)]


def similarity_score(left: str, right: str) -> float:
    """Calculate Sorensen-Dice bigram similarity coefficient (0.0 to 1.0)."""
    if not left or not right:
        return 1.0 if left == right else 0.0
    if left == right:
        return 1.0
    left_bigrams = to_bigrams(left)
    right_bigrams = to_bigrams(right)
    if not left_bigrams or not right_bigrams:
        return 1.0 if left == right else 0.0

    from collections import Counter

    right_counts = Counter(right_bigrams)
    overlap = 0
    for bg in left_bigrams:
        if right_counts[bg] > 0:
            overlap += 1
            right_counts[bg] -= 1

    return (2.0 * overlap) / (len(left_bigrams) + len(right_bigrams))


def build_loose_character_pattern(character: str) -> str:
    """Match quotes and typographic variants interchangeably, allowing optional escaping."""
    if character in ('"', "“", "”"):
        return r'\\*["“”]'
    if character in ("'", "‘", "’"):
        return r"\\*['‘’]"
    return re.escape(character)


def build_loose_escape_regex(source: str) -> re.Pattern | None:
    """Build a regex pattern that matches escaping and quote variations."""
    if not source:
        return None
    pattern = ""
    index = 0
    n = len(source)
    while index < n:
        if source[index] == "\\":
            slash_end = index
            while slash_end < n and source[slash_end] == "\\":
                slash_end += 1
            if slash_end < n:
                pattern += r"\\*"
                pattern += build_loose_character_pattern(source[slash_end])
                index = slash_end + 1
                continue
            pattern += re.escape(source[index:slash_end])
            index = slash_end
            continue
        pattern += build_loose_character_pattern(source[index])
        index += 1
    try:
        return re.compile(pattern)
    except re.error:
        return None


def find_loose_escape_matches(scope_text: str, needle: str) -> list[dict[str, Any]]:
    """Find loose character matches scored by bigram similarity."""
    regex = build_loose_escape_regex(needle)
    if not regex:
        return []
    normalized_needle = _normalize_loose_text(needle)
    matches: list[dict[str, Any]] = []
    for match in regex.finditer(scope_text):
        text = match.group(0)
        score = similarity_score(normalized_needle, _normalize_loose_text(text))
        matches.append(
            {
                "text": text,
                "score": score,
                "start_offset": match.start(),
                "end_offset": match.end(),
            }
        )
    return matches


def _find_loose_candidate(scope_text: str, old: str) -> str | None:
    loose_matches = find_loose_escape_matches(scope_text, old)
    if loose_matches and loose_matches[0]["score"] == 1.0:
        return loose_matches[0]["text"]
    norm_old = _normalize_loose_text(old)
    if not norm_old:
        return None
    lines = scope_text.splitlines(keepends=True)
    old_line_count = max(1, old.count("\n") + 1)
    for i in range(len(lines)):
        chunk = "".join(lines[i : i + old_line_count])
        if _normalize_loose_text(chunk) == norm_old:
            return chunk.rstrip("\r\n")
    return None


def _describe_correction_problems(old: str, matched: str) -> str:
    has_esc = _normalize_quotes(old) != _normalize_quotes(matched)
    has_quote = _normalize_escaping(old) != _normalize_escaping(matched)
    if has_esc and has_quote:
        return "the problems are escaping and quotation mark"
    if has_quote:
        return "the only problem is quotation mark"
    return "the only problem is escaping"


def _correct_escaped_strings_with_llm(
    snippet_text: str,
    old_string: str,
    new_string: str,
    matched_text: str,
    ctx: Any,
) -> dict[str, str] | None:
    client_factory = getattr(ctx, "create_openai_client", None) or (
        ctx.get("create_openai_client") if isinstance(ctx, dict) else None
    )
    if not client_factory:
        return None

    try:
        info = client_factory()
        client = info.get("client")
        if not client:
            return None
        model = info.get("model", "gpt-5.6-luna")
        problem = _describe_correction_problems(old_string, matched_text)

        response = _call_completions(
            client,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You correct file-edit strings when {problem}. "
                        "Return XML only using <response><corrected_old_string>...</corrected_old_string><corrected_new_string>...</corrected_new_string></response>. "
                        "Do not change semantics; only fix quoting or escaping so corrected_old_string matches the snippet exactly."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "<request>\n"
                        f"  <snippet_text><![CDATA[{snippet_text}]]></snippet_text>\n"
                        f"  <old_string><![CDATA[{old_string}]]></old_string>\n"
                        f"  <new_string><![CDATA[{new_string}]]></new_string>\n"
                        f"  <matched_text><![CDATA[{matched_text}]]></matched_text>\n"
                        "</request>\n"
                        "<output_format>\n"
                        "  <response>\n"
                        "    <corrected_old_string><![CDATA[...]]></corrected_old_string>\n"
                        "    <corrected_new_string><![CDATA[...]]></corrected_new_string>\n"
                        "  </response>\n"
                        "</output_format>"
                    ),
                },
            ],
            **build_thinking_request_options(
                bool(info.get("thinkingEnabled")),
                info.get("baseURL"),
                info.get("reasoningEffort"),
            ),
        )

        content = response.choices[0].message.content or ""
        parsed = _parse_corrected_edit_strings(content)
        if not parsed:
            return None
        if _normalize_loose_text(parsed["old_string"]) != _normalize_loose_text(old_string):
            return None
        if _normalize_loose_text(parsed["new_string"]) != _normalize_loose_text(new_string):
            return None
        if parsed["old_string"] != matched_text:
            return None
        if parsed["old_string"] == parsed["new_string"]:
            return None
        return parsed
    except Exception:
        return None


def _parse_corrected_edit_strings(content: str) -> dict[str, str] | None:
    trimmed = content.strip()
    if not trimmed:
        return None
    normalized = re.sub(r"```(?:xml)?\s*([\s\S]*?)```", r"\1", trimmed).strip()
    old_match = re.search(
        r"<corrected_old_string>(?:<!\[CDATA\[([\s\S]*?)\]\]>|([\s\S]*?))<\/corrected_old_string>",
        normalized,
        re.IGNORECASE,
    )
    new_match = re.search(
        r"<corrected_new_string>(?:<!\[CDATA\[([\s\S]*?)\]\]>|([\s\S]*?))<\/corrected_new_string>",
        normalized,
        re.IGNORECASE,
    )
    corr_old = (
        old_match.group(1)
        if old_match and old_match.group(1) is not None
        else (old_match.group(2) if old_match else None)
    )
    corr_new = (
        new_match.group(1)
        if new_match and new_match.group(1) is not None
        else (new_match.group(2) if new_match else None)
    )
    if corr_old is not None and corr_new is not None:
        return {"old_string": corr_old, "new_string": corr_new}
    return None


def _infer_old_string_not_found_reason_with_llm(
    raw: str,
    scope: dict[str, Any],
    old_string: str,
    new_string: str,
    ctx: Any,
) -> str | None:
    client_factory = getattr(ctx, "create_openai_client", None) or (
        ctx.get("create_openai_client") if isinstance(ctx, dict) else None
    )
    if not client_factory:
        return None

    try:
        info = client_factory()
        client = info.get("client")
        if not client:
            return None
        model = info.get("model", "gpt-5.6-luna")
        lines = raw.splitlines()
        before = "\n".join(
            lines[max(0, scope["start_line"] - 1 - 20) : max(0, scope["start_line"] - 1)]
        )
        after = "\n".join(
            lines[min(len(lines), scope["end_line"]) : min(len(lines), scope["end_line"] + 20)]
        )
        snippet_text = raw[scope["start_offset"] : scope["end_offset"]]

        response = _call_completions(
            client,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You diagnose failed file edits when old_string was not found. "
                        "Return XML only using <response><reason>...</reason></response>. "
                        "Be concise and specific. Explain the likely mismatch between old_string and the <snippet_text/> content. "
                        "Do not suggest unrelated changes."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "<request>\n"
                        f"  <content_before_snippet><![CDATA[{before}]]></content_before_snippet>\n"
                        f"  <snippet_text><![CDATA[{snippet_text}]]></snippet_text>\n"
                        f"  <content_after_snippet><![CDATA[{after}]]></content_after_snippet>\n"
                        f"  <old_string><![CDATA[{old_string}]]></old_string>\n"
                        f"  <new_string><![CDATA[{new_string}]]></new_string>\n"
                        "</request>\n"
                        "<output_format>\n"
                        "  <response>\n"
                        "    <reason><![CDATA[...]]></reason>\n"
                        "  </response>\n"
                        "</output_format>"
                    ),
                },
            ],
            **build_thinking_request_options(
                bool(info.get("thinkingEnabled")),
                info.get("baseURL"),
                info.get("reasoningEffort"),
            ),
        )

        content = response.choices[0].message.content or ""
        normalized = re.sub(r"```(?:xml)?\s*([\s\S]*?)```", r"\1", content).strip()
        reason_match = re.search(
            r"<reason>(?:<!\[CDATA\[([\s\S]*?)\]\]>|([\s\S]*?))<\/reason>",
            normalized,
            re.IGNORECASE,
        )
        reason = (
            reason_match.group(1)
            if reason_match and reason_match.group(1) is not None
            else (reason_match.group(2) if reason_match else None)
        )
        return reason.strip() if reason else None
    except Exception:
        return None
# --- from coderai/core/tools/str_replace_editor.py ---
"""str_replace_editor tool — Anthropic-style custom file editor (view, create, str_replace, insert, undo_edit)."""


import os
import pathlib
import time
from typing import Any

from coderai.utils.path import (
    build_diff_preview,
    ensure_parent_directory,
    read_text_file_with_metadata,
)
from coderai.utils.common.string_matcher import match_multistage
from coderai.state import (
    FileState,
    normalize_file_path,
    record_file_state,
)
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.tools.file.utils import check_file_write_access, write_file_with_callbacks

DEFAULT_MAX_OUTPUT_CHARS = 32_000
TRUNCATED_MESSAGE = "\n<response clipped>"

# History stack for undo_edit: path -> list of previous file contents
_UNDO_HISTORY: dict[str, list[str]] = {}


def _record_state(session_id: str, file_path: str, content: str) -> None:
    try:
        record_file_state(
            session_id,
            FileState(
                file_path=file_path,
                content=content,
                timestamp=int(time.time() * 1000),
            ),
            increment_version=True,
        )
    except Exception:
        pass


def _push_undo(path: str, content: str) -> None:
    norm = normalize_file_path(path)
    if norm not in _UNDO_HISTORY:
        _UNDO_HISTORY[norm] = []
    _UNDO_HISTORY[norm].append(content)
    # Keep last 20 revisions
    if len(_UNDO_HISTORY[norm]) > 20:
        _UNDO_HISTORY[norm].pop(0)


def _pop_undo(path: str) -> str | None:
    norm = normalize_file_path(path)
    if norm in _UNDO_HISTORY and _UNDO_HISTORY[norm]:
        return _UNDO_HISTORY[norm].pop()
    return None


def _resolve_path(path_str: str, project_root: str) -> str:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        p = pathlib.Path(project_root) / p
    return str(p.resolve())


def _format_lines(content: str, start_line: int = 1, end_line: int = -1) -> str:
    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return "1\t"

    if start_line < 1:
        start_line = 1
    if end_line == -1 or end_line > total_lines:
        end_line = total_lines

    formatted = []
    for idx in range(start_line - 1, end_line):
        if 0 <= idx < total_lines:
            line_num = idx + 1
            formatted.append(f"{line_num:6d}\t{lines[idx]}")
    return "\n".join(formatted)


def _find_line_numbers(content: str, search: str) -> list[int]:
    offsets: list[int] = []
    pos = 0
    while True:
        idx = content.find(search, pos)
        if idx == -1:
            break
        offsets.append(idx)
        pos = idx + len(search)
        if not search:
            break

    line_nums: list[int] = []
    for off in offsets:
        line_num = content[:off].count("\n") + 1
        line_nums.append(line_num)
    return line_nums


def handle_str_replace_editor_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Execute str_replace_editor command."""
    command = as_str(args.get("command", "")).strip()
    path_arg = as_str(args.get("path", "")).strip()

    if not command:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Missing required parameter `command`.",
        )

    if not path_arg:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Missing required parameter `path`.",
        )

    iso_val = (
        context.get("isolated_cwd")
        if isinstance(context, dict)
        else getattr(context, "isolated_cwd", None)
    )
    isolated_cwd = (
        str(iso_val)
        if isinstance(iso_val, (str, pathlib.Path)) and "MagicMock" not in str(type(iso_val))
        else None
    )

    pr_val = (
        context.get("project_root")
        if isinstance(context, dict)
        else getattr(context, "project_root", None)
    )
    project_root = (
        str(pr_val)
        if isinstance(pr_val, (str, pathlib.Path)) and "MagicMock" not in str(type(pr_val))
        else "."
    )
    effective_root = isolated_cwd or project_root
    target_path = _resolve_path(path_arg, effective_root)

    if command == "view":
        return _handle_view(target_path, args.get("view_range"), project_root, context)
    elif command == "create":
        return _handle_create(target_path, args.get("file_text"), context, args=args)
    elif command == "str_replace":
        return _handle_str_replace(
            target_path, args.get("old_str"), args.get("new_str"), context, args=args
        )
    elif command == "insert":
        return _handle_insert(
            target_path, args.get("insert_line"), args.get("new_str"), context, args=args
        )
    elif command in ("undo_edit", "undo_command"):
        return _handle_undo(target_path, context)
    else:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Unrecognized command `{command}`. Allowed commands are: `view`, `create`, `str_replace`, `insert`, `undo_edit` (or `undo_command`).",
        )


def _handle_view(
    target_path: str, view_range: Any, project_root: str, context: Any = None
) -> ToolResult:
    p = pathlib.Path(target_path)
    if not p.exists():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"The path `{target_path}` does not exist.",
        )

    if p.is_dir():
        # Directory view
        try:
            entries = sorted(os.listdir(target_path))
            listing = "\n".join(f"- {e}" for e in entries[:200])
            if len(entries) > 200:
                listing += f"\n... and {len(entries) - 200} more items"
            return ToolResult(
                ok=True,
                name="str_replace_editor",
                output=f"Directory listing for `{target_path}`:\n{listing}",
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                name="str_replace_editor",
                error=f"Failed to list directory `{target_path}`: {exc}",
            )

    # File view
    try:
        meta = read_text_file_with_metadata(target_path)
        content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    from coderai.tools.legacy.observation import get_observation_tracker

    get_observation_tracker().record_observation(session_id, target_path, content=content)

    start_line = 1
    end_line = -1
    if isinstance(view_range, list) and len(view_range) >= 2:
        try:
            start_line = int(view_range[0])
            end_line = int(view_range[1])
        except (ValueError, TypeError):
            pass

    formatted = _format_lines(content, start_line, end_line)
    if len(formatted) > DEFAULT_MAX_OUTPUT_CHARS:
        formatted = formatted[:DEFAULT_MAX_OUTPUT_CHARS] + TRUNCATED_MESSAGE

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"Here's the result of running `cat -n` on {target_path}:\n{formatted}",
    )


def _handle_create(
    target_path: str, file_text: Any, context: Any, args: dict[str, Any] | None = None
) -> ToolResult:
    if file_text is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `file_text` is required for command `create`.",
        )

    text = str(file_text)
    p = pathlib.Path(target_path)
    if p.exists():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File already exists at `{target_path}`. Cannot use `create` on existing files. Use `str_replace` or `insert`.",
        )

    sandbox_error = check_file_write_access(context, target_path)
    if sandbox_error:
        return ToolResult(ok=False, name="str_replace_editor", error=sandbox_error)

    from coderai.tools.file.utils import generate_virtual_patch, is_dry_run

    if is_dry_run(context, args):
        patch = generate_virtual_patch(target_path, None, text)
        return ToolResult(
            ok=True,
            name="str_replace_editor",
            output=f"[dry-run] Virtual create preview for {target_path}:\n{patch['diff']}"
            if patch["diff"]
            else f"[dry-run] File creation verified at: {target_path}",
            metadata={"dry_run": True, "virtual_patch": patch, "file_path": target_path},
        )

    ensure_parent_directory(target_path)
    try:
        write_file_with_callbacks(context, target_path, text)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    _record_state(session_id, target_path, text)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"File created successfully at: {target_path}",
    )


def _handle_str_replace(
    target_path: str,
    old_str: Any,
    new_str: Any,
    context: Any,
    args: dict[str, Any] | None = None,
) -> ToolResult:
    if old_str is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `old_str` is required for command `str_replace`.",
        )

    old_s = str(old_str)
    new_s = str(new_str) if new_str is not None else ""

    p = pathlib.Path(target_path)
    if not p.is_file():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File does not exist at `{target_path}`.",
        )

    try:
        meta = read_text_file_with_metadata(target_path)
        current_content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    sandbox_error = check_file_write_access(context, target_path)
    if sandbox_error:
        return ToolResult(ok=False, name="str_replace_editor", error=sandbox_error)

    from coderai.tools.file.utils import generate_virtual_patch, is_dry_run

    if not is_dry_run(context, args):
        from coderai.tools.legacy.observation import get_observation_tracker

        allowed, obs_err = get_observation_tracker().check_mutation_allowed(
            session_id, target_path, require_observed=True
        )
        if not allowed and obs_err:
            return ToolResult(
                ok=False,
                name="str_replace_editor",
                error=obs_err,
            )

    match_res = match_multistage(current_content, old_s, new_s)
    matches = match_res.matches

    if len(matches) == 0:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No replacement was performed, old_str did not appear in {target_path}.",
        )

    if len(matches) > 1:
        line_nums = [current_content[:s].count("\n") + 1 for s, _ in matches]
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No replacement was performed. Multiple occurrences of old_str in lines [{', '.join(map(str, line_nums))}]. Please include more surrounding context in `old_str` to make it unique.",
        )

    start_off, end_off = matches[0]
    new_content = current_content[:start_off] + match_res.replaced_new + current_content[end_off:]

    from coderai.tools.file.utils import is_dry_run

    if is_dry_run(context, args):
        patch = generate_virtual_patch(target_path, current_content, new_content)
        return ToolResult(
            ok=True,
            name="str_replace_editor",
            output=f"[dry-run] The file {target_path} edit preview:\n{patch['diff']}",
            metadata={"dry_run": True, "virtual_patch": patch, "file_path": target_path},
        )

    # Save for undo
    _push_undo(target_path, current_content)

    try:
        write_file_with_callbacks(context, target_path, new_content)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    _record_state(session_id, target_path, new_content)
    get_observation_tracker().record_observation(session_id, target_path, content=new_content)

    diff = build_diff_preview(target_path, current_content, new_content)
    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"The file {target_path} has been edited successfully.\n{diff}",
    )


def _handle_insert(
    target_path: str,
    insert_line: Any,
    new_str: Any,
    context: Any,
    args: dict[str, Any] | None = None,
) -> ToolResult:
    if insert_line is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `insert_line` is required for command `insert`.",
        )
    if new_str is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `new_str` is required for command `insert`.",
        )

    try:
        ins_line = int(insert_line)
    except (ValueError, TypeError):
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Invalid `insert_line` parameter: {insert_line}. Must be an integer.",
        )

    new_s = str(new_str)
    p = pathlib.Path(target_path)
    if not p.is_file():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File does not exist at `{target_path}`.",
        )

    try:
        meta = read_text_file_with_metadata(target_path)
        current_content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    lines = current_content.splitlines(keepends=True)
    total_lines = len(lines)

    if ins_line < 0 or ins_line > total_lines:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Invalid `insert_line` parameter: {ins_line}. It should be within the range of lines of the file: [0, {total_lines}].",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    sandbox_error = check_file_write_access(context, target_path)
    if sandbox_error:
        return ToolResult(ok=False, name="str_replace_editor", error=sandbox_error)

    from coderai.tools.file.utils import generate_virtual_patch, is_dry_run

    if not is_dry_run(context, args):
        from coderai.tools.legacy.observation import get_observation_tracker

        allowed, obs_err = get_observation_tracker().check_mutation_allowed(
            session_id, target_path, require_observed=True
        )
        if not allowed and obs_err:
            return ToolResult(
                ok=False,
                name="str_replace_editor",
                error=obs_err,
            )

    insert_content = new_s if new_s.endswith("\n") else new_s + "\n"
    if ins_line == 0:
        new_content = insert_content + "".join(lines)
    else:
        new_content = "".join(lines[:ins_line]) + insert_content + "".join(lines[ins_line:])

    from coderai.tools.file.utils import is_dry_run

    if is_dry_run(context, args):
        patch = generate_virtual_patch(target_path, current_content, new_content)
        return ToolResult(
            ok=True,
            name="str_replace_editor",
            output=f"[dry-run] The file {target_path} insertion preview:\n{patch['diff']}",
            metadata={"dry_run": True, "virtual_patch": patch, "file_path": target_path},
        )

    _push_undo(target_path, current_content)

    try:
        write_file_with_callbacks(context, target_path, new_content)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    _record_state(session_id, target_path, new_content)
    get_observation_tracker().record_observation(session_id, target_path, content=new_content)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"The file {target_path} has been edited successfully (inserted text after line {ins_line}).",
    )


def _handle_undo(target_path: str, context: Any) -> ToolResult:
    prev = _pop_undo(target_path)
    if prev is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No previous edit found in history for `{target_path}` to undo.",
        )

    try:
        write_file_with_callbacks(context, target_path, prev)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to restore file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    _record_state(session_id, target_path, prev)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"Successfully undid previous edit on {target_path}.",
    )


# --- Kimi CallableTool2 Parity ---

from collections.abc import Callable as _Callable
from pathlib import Path as _Path
from kaos.path import KaosPath as _KaosPath
from kosong.tooling import CallableTool2 as _CallableTool2, ToolError as _ToolError, ToolReturnValue as _ToolReturnValue
from pydantic import BaseModel as _BaseModel, Field as _Field

from coderai.soul.agent import Runtime as _Runtime
from coderai.soul.approval import Approval as _Approval
from coderai.tools.display import DisplayBlock as _DisplayBlock
from coderai.tools.file.plan_mode import inspect_plan_edit_target as _inspect_plan_edit_target
from coderai.tools.utils import load_desc as _load_desc
from coderai.utils.diff import build_diff_blocks as _build_diff_blocks
from coderai.utils.logging import logger as _logger
from coderai.utils.path import is_within_workspace as _is_within_workspace, kaos_path_from_user_input as _kaos_path_from_user_input

_BASE_REPLACE_DESCRIPTION = _load_desc(_Path(__file__).parent / "replace.md")


class ReplaceEdit(_BaseModel):
    old: str = _Field(description="The old string to replace. Can be multi-line.")
    new: str = _Field(description="The new string to replace with. Can be multi-line.")
    replace_all: bool = _Field(description="Whether to replace all occurrences.", default=False)


class ReplaceParams(_BaseModel):
    path: str = _Field(
        description=(
            "The path to the file to edit. Absolute paths are required when editing files "
            "outside the working directory."
        )
    )
    edit: ReplaceEdit | list[ReplaceEdit] = _Field(
        description=(
            "The edit(s) to apply to the file. "
            "You can provide a single edit or a list of edits here."
        )
    )


class StrReplaceFile(_CallableTool2[ReplaceParams]):
    name: str = "StrReplaceFile"
    description: str = _BASE_REPLACE_DESCRIPTION
    params: type[ReplaceParams] = ReplaceParams

    def __init__(self, runtime: _Runtime, approval: _Approval):
        super().__init__()
        builtin = getattr(runtime, "builtin_args", None)
        self._work_dir = getattr(builtin, "CODERAI_WORK_DIR", getattr(builtin, "KIMI_WORK_DIR", _KaosPath.cwd()))
        self._additional_dirs = getattr(runtime, "additional_dirs", [])
        self._approval = approval
        self._plan_mode_checker: _Callable[[], bool] | None = None
        self._plan_file_path_getter: _Callable[[], _Path | None] | None = None

    def bind_plan_mode(
        self, checker: _Callable[[], bool], path_getter: _Callable[[], _Path | None]
    ) -> None:
        self._plan_mode_checker = checker
        self._plan_file_path_getter = path_getter

    async def _validate_path(self, path: _KaosPath) -> _ToolError | None:
        resolved_path = path.canonical()
        if (
            not _is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            return _ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to edit a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    def _apply_edit(self, content: str, edit: ReplaceEdit) -> str:
        if edit.replace_all:
            return content.replace(edit.old, edit.new)
        else:
            return content.replace(edit.old, edit.new, 1)

    async def __call__(self, params: ReplaceParams) -> _ToolReturnValue:
        if not params.path:
            return _ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        try:
            p = _kaos_path_from_user_input(params.path)
            if err := await self._validate_path(p):
                return err
            p = p.canonical()

            plan_target = _inspect_plan_edit_target(
                p,
                plan_mode_checker=self._plan_mode_checker,
                plan_file_path_getter=self._plan_file_path_getter,
            )
            if isinstance(plan_target, _ToolError):
                return plan_target

            is_plan_file_edit = plan_target.is_plan_target

            if not await p.exists():
                if is_plan_file_edit:
                    return _ToolError(
                        message=(
                            "The current plan file does not exist yet. "
                            "Use WriteFile to create it before calling StrReplaceFile."
                        ),
                        brief="Plan file not created",
                    )
                return _ToolError(
                    message=f"`{params.path}` does not exist.",
                    brief="File not found",
                )
            if not await p.is_file():
                return _ToolError(
                    message=f"`{params.path}` is not a file.",
                    brief="Invalid path",
                )

            content = await p.read_text(errors="replace")
            original_content = content
            edits = [params.edit] if isinstance(params.edit, ReplaceEdit) else params.edit

            for edit in edits:
                content = self._apply_edit(content, edit)

            if content == original_content:
                return _ToolError(
                    message="No replacements were made. The old string was not found in the file.",
                    brief="No replacements made",
                )

            diff_blocks = await _build_diff_blocks(
                str(p), original_content, content
            )

            from coderai.tools.file import FileActions
            action = (
                FileActions.EDIT
                if _is_within_workspace(p, self._work_dir, self._additional_dirs)
                else FileActions.EDIT_OUTSIDE
            )

            if not is_plan_file_edit:
                result = await self._approval.request(
                    self.name,
                    action,
                    f"Edit file `{p}`",
                    display=diff_blocks,
                )
                if not result:
                    return result.rejection_error()

            await p.write_text(content, errors="replace")

            total_replacements = 0
            for edit in edits:
                if edit.replace_all:
                    total_replacements += original_content.count(edit.old)
                else:
                    total_replacements += 1 if edit.old in original_content else 0

            return _ToolReturnValue(
                is_error=False,
                output="",
                message=(
                    f"File successfully edited. "
                    f"Applied {len(edits)} edit(s) with {total_replacements} total replacement(s)."
                ),
                display=diff_blocks,
            )
        except Exception as e:
            _logger.warning("StrReplaceFile failed: {path}: {error}", path=params.path, error=e)
            return _ToolError(
                message=f"Failed to edit. Error: {e}",
                brief="Failed to edit file",
            )

