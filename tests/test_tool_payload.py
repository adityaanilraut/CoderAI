import json
from types import SimpleNamespace

from coderai.soul.compaction import ToolResultPruner
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.types import ToolResult
from coderai.utils.common.message_converter import OpenAIMessageConverter
from coderai.utils.common.usage import extract_usage_dict, accumulate_usage_dict


def test_grep_keeps_full_metadata_for_ui_but_does_not_duplicate_model_output(tmp_path):
    result = ToolResult(
        ok=True,
        name="grep",
        output="file.py\nLine 1: needle",
        metadata={"matches": [{"path": "file.py", "line": "needle"}], "count": 1},
    )
    data = json.loads(ToolExecutor(str(tmp_path)).format_tool_result(result))
    assert "matches" not in data["metadata"]
    assert data["metadata"]["count"] == 1
    assert result.metadata["matches"][0]["line"] == "needle"


def test_pruning_preserves_json_exit_status_and_escaped_output():
    raw = json.dumps(
        {
            "ok": False,
            "name": "bash",
            "output": ('quoted "line"\n' * 10000),
            "metadata": {"exitCode": 1, "startTime": 1, "endTime": 3},
        }
    )
    bounded = ToolResultPruner(1000).prune_content(raw)
    data = json.loads(bounded)
    assert len(bounded) <= 1000
    assert data["metadata"]["exitCode"] == 1 and data["ok"] is False
    assert "characters omitted" in data["output"] and data["truncated"]


def test_wire_conversion_preserves_structured_oversized_result():
    raw = json.dumps(
        {
            "ok": True,
            "name": "read",
            "output": "hello\n" * 10000,
            "metadata": {"snippet_id": "scope-123"},
        }
    )
    msg = SimpleNamespace(role="tool", content=raw, tool_call_id="call1")
    converted = OpenAIMessageConverter()._convert_message(msg, False, "deepseek-flash", 1500)
    data = json.loads(converted["content"])
    assert data["metadata"]["snippet_id"] == "scope-123"
    assert len(converted["content"]) <= 1500


def test_usage_retains_exact_reasoning_without_double_counting_completion():
    usage = extract_usage_dict(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
    )
    assert usage["reasoning_tokens"] == 30 and usage["completion_tokens"] == 50
    assert accumulate_usage_dict(usage, usage)["reasoning_tokens"] == 60
    assert "reasoning_tokens" not in extract_usage_dict({"completion_tokens": 5})


def test_nested_payload_and_tiny_limits_remain_parseable_and_bounded():
    from coderai.utils.common.tool_payload import prune_tool_payload

    raw = json.dumps(
        {
            "ok": False,
            "name": "bash",
            "extra": ["\\" * 4000] * 3,
            "metadata": {"exitCode": 1},
            "output": "huge" * 1000,
        }
    )
    for limit in (2, 16, 60, 100, 500):
        bounded = prune_tool_payload(raw, limit)
        data = json.loads(bounded)
        assert len(bounded) <= limit
        if limit >= 60:
            assert data["metadata"]["exitCode"] == 1
