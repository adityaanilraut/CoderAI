"""edit tool — snippet-scoped replacement with LLM correction fallback (deepcode edit-handler.ts)."""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re
from typing import Any

from coderai.core.common.file_utils import (
    build_diff_preview,
    has_file_changed_since_state,
    read_text_file_with_metadata,
    write_text_file,
)
from coderai.core.common.openai_thinking import build_thinking_request_options
from coderai.core.common.string_matcher import (
    find_occurrences as _find_occurrences,
    match_multistage,
    normalize_escaping as _normalize_escaping,
    normalize_loose_text as _normalize_loose_text,
    normalize_quotes as _normalize_quotes,
)
from coderai.core.common.validate import execute_validated_tool, semantic_boolean, semantic_integer
from coderai.core.state import (
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
from coderai.core.tools.types import ToolResult, as_str

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
                    (ctx.get("project_root") if isinstance(ctx, dict) else getattr(ctx, "project_root", None))
                    or "."
                )
                file_path = normalize_file_path(str(pathlib.Path(project_root) / file_path))
            if snippet.file_path != file_path and not file_path.endswith(snippet.file_path):
                return ToolResult(
                    ok=False, name="edit", error="snippet_id does not belong to the provided file_path."
                )
        else:
            if not file_path_arg:
                return ToolResult(ok=False, name="edit", error="file_path is required when snippet_id is omitted.")
            project_root = (
                (ctx.get("project_root") if isinstance(ctx, dict) else getattr(ctx, "project_root", None))
                or "."
            )
            file_path = file_path_arg if is_absolute_file_path(file_path_arg) else str(pathlib.Path(project_root) / file_path_arg)
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
                from coderai.core.state import mark_file_read
                from coderai.core.tools.observation import get_observation_tracker

                get_observation_tracker().record_observation(
                    session_id, file_path, content_res["content"]
                )
                mark_file_read(session_id, file_path, content_res)
                file_state = get_file_state(session_id, file_path)
            except Exception as e:
                return ToolResult(ok=False, name="edit", error=f"Error reading file before editing: {e}")

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

        sandbox_mode = ctx.get("sandbox_mode") if isinstance(ctx, dict) else getattr(ctx, "sandbox_mode", None)
        project_root = (
            (ctx.get("project_root") if isinstance(ctx, dict) else getattr(ctx, "project_root", None))
            or "."
        )
        from coderai.core.sandbox import check_sandbox_path_access

        sb_allowed, sb_err = check_sandbox_path_access(
            file_path,
            op="write",
            mode=sandbox_mode,
            workspace_root=project_root,
        )
        if not sb_allowed and sb_err:
            return ToolResult(ok=False, name="edit", error=sb_err)

        file_state = get_file_state(session_id, file_path)
        if not file_state:
            return ToolResult(ok=False, name="edit", error="Must read file before editing.")

        if has_file_changed_since_state(file_path, file_state):
            return ToolResult(
                ok=False,
                name="edit",
                error="File has been modified since read. Read it again before editing.",
            )

        from coderai.core.tools.observation import get_observation_tracker

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

        if isinstance(ctx, dict):
            on_before_mutation = ctx.get("on_before_file_mutation")
            on_after_mutation = ctx.get("on_after_file_mutation")
        else:
            on_before_mutation = getattr(ctx, "on_before_file_mutation", None)
            on_after_mutation = getattr(ctx, "on_after_file_mutation", None)

        updated_content = _apply_replacement(
            raw, scope, matches, replacement_old, replacement_new, replace_all
        )
        diff_preview = build_diff_preview(file_path, raw, updated_content)

        try:
            if on_before_mutation:
                on_before_mutation(file_path)

            write_text_file(
                file_path, updated_content, metadata["encoding"], metadata["lineEndings"]
            )

            if on_after_mutation:
                on_after_mutation(file_path)

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
