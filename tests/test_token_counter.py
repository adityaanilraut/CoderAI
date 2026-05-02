import pytest
from unittest.mock import patch, MagicMock
from coderAI.llm._token_counter import count_tokens_anthropic, _cache


@pytest.fixture(autouse=True)
def clear_cache():
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture(autouse=True)
def no_event_loop():
    """Ensure count_tokens_anthropic sees no running event loop so it
    actually hits the (mocked) HTTP endpoint instead of falling back to
    the char/4 heuristic."""
    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        yield


def test_count_tokens_no_api_key():
    # Fallback to len // 4 if no api key
    text = "hello world"
    # len is 11, 11 // 4 = 2
    count = count_tokens_anthropic(text, "claude-3-5-sonnet-20241022", None)
    assert count == 2


@patch("requests.post")
def test_count_tokens_api_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"input_tokens": 42}
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    text = "this is a test text"
    model = "claude-3-5-sonnet-20241022"
    api_key = "test_key"

    count = count_tokens_anthropic(text, model, api_key)
    assert count == 42

    # Should be cached
    count2 = count_tokens_anthropic(text, model, api_key)
    assert count2 == 42

    # requests.post should only have been called once
    assert mock_post.call_count == 1


@patch("requests.post")
def test_count_tokens_api_failure(mock_post):
    mock_post.side_effect = Exception("API error")

    text = "fallback text"  # len 13, 13 // 4 = 3
    model = "claude-3-5-sonnet-20241022"
    api_key = "test_key"

    count = count_tokens_anthropic(text, model, api_key)
    assert count == 3
