"""WebSearch tool — search the web via custom tools, DeepSeek responses API, or search endpoints (deepcode web-search-handler.ts)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import uuid
from typing import Any

from coderai.core.network.cache import get_search_cache
from coderai.core.network.client import get_http_client
from coderai.core.network.sanitizer import sanitize_prompt_injection, slice_payload
from coderai.core.tools.types import ToolResult, as_str

DEFAULT_WEB_SEARCH_API_URL = "https://deepcode.vegamo.cn/api/plugin/web-search"
DEEPSEEK_WEB_SEARCH_MODEL = "deepseek-v4-flash"
WEB_SEARCH_ACTIVITY_PREFIX = "WebSearch:"
MAX_CAPTURE_CHARS = 100_000
MAX_OUTPUT_CHARS = 30_000


def _format_activity_label(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    max_len = 180
    clipped = f"{normalized[: max_len - 3]}..." if len(normalized) > max_len else normalized
    return f"{WEB_SEARCH_ACTIVITY_PREFIX} {clipped}"


def _append_chunk(existing: str, chunk: str) -> str:
    if len(existing) >= MAX_CAPTURE_CHARS:
        return existing
    remaining = MAX_CAPTURE_CHARS - len(existing)
    return existing + chunk[:remaining]


def _truncate_output(text: str) -> tuple[str, bool]:
    return slice_payload(text, max_chars=MAX_OUTPUT_CHARS)


def _build_command_error(exit_code: int | None, signal: str | None) -> str:
    if signal:
        return f"WebSearch command terminated by signal {signal}."
    if exit_code is not None:
        return f"WebSearch command failed with exit code {exit_code}."
    return "WebSearch command failed."


def _contains_chinese_char(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _strip_code_fence(text: str) -> str:
    trimmed = text.strip()
    m = re.match(r"^```(?:[\w-]+)?\n([\s\S]*?)\n```$", trimmed)
    return m.group(1) if m else trimmed


def _parse_json_response(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            return json.loads(cleaned[first : last + 1])
        raise ValueError(f"Failed to parse JSON response: {cleaned}")


async def _chat(client: Any, model: str, prompt: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return str(content or "").strip()


async def _decide_search_language(client: Any, model: str, query: str) -> dict[str, str]:
    prompt = (
        "Decide whether the topic below has more useful online material in English or Chinese.\n\n"
        f"Topic:\n```text\n{query}\n```\n\n"
        'Return strict JSON:\n{"dominant_language":"en"|"zh","reason":"one short sentence"}\n'
        "Do not include markdown or any extra text."
    )
    try:
        parsed = _parse_json_response(await _chat(client, model, prompt))
        dominant = parsed.get("dominant_language")
        if dominant in ("en", "zh"):
            return {"dominantLanguage": dominant, "reason": str(parsed.get("reason", ""))}
    except Exception:
        pass
    return {"dominantLanguage": "en", "reason": ""}


async def _translate_query(client: Any, model: str, query: str, target_lang: str) -> str:
    prompt = (
        f"Translate the query text below into {target_lang}.\n\n"
        "Requirements:\n"
        "- Preserve product names, library names, API names, versions, and abbreviations.\n"
        "- Return only the translated query, without quotes or explanation.\n\n"
        f"Query:\n```text\n{query}\n```"
    )
    try:
        translated = _strip_code_fence(await _chat(client, model, prompt)).strip()
        return translated.strip("'\"")
    except Exception:
        return query


async def _prepare_search_query(client: Any, model: str, query: str) -> tuple[str, bool]:
    try:
        decision = await _decide_search_language(client, model, query)
        has_chinese = _contains_chinese_char(query)
        if decision["dominantLanguage"] == "en" and has_chinese:
            translated = await _translate_query(client, model, query, "English")
            if translated and translated != query:
                return translated, True
        elif decision["dominantLanguage"] == "zh" and not has_chinese:
            translated = await _translate_query(client, model, query, "Chinese")
            if translated and translated != query:
                return translated, True
    except Exception:
        pass
    return query, False


def _run_custom_web_search_tool(
    query: str,
    tool_command: str,
    cwd: str,
    configured_env: dict[str, str],
    context: Any,
) -> ToolResult:
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )

    env = {**os.environ, **configured_env}
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        if os.path.isfile(tool_command) and os.access(tool_command, os.X_OK):
            proc = subprocess.Popen([tool_command, query], **kwargs)
        else:
            # Fallback via shell
            proc = subprocess.Popen(f"{tool_command} {json.dumps(query)}", shell=True, **kwargs)
    except Exception as e:
        return ToolResult(ok=False, name="WebSearch", error=f"Failed to launch search tool: {e}")

    pid = proc.pid
    if on_process_start and pid:
        on_process_start(pid, _format_activity_label(query))

    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_raw, stderr_raw = proc.communicate()
    finally:
        if on_process_exit and pid:
            on_process_exit(pid)

    exit_code = proc.returncode
    signal_name = f"SIG{-exit_code}" if exit_code and exit_code < 0 else None
    if exit_code and exit_code < 0:
        exit_code = None

    if exit_code != 0 or signal_name:
        err = _build_command_error(exit_code, signal_name)
        combined_err = (stderr_raw or "").strip()
        if combined_err:
            err = f"{err}\n{combined_err}"
        return ToolResult(ok=False, name="WebSearch", error=err)

    output = (stdout_raw or "").strip()
    if not output:
        output = f"No online results found for '{query}'."
    sanitized = sanitize_prompt_injection(output)
    truncated, _ = _truncate_output(sanitized)
    return ToolResult(ok=True, name="WebSearch", output=truncated, metadata={"query": query})


async def handle_web_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    raw_query = as_str(args.get("query")).strip()
    if not raw_query:
        return ToolResult(
            ok=False,
            name="WebSearch",
            error='Missing required "query" string.',
        )

    project_root = getattr(context, "project_root", None) or (
        context.get("project_root", os.getcwd()) if isinstance(context, dict) else os.getcwd()
    )
    client_factory = getattr(context, "create_openai_client", None) or (
        context.get("create_openai_client") if isinstance(context, dict) else None
    )
    client_info = client_factory() if callable(client_factory) else {}
    client = client_info.get("client") if isinstance(client_info, dict) else None
    model = str(
        client_info.get("model")
        if isinstance(client_info, dict) and client_info.get("model")
        else (getattr(context, "model", None) or "gpt-4o")
    )
    configured_env = (client_info.get("env") or {}) if isinstance(client_info, dict) else {}

    # Check for custom search tool configured in environment or settings
    web_search_tool = (
        configured_env.get("CODERAI_WEB_SEARCH_TOOL")
        or configured_env.get("DEEPCODE_WEB_SEARCH_TOOL")
        or os.environ.get("CODERAI_WEB_SEARCH_TOOL")
        or os.environ.get("DEEPCODE_WEB_SEARCH_TOOL")
    )
    if web_search_tool:
        return _run_custom_web_search_tool(
            raw_query, web_search_tool, project_root, configured_env, context
        )

    # Check in-memory search cache
    cache = get_search_cache()
    cache_key = cache._generate_key("search", {"query": raw_query})
    cached_res = cache.get(cache_key)
    if cached_res is not None and isinstance(cached_res, ToolResult):
        return cached_res

    # Prepare/optimize query language if client available
    query = raw_query
    if client is not None:
        query, _ = await _prepare_search_query(client, model, raw_query)

    activity_id = f"web-search-{uuid.uuid4()}"
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )
    on_rate_limit = getattr(context, "on_plugin_rate_limit_exceeded", None) or (
        context.get("on_plugin_rate_limit_exceeded") if isinstance(context, dict) else None
    )

    if on_process_start:
        on_process_start(activity_id, _format_activity_label(query))

    try:
        # 1. DeepSeek Native Responses API Search (if supported by client)
        if client is not None and hasattr(client, "responses"):
            try:
                resp = client.responses.create(
                    model=DEEPSEEK_WEB_SEARCH_MODEL,
                    input=query,
                    tools=[{"type": "web_search"}],
                    tool_choice="required",
                )
                output_text = getattr(resp, "output_text", "")
                if output_text and str(output_text).strip():
                    sanitized = sanitize_prompt_injection(str(output_text).strip())
                    res = ToolResult(
                        ok=True,
                        name="WebSearch",
                        output=sanitized,
                        metadata={"query": query, "rawQuery": raw_query, "provider": "deepseek"},
                    )
                    cache.set(cache_key, res, ttl_seconds=600.0)
                    return res
            except Exception:
                pass

        # 2. Plugin Web Search Endpoint (if machine_id / api key available)
        machine_id = client_info.get("machineId") if isinstance(client_info, dict) else None
        plus_api_key = client_info.get("plusApiKey") if isinstance(client_info, dict) else None
        if machine_id or plus_api_key:
            try:
                http_client = get_http_client()
                headers = {"Content-Type": "application/json"}
                if machine_id:
                    headers["Token"] = machine_id
                if plus_api_key:
                    headers["PLUS-API-KEY"] = plus_api_key

                resp = await http_client.post_async(
                    DEFAULT_WEB_SEARCH_API_URL,
                    headers=headers,
                    json_data={"query": query},
                    timeout=(10.0, 30.0),
                )
                if resp.ok:
                    payload = json.loads(resp.text)
                    if payload.get("success") is True and payload.get("result"):
                        sanitized = sanitize_prompt_injection(str(payload["result"]).strip())
                        res = ToolResult(
                            ok=True,
                            name="WebSearch",
                            output=sanitized,
                            metadata={
                                "query": query,
                                "rawQuery": raw_query,
                                "provider": "plugin_api",
                            },
                        )
                        cache.set(cache_key, res, ttl_seconds=600.0)
                        return res
                    if "rate limit" in str(payload.get("reason", "")).lower() and on_rate_limit:
                        on_rate_limit("WebSearch")
            except Exception:
                pass

        # 3. Fallback direct search via DuckDuckGo / Instant Answer API
        results: list[str] = []
        encoded = urllib.parse.urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        url = f"https://api.duckduckgo.com/?{encoded}"

        try:
            http_client = get_http_client()
            resp = await http_client.get_async(url, timeout=(8.0, 15.0), use_cache=True)
            if resp.ok:
                data = json.loads(resp.text)
                abstract = data.get("AbstractText")
                if abstract:
                    results.append(abstract)
                for item in data.get("RelatedTopics", [])[:6]:
                    if isinstance(item, dict) and item.get("Text"):
                        results.append(f"- {item['Text']}")
            elif resp.status_code == 429 and on_rate_limit:
                on_rate_limit("WebSearch")
        except Exception:
            pass

        if not results:
            return ToolResult(
                ok=True,
                name="WebSearch",
                output=f"No online results found for '{query}'.",
                metadata={"query": query, "rawQuery": raw_query},
            )

        sanitized_res = sanitize_prompt_injection("\n\n".join(results))
        res = ToolResult(
            ok=True,
            name="WebSearch",
            output=sanitized_res,
            metadata={"query": query, "rawQuery": raw_query, "provider": "duckduckgo"},
        )
        cache.set(cache_key, res, ttl_seconds=600.0)
        return res

    finally:
        if on_process_exit:
            on_process_exit(activity_id)


# Backward compatibility
handle = handle_web_search_tool
