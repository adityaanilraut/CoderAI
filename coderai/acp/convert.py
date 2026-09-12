"""ACP content codecs and NDJSON streaming parser.

- :class:`AcpNdjsonParser`: line-based NDJSON parser for ACP child output
  (pre-existing CoderAI codec, kept intact).
- ``acp_blocks_to_content_parts`` / ``display_block_to_acp_content`` /
  ``tool_result_to_acp_content``: ACP-SDK ↔ kosong content conversions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import acp
from kosong.message import ContentPart, ImageURLPart, TextPart
from kosong.tooling import DisplayBlock, ToolReturnValue

from coderai.acp.types import AcpMessage, ACPContentBlock, PROTOCOL_VERSION
from coderai.tools.display import DiffDisplayBlock

logger = logging.getLogger(__name__)


class HideOutputDisplayBlock(DisplayBlock):
    """A special DisplayBlock indicating output should be hidden in ACP clients."""

    type: str = "acp/hide_output"


class AcpNdjsonParser:
    """Streaming line-based NDJSON parser for ACP child process output."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, data: str | bytes) -> list[AcpMessage]:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        self._buffer += data

        messages: list[AcpMessage] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                if isinstance(raw, dict):
                    messages.append(
                        AcpMessage(
                            jsonrpc=raw.get("jsonrpc", "2.0"),
                            id=raw.get("id"),
                            method=raw.get("method"),
                            params=raw.get("params"),
                            result=raw.get("result"),
                            error=raw.get("error"),
                        )
                    )
            except Exception:
                continue

        return messages


def acp_blocks_to_content_parts(prompt: list[ACPContentBlock]) -> list[ContentPart]:
    content: list[ContentPart] = []
    for block in prompt:
        match block:
            case acp.schema.TextContentBlock():
                content.append(TextPart(text=block.text))
            case acp.schema.ImageContentBlock():
                content.append(
                    ImageURLPart(
                        image_url=ImageURLPart.ImageURL(
                            url=f"data:{block.mime_type};base64,{block.data}"
                        )
                    )
                )
            case acp.schema.EmbeddedResourceContentBlock():
                resource = block.resource
                if isinstance(resource, acp.schema.TextResourceContents):
                    uri = resource.uri
                    text = resource.text
                    content.append(TextPart(text=f"<resource uri={uri!r}>\n{text}\n</resource>"))
                else:
                    logger.warning(
                        "Unsupported embedded resource type: %s",
                        type(resource).__name__,
                    )
            case acp.schema.ResourceContentBlock():
                # ResourceContentBlock is a link reference without inline content;
                # include the URI so the model is at least aware of the reference.
                content.append(
                    TextPart(text=f"<resource_link uri={block.uri!r} name={block.name!r} />")
                )
            case _:
                logger.warning("Unsupported prompt content block: %s", block)
    return content


def display_block_to_acp_content(
    block: DisplayBlock,
) -> acp.schema.FileEditToolCallContent | None:
    if isinstance(block, DiffDisplayBlock):
        return acp.schema.FileEditToolCallContent(
            type="diff",
            path=block.path,
            old_text=block.old_text,
            new_text=block.new_text,
        )

    return None


def tool_result_to_acp_content(
    tool_ret: ToolReturnValue,
) -> list[
    acp.schema.ContentToolCallContent
    | acp.schema.FileEditToolCallContent
    | acp.schema.TerminalToolCallContent
]:
    def _to_acp_content(
        part: ContentPart,
    ) -> (
        acp.schema.ContentToolCallContent
        | acp.schema.FileEditToolCallContent
        | acp.schema.TerminalToolCallContent
    ):
        if isinstance(part, TextPart):
            return acp.schema.ContentToolCallContent(
                type="content", content=acp.schema.TextContentBlock(type="text", text=part.text)
            )
        logger.warning("Unsupported content part in tool result: %s", part)
        return acp.schema.ContentToolCallContent(
            type="content",
            content=acp.schema.TextContentBlock(type="text", text=f"[{part.__class__.__name__}]"),
        )

    def _to_text_block(text: str) -> acp.schema.ContentToolCallContent:
        return acp.schema.ContentToolCallContent(
            type="content", content=acp.schema.TextContentBlock(type="text", text=text)
        )

    contents: list[
        acp.schema.ContentToolCallContent
        | acp.schema.FileEditToolCallContent
        | acp.schema.TerminalToolCallContent
    ] = []

    for block in tool_ret.display:
        if isinstance(block, HideOutputDisplayBlock):
            # return early to indicate no output should be shown
            return []

        content = display_block_to_acp_content(block)
        if content is not None:
            contents.append(content)
    # TODO: better concatenation of `display` blocks and `output`?

    output = tool_ret.output
    if isinstance(output, str):
        if output:
            contents.append(_to_text_block(output))
    else:
        # NOTE: At the moment, ToolReturnValue.output is either a string or a
        # list of ContentPart. We avoid an unnecessary isinstance() check here
        # to keep pyright happy while still handling list outputs.
        contents.extend(_to_acp_content(part) for part in output)

    if not contents and tool_ret.message:
        # Fallback to the `message` for LLM if there's no other content
        contents.append(_to_text_block(tool_ret.message))

    return contents
