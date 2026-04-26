import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from coderAI.agent import Agent
from coderAI.agent_loop import ExecutionLoop

@pytest.fixture
def mock_hooks_file(tmp_path):
    hooks_dir = tmp_path / ".coderAI"
    hooks_dir.mkdir()
    hooks_file = hooks_dir / "hooks.json"
    
    # We'll use a shell script that appends to a log file so we can verify it ran
    log_file = tmp_path / "hooks.log"
    
    hooks_config = {
        "hooks": [
            {
                "type": "on_user_prompt",
                "tool": "*",
                "command": f"echo 'on_user_prompt' >> {log_file}"
            },
            {
                "type": "on_stop",
                "tool": "*",
                "command": f"echo 'on_stop' >> {log_file}"
            },
            {
                "type": "on_compact",
                "tool": "*",
                "command": f"echo 'on_compact' >> {log_file}"
            }
        ]
    }
    hooks_file.write_text(json.dumps(hooks_config))
    return log_file

@pytest.mark.asyncio
async def test_hooks_lifecycle(tmp_path, mock_hooks_file):
    # Setup agent in the tmp_path project
    agent = Agent()
    agent.config.project_root = str(tmp_path)
    agent.config.save_history = False # don't clutter
    
    # Mock LLM to just return a simple response immediately
    agent.provider.chat = AsyncMock(return_value={
        "choices": [{
            "message": {
                "content": "Hello there",
                "tool_calls": None
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
    
    # 1. Test on_user_prompt and on_stop
    loop = ExecutionLoop(agent)
    await loop.run("test prompt")
    
    log_content = mock_hooks_file.read_text()
    assert "on_user_prompt" in log_content
    assert "on_stop" in log_content
    
    # 2. Test on_compact
    # Setup session with many messages to force compaction
    agent.create_session()
    for i in range(10):
        agent.session.add_message("user", f"user message {i}")
        agent.session.add_message("assistant", f"assistant message {i}")
    
    # Force compaction by returning a very high token count
    agent.context_controller.estimate_tokens = MagicMock(return_value=1000000)
    
    # Mock a summarization response structure
    agent.provider.chat = AsyncMock(return_value={
        "choices": [{
            "message": {
                "content": "This is a summary",
            }
        }]
    })
    
    success = await agent.compact_context()
    assert success is True
    
    log_content = mock_hooks_file.read_text()
    assert "on_compact" in log_content
