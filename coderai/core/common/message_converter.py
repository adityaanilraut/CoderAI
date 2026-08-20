"""OpenAI message conversion — port of deepcode core/src/common/openai-message-converter.ts.

Handles message serialization for OpenAI chat completions:
- Tool call & response pairing: associates assistant tool_calls with subsequent tool messages.
- Interrupted tool call recovery: injects synthetic completed tool responses with
  `interrupted: true` if an assistant tool call has no paired tool execution, preventing
  OpenAI API schema rejections on subsequent turns.
- Thinking mode: formats and replays reasoning_content appropriately.
- Multimodal content support for vision-capable models.
"""

from __future__ import annotations

import json
from typing import Any

from coderai.core.common.model_capabilities import supports_multimodal
from coderai.core.session_log import derive_messages


class OpenAIMessageConverter:
    """Converts internal SessionMessage objects into OpenAI chat message dicts."""

    def __init__(self, render_init_prompt: Any = None) -> None:
        self.render_init_prompt = render_init_prompt

    def convert_session_messages(
        self,
        messages: list[Any],
        model: str | None = None,
        thinking_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        active_messages = derive_messages(messages)
        tool_pairings = self._pair_tool_messages(active_messages)

        target_model = model or "gpt-5.6-luna"
        openai_messages: list[dict[str, Any]] = []

        for index, message in enumerate(active_messages):
            role = getattr(message, "role", "")
            if role == "tool":
                # Paired tool messages are appended directly after their assistant message
                continue

            openai_messages.append(
                self._convert_message(
                    message, thinking_enabled=thinking_enabled, model=target_model
                )
            )

            if role != "assistant":
                continue

            tool_calls = self._get_assistant_tool_calls(message)
            if not tool_calls:
                continue

            for tool_call_index, tool_call in enumerate(tool_calls):
                tool_call_id = self._get_tool_call_id(tool_call)
                if not tool_call_id:
                    continue

                pairing_key = self._build_tool_pairing_key(index, tool_call_index)
                paired_tool_index = tool_pairings.get(pairing_key)

                if paired_tool_index is not None:
                    openai_messages.append(
                        self._convert_message(
                            active_messages[paired_tool_index],
                            thinking_enabled=thinking_enabled,
                            model=target_model,
                        )
                    )
                    continue

                # Trailing assistant with pending unexecuted tool calls at the end of history
                if index == len(active_messages) - 1:
                    continue

                # Orphaned / interrupted tool call: emit synthetic completed tool result
                openai_messages.append(
                    self._build_interrupted_openai_tool_message(tool_calls, tool_call_id)
                )

        return openai_messages

    def build_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        """Backwards-compatible wrapper around convert_session_messages."""
        return self.convert_session_messages(messages)

    def get_trailing_pending_tool_calls(self, messages: list[Any]) -> list[Any]:
        """Return tool_calls from the trailing assistant message if it is the latest active message."""
        info = self.get_trailing_pending_tool_call_message(messages)
        return info.get("toolCalls") or []

    def get_trailing_pending_tool_call_message(self, messages: list[Any]) -> dict[str, Any]:
        """Return trailing assistant message and its pending tool calls."""
        active = derive_messages(messages)
        if not active:
            return {"message": None, "toolCalls": []}
        latest = active[-1]
        if getattr(latest, "role", "") != "assistant":
            return {"message": None, "toolCalls": []}
        tool_calls = self._get_assistant_tool_calls(latest)
        valid_calls = [tc for tc in tool_calls if self._get_tool_call_id(tc)]
        if not valid_calls:
            return {"message": None, "toolCalls": []}
        return {"message": latest, "toolCalls": valid_calls}

    def _convert_message(
        self,
        message: Any,
        thinking_enabled: bool,
        model: str,
    ) -> dict[str, Any]:
        role = getattr(message, "role", "user")
        content = self._render_content(message)
        base: dict[str, Any] = {"role": role, "content": content}

        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            base["tool_calls"] = tool_calls

        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            base["tool_call_id"] = tool_call_id

        thinking = getattr(message, "thinking", None)
        if isinstance(thinking, str) and thinking:
            base["reasoning_content"] = thinking
        elif thinking_enabled and role == "assistant":
            base["reasoning_content"] = ""

        # Multimodal content support
        meta = getattr(message, "meta", None) or {}
        content_params = meta.get("contentParams")
        if (role in ("user", "system")) and content_params:
            content_parts: list[dict[str, Any]] = []
            if content:
                content_parts.append({"type": "text", "text": content})
            params = content_params if isinstance(content_params, list) else [content_params]
            for param in params:
                if isinstance(param, dict):
                    if param.get("type") != "image_url" or supports_multimodal(model):
                        content_parts.append(param)
            if content_parts:
                base["content"] = content_parts

        return base

    def _render_content(self, message: Any) -> str:
        content = getattr(message, "content", "") or ""
        role = getattr(message, "role", "")
        if role == "user" and content == "/init" and callable(self.render_init_prompt):
            return self.render_init_prompt() or ""
        return content

    def _pair_tool_messages(self, messages: list[Any]) -> dict[str, int]:
        pairings: dict[str, int] = {}
        used_tool_message_indexes: set[int] = set()

        for assistant_idx, message in enumerate(messages):
            if getattr(message, "role", "") != "assistant":
                continue
            tool_calls = self._get_assistant_tool_calls(message)
            for tool_call_idx, tool_call in enumerate(tool_calls):
                tool_call_id = self._get_tool_call_id(tool_call)
                if not tool_call_id:
                    continue

                tool_idx = self._find_pairable_tool_message_index(
                    messages, assistant_idx, tool_call_id, used_tool_message_indexes
                )
                if tool_idx is None:
                    continue

                used_tool_message_indexes.add(tool_idx)
                pairings[self._build_tool_pairing_key(assistant_idx, tool_call_idx)] = tool_idx

        return pairings

    def _find_pairable_tool_message_index(
        self,
        messages: list[Any],
        assistant_idx: int,
        tool_call_id: str,
        used_indexes: set[int],
    ) -> int | None:
        first_matching_idx: int | None = None
        for index in range(assistant_idx + 1, len(messages)):
            message = messages[index]
            if getattr(message, "role", "") != "tool" or index in used_indexes:
                continue
            cand_id = getattr(message, "tool_call_id", None)
            if cand_id != tool_call_id:
                continue
            if first_matching_idx is None:
                first_matching_idx = index
            if not self._is_interrupted_tool_message(message):
                return index
        return first_matching_idx

    def _get_assistant_tool_calls(self, message: Any) -> list[Any]:
        if getattr(message, "role", "") != "assistant":
            return []
        tool_calls = getattr(message, "tool_calls", None)
        return tool_calls if isinstance(tool_calls, list) else []

    def _get_tool_call_id(self, tool_call: Any) -> str | None:
        if isinstance(tool_call, dict):
            cid = tool_call.get("id")
            return str(cid) if cid else None
        cid = getattr(tool_call, "id", None)
        return str(cid) if cid else None

    def _build_tool_pairing_key(self, assistant_idx: int, tool_call_idx: int) -> str:
        return f"{assistant_idx}:{tool_call_idx}"

    def _is_interrupted_tool_message(self, message: Any) -> bool:
        content = getattr(message, "content", "")
        if not isinstance(content, str) or not content.strip():
            return False
        try:
            parsed = json.loads(content)
            return isinstance(parsed, dict) and bool(parsed.get("metadata", {}).get("interrupted"))
        except Exception:
            return False

    def _build_interrupted_openai_tool_message(
        self, tool_calls: list[Any], tool_call_id: str
    ) -> dict[str, Any]:
        tool_function = self.find_tool_function(tool_calls, tool_call_id)
        return {
            "role": "tool",
            "content": self._build_interrupted_tool_result(
                tool_function, "Previous tool call did not complete."
            ),
            "tool_call_id": tool_call_id,
        }

    def find_tool_function(self, tool_calls: list[Any], tool_call_id: str) -> Any | None:
        for tool_call in tool_calls:
            if isinstance(tool_call, dict):
                if tool_call.get("id") == tool_call_id:
                    return tool_call.get("function")
            elif getattr(tool_call, "id", None) == tool_call_id:
                return getattr(tool_call, "function", None)
        return None

    def _build_interrupted_tool_result(self, tool_function: Any | None, reason: str) -> str:
        tool_name = "tool"
        if isinstance(tool_function, dict) and isinstance(tool_function.get("name"), str):
            tool_name = tool_function["name"]
        elif hasattr(tool_function, "name") and isinstance(getattr(tool_function, "name"), str):
            tool_name = getattr(tool_function, "name")
        return json.dumps(
            {
                "ok": False,
                "name": tool_name,
                "error": reason,
                "metadata": {"interrupted": True},
            },
            indent=2,
        )
