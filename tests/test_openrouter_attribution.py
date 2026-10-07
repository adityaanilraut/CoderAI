"""OpenRouter attribution reaches requests and survives client caching."""

from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

APP_URL = "https://github.com/adityaanilraut/CoderAI"


@pytest.fixture
def runtime(monkeypatch, isolated_home):
    import coderai.config as config
    import coderai.llm as llm

    provider = SimpleNamespace(
        type="openai_legacy",
        base_url="https://openrouter.ai/api/v1",
        api_key=SecretStr("test-key"),
        custom_headers=None,
        reasoning_key=None,
        oauth=None,
    )
    model = SimpleNamespace(
        provider="router",
        model="thinkingmachines/inkling-small:free",
        capabilities=[],
        display_name=None,
        max_context_size=128000,
    )
    monkeypatch.setattr(llm, "_client_pool", {})
    monkeypatch.setattr(
        llm,
        "_load_runtime_typed_config",
        lambda _: SimpleNamespace(
            providers={"router": provider},
            models={"inkling": model},
        ),
    )
    monkeypatch.setattr(
        config,
        "resolve_current_settings",
        lambda _: {
            "model": "inkling",
            "baseURL": provider.base_url,
            "apiKey": "test-key",
        },
    )
    return llm, provider, model


def test_attribution_on_wire_and_header_changes_invalidate_client(runtime):
    llm, provider, _ = runtime
    first = llm.create_openai_client()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": first["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    },
                ],
            },
        )

    client = first["client"].with_options(
        http_client=httpx.Client(
            transport=httpx.MockTransport(respond),
        )
    )
    client.chat.completions.create(
        model=first["model"],
        messages=[
            {"role": "user", "content": "hi"},
        ],
    )
    assert seen[0].headers["HTTP-Referer"] == APP_URL
    assert seen[0].headers["X-OpenRouter-Title"] == "CoderAI"
    assert seen[0].headers["X-OpenRouter-Categories"] == "cli-agent"
    assert llm.create_openai_client()["client"] is first["client"]

    provider.custom_headers = {"http-referer": "https://example.com/my-agent", "X-Test": "yes"}
    second = llm.create_openai_client()
    assert second["client"] is not first["client"]
    assert second["client"].default_headers["http-referer"] == "https://example.com/my-agent"
    assert second["client"].default_headers["X-Test"] == "yes"
    assert second["client"].default_headers["X-OpenRouter-Categories"] == "cli-agent"
    assert sum(k.lower() == "http-referer" for k in second["customHeaders"]) == 1


@pytest.mark.parametrize(
    "provider_type,module_name,class_name",
    [
        ("openai_legacy", "kosong.contrib.chat_provider.openai_legacy", "OpenAILegacy"),
        ("openai_responses", "kosong.contrib.chat_provider.openai_responses", "OpenAIResponses"),
        ("openrouter", "kosong.contrib.chat_provider.openai_legacy", "OpenAILegacy"),
    ],
)
def test_typed_provider_receives_attribution(
    runtime, monkeypatch, provider_type, module_name, class_name
):
    import importlib

    llm, provider, model = runtime
    provider.type = provider_type
    provider.custom_headers = {"x-title": "Custom agent", "X-Test": "yes"}
    seen = {}

    def construct(**kwargs):
        seen.update(kwargs)
        result = SimpleNamespace()
        result.with_thinking = lambda _: result
        return result

    monkeypatch.setattr(importlib.import_module(module_name), class_name, construct)
    llm.create_llm(provider, model)
    headers = seen["default_headers"]
    assert headers["HTTP-Referer"] == APP_URL
    assert headers["x-title"] == "Custom agent"
    assert "X-OpenRouter-Title" not in headers
    assert headers["X-Test"] == "yes"
    assert headers["X-OpenRouter-Categories"] == "cli-agent"


@pytest.mark.parametrize(
    "base_url", ["https://api.openai.com/v1", "https://openrouter.ai.example.com/v1"]
)
def test_non_openrouter_provider_keeps_only_its_headers(runtime, base_url):
    llm, provider, _ = runtime
    provider.base_url = base_url
    provider.custom_headers = {"X-Test": "yes"}
    info = llm.create_openai_client()
    assert info["customHeaders"] == {"X-Test": "yes"}


def test_harness_gate_error_explains_external_eligibility():
    from openai import PermissionDeniedError

    from coderai.utils.common.llm_error import describe_llm_error

    error = PermissionDeniedError(
        "thinkingmachines/inkling:free is only available on agentic harnesses.",
        response=httpx.Response(
            403, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        ),
        body=None,
    )
    message = describe_llm_error(error)
    assert "HTTP 403" in message
    assert "harness eligibility" in message
    assert "contact OpenRouter" in message
    assert "/model openrouter" in message and "/retry" in message


def test_unrelated_forbidden_error_does_not_claim_harness_rejection():
    from openai import PermissionDeniedError

    from coderai.utils.common.llm_error import describe_llm_error

    error = PermissionDeniedError(
        "Account disabled.",
        response=httpx.Response(
            403, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        ),
        body=None,
    )
    assert "harness eligibility" not in describe_llm_error(error)
