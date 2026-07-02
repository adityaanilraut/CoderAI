"""End-to-end wiring tests for the read_image -> LLM vision pipeline.

These cover the fix for the previously dead ``_vision`` path: a ``read_image``
tool result must reach an Anthropic model as a real image content block, and
must be stripped (not crash) for providers that don't support it.
"""

from coderAI.core.tool_executor import _extract_vision_images
from coderAI.llm.base import LLMProvider
from coderAI.llm.anthropic import AnthropicProvider
from coderAI.system.history import Session


_B64 = "aGVsbG8="  # "hello" — stand-in for base64 image bytes


def _vision_result() -> dict:
    return {
        "success": True,
        "image_data": _B64,
        "mime_type": "image/png",
        "file_name": "diagram.png",
        "file_size": 1234,
        "_vision": True,
    }


class TestExtractVisionImages:
    def test_splits_image_out_of_result(self):
        clean, images = _extract_vision_images(_vision_result())
        # Heavy base64 is pulled out of the text-bound result...
        assert "image_data" not in clean
        assert clean["image_attached"] is True
        assert clean["mime_type"] == "image/png"
        # ...and surfaced as a structured image block.
        assert images == [{"mime_type": "image/png", "data": _B64}]

    def test_non_vision_result_untouched(self):
        res = {"success": True, "content": "plain text"}
        clean, images = _extract_vision_images(res)
        assert clean is res
        assert images is None

    def test_vision_flag_without_data_is_ignored(self):
        res = {"success": True, "_vision": True, "mime_type": "image/png"}
        clean, images = _extract_vision_images(res)
        assert images is None

    def test_non_dict_result_untouched(self):
        clean, images = _extract_vision_images("oops")
        assert clean == "oops"
        assert images is None


class TestToolImagesCarrier:
    def test_get_messages_for_api_surfaces_tool_images(self):
        session = Session(session_id="session_1_abcdef01")
        images = [{"mime_type": "image/png", "data": _B64}]
        session.add_message(
            "tool", "{}", tool_call_id="call_1", name="read_image", tool_images=images
        )
        api = session.get_messages_for_api()
        assert api[-1]["tool_images"] == images


class TestProviderCleaning:
    def test_anthropic_keeps_tool_images(self):
        provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
        msgs = [{"role": "tool", "content": "{}", "tool_call_id": "c1",
                 "tool_images": [{"mime_type": "image/png", "data": _B64}]}]
        assert provider.clean_messages(msgs)[0].get("tool_images")

    def test_base_strip_removes_tool_images(self):
        msgs = [{"role": "tool", "content": "{}", "tool_call_id": "c1",
                 "tool_images": [{"mime_type": "image/png", "data": _B64}]}]
        cleaned = LLMProvider._strip_tool_images(msgs)
        assert "tool_images" not in cleaned[0]
        # Original list is not mutated.
        assert "tool_images" in msgs[0]


class TestAnthropicVisionRendering:
    def test_tool_result_becomes_image_block(self):
        provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_image", "arguments": "{}"}}
                ],
            },
            {
                "role": "tool",
                "content": '{"image_attached": true}',
                "tool_call_id": "c1",
                "name": "read_image",
                "tool_images": [{"mime_type": "image/png", "data": _B64}],
            },
        ]
        _system, anthropic_messages = provider._convert_messages(messages)
        # Last message is the user turn carrying the tool_result.
        tool_result = anthropic_messages[-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        blocks = tool_result["content"]
        assert isinstance(blocks, list)
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": _B64,
        }

    def test_text_only_tool_result_stays_string(self):
        provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
        messages = [
            {"role": "tool", "content": "plain result", "tool_call_id": "c1"},
        ]
        _system, anthropic_messages = provider._convert_messages(messages)
        tool_result = anthropic_messages[-1]["content"][0]
        assert tool_result["content"] == "plain result"
