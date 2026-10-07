"""Dynamic OpenRouter catalog: picker reflects whatever the provider returns."""

from __future__ import annotations

import json

import pytest


def _fake_response(payload: dict) -> object:
    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    return _Resp()


def _payload(ids: list[str], paid: list[str] | None = None) -> dict:
    data = [
        {
            "id": mid,
            "name": f"Model {mid}",
            "context_length": 128000,
            "pricing": {"prompt": "0", "completion": "0"},
        }
        for mid in ids
    ]
    for mid in paid or []:
        data.append(
            {
                "id": mid,
                "name": f"Model {mid}",
                "context_length": 128000,
                "pricing": {"prompt": "0.003", "completion": "0.015"},
            }
        )
    return {"data": data}


def test_picker_lists_whatever_provider_returns(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A brand-new free model appearing on the provider shows up in the picker."""
    import coderai.openrouter as oro

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        oro.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(
            _payload(["some-new/model-xyz:free", "other:free"], paid=["paid/model"])
        ),
    )
    from coderai.ui.shell.session_picker import get_available_models

    names = [n for n, _, _ in get_available_models("x", force_refresh_openrouter=True)]
    assert "openrouter/some-new/model-xyz:free" in names
    assert "openrouter/other:free" in names
    assert not any(n == "openrouter/paid/model" for n in names)
    assert oro._last_source == "live"


def test_second_picker_hit_uses_cache_without_pinging(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Proves the cache path: provider returning changed data is NOT reflected."""
    import coderai.openrouter as oro

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    calls: list[int] = []

    def _urlopen(*a: object, **k: object) -> object:
        calls.append(1)
        return _fake_response(_payload(["first/model:free"]))

    monkeypatch.setattr(oro.urllib.request, "urlopen", _urlopen)
    from coderai.ui.shell.session_picker import get_available_models

    first = [n for n, _, _ in get_available_models("x", force_refresh_openrouter=True)]
    assert "openrouter/first/model:free" in first
    second = [n for n, _, _ in get_available_models("x")]
    assert "openrouter/first/model:free" in second
    assert len(calls) == 1  # second call served from cache, provider not pinged
    assert oro._last_source == "cache"


def test_provider_outage_falls_back_without_breaking_picker(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Offline first run still yields fallback models and a fallback status."""
    import coderai.openrouter as oro

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))

    def _boom(*a: object, **k: object) -> object:
        raise OSError("no network")

    monkeypatch.setattr(oro.urllib.request, "urlopen", _boom)
    from coderai.ui.shell.session_picker import get_available_models

    names = [n for n, _, _ in get_available_models("x", force_refresh_openrouter=True)]
    assert any(n.endswith(":free") for n in names)
    assert oro._last_source == "fallback"
    assert "fallback" in oro.catalog_status(len(names))


def test_refresh_command_reping_and_reports(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """refresh_openrouter_catalog force-pings and reports a live status line."""
    import coderai.openrouter as oro

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        oro.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(_payload(["fresh/model:free"])),
    )
    count, status = oro.refresh_openrouter_catalog()
    assert count == 1
    assert "live ping" in status
    assert "fresh/model:free" in status or count == 1


def test_ping_sends_non_default_user_agent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Provider frontends challenge bare Python-urllib clients; identify honestly."""
    import coderai.openrouter as oro

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    seen: dict[str, object] = {}

    def _urlopen(req: object, *a: object, **k: object) -> object:
        seen["headers"] = req.headers if hasattr(req, "headers") else {}
        return _fake_response(_payload(["ua/model:free"]))

    monkeypatch.setattr(oro.urllib.request, "urlopen", _urlopen)
    oro.fetch_openrouter_models(force_refresh=True)
    headers = seen["headers"]
    assert isinstance(headers, dict)
    ua = headers.get("User-agent", headers.get("User-Agent", ""))
    assert "Python-urllib" not in str(ua)
    assert "CoderAI" in str(ua)


def test_failed_ping_reports_reason_not_just_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A failed ping must say WHY (e.g. HTTP 403), not just 'offline fallback'."""
    import coderai.openrouter as oro
    from urllib.error import HTTPError

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))

    def _boom(*a: object, **k: object) -> object:
        raise HTTPError("https://openrouter.ai/api/v1/models", 403, "Forbidden", {}, None)

    monkeypatch.setattr(oro.urllib.request, "urlopen", _boom)
    models = oro.get_openrouter_free_models(force_refresh=True)
    status = oro.catalog_status(len(models))
    assert oro._last_source == "fallback"
    assert "403" in status
    assert "fallback" in status


def test_raw_author_slug_routes_to_openrouter_with_full_wire_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stealth/space-bunny-alpha (no prefix, no :free) resolves via OpenRouter intact."""
    import coderai.openrouter as oro
    from coderai.llm import resolve_model_provider_routing

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODERAI_API_KEY", raising=False)

    url, key = resolve_model_provider_routing("stealth/space-bunny-alpha")
    assert url == "https://openrouter.ai/api/v1"
    assert key == "sk-or-test"
    assert oro.is_openrouter_model("stealth/space-bunny-alpha")
    assert not oro.is_openrouter_model("gpt-6-luna")
    assert not oro.is_openrouter_model("kimi-code/kimi-for-coding")


def test_native_families_still_win_over_openrouter_catchall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw-slug catch-all must not hijack DeepSeek/Kimi/Gemini/Claude ids."""
    from coderai.llm import resolve_model_provider_routing

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    url, _ = resolve_model_provider_routing("deepseek-chat")
    assert "deepseek" in url
    url, _ = resolve_model_provider_routing("kimi-code/kimi-for-coding")
    assert "kimi" in url


def test_openrouter_reasoning_param_shape() -> None:
    """OpenRouter gets its native reasoning object, not top-level reasoning_effort."""
    from coderai.utils.common.openai_thinking import build_thinking_request_options as b

    or_url = "https://openrouter.ai/api/v1"
    assert b(True, base_url=or_url, reasoning_effort="max", model="stealth/x") == {
        "extra_body": {"reasoning": {"enabled": True}}
    }
    assert b(True, base_url=or_url, reasoning_effort="low", model="stealth/x") == {
        "extra_body": {"reasoning": {"enabled": True, "effort": "low"}}
    }
    assert b(False, base_url=or_url, reasoning_effort="max", model="stealth/x") == {
        "extra_body": {"reasoning": {"enabled": False}}
    }
    # Non-OpenRouter paths untouched.
    assert b(True, model="deepseek-v4-pro", reasoning_effort="max") == {
        "extra_body": {"reasoning_effort": "max"}
    }


@pytest.mark.parametrize("thinking,effort", [(False, "max"), (True, "off"), (True, "high")])
def test_mandatory_reasoning_uses_catalog_metadata(monkeypatch, tmp_path, thinking, effort):
    import coderai.openrouter as oro
    from coderai.utils.common.openai_thinking import build_thinking_request_options

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    entry = _payload(["liquid/lfm-2.5-2.6b:free"])["data"][0]
    entry["reasoning"] = {"mandatory": True}
    oro._write_cache([entry])
    monkeypatch.setattr(oro.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))

    options = build_thinking_request_options(
        thinking, model="openrouter/liquid/lfm-2.5-2.6b:free", reasoning_effort=effort
    )
    expected = {"enabled": True}
    if not thinking or effort == "off":
        expected["exclude"] = True
    assert options == {"extra_body": {"reasoning": expected}}


def test_retired_active_model_is_not_reinserted_after_refresh(monkeypatch, tmp_path):
    import coderai.openrouter as oro
    from coderai.ui.shell.session_picker import get_available_models

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        oro.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(_payload(["fresh/model:free"])),
    )
    retired = "openrouter/stealth/space-bunny-alpha"
    names = [n for n, _, _ in get_available_models(retired, force_refresh_openrouter=True)]
    assert retired not in names
    assert "openrouter/fresh/model:free" in names


def test_refresh_warns_and_opens_available_openrouter_models(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace as NS

    import coderai.openrouter as oro
    import coderai.ui.shell.session_picker as picker
    from coderai.ui.shell.dispatch import ShellContext, cmd_model

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        oro.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(_payload(["fresh/model:free"])),
    )
    selected = []

    def choose(console, items, **kwargs):
        if kwargs["title"].startswith("OpenRouter model providers"):
            assert items[0][0] == "fresh"
        else:
            assert kwargs["title"].startswith("OpenRouter / Fresh models")
            assert items[0][0] == "openrouter/fresh/model:free"
        return 0

    monkeypatch.setattr(picker, "select_with_arrows", choose)
    manager = NS(
        get_active_model=lambda: "openrouter/stealth/space-bunny-alpha", set_model=selected.append
    )
    cmd_model(ShellContext(mgr=manager), "refresh")
    assert selected == ["openrouter/fresh/model:free"]
    assert "absent from the latest OpenRouter catalog" in capsys.readouterr().out


def test_missing_endpoint_error_has_recovery_instructions():
    import httpx
    from openai import NotFoundError

    from coderai.utils.common.llm_error import describe_llm_error

    error = NotFoundError(
        "No endpoints found for stealth/space-bunny-alpha.",
        response=httpx.Response(
            404, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        ),
        body=None,
    )
    message = describe_llm_error(error)
    assert "No endpoints found" in message
    assert "/model openrouter" in message and "/retry" in message
    assert "alone does not switch" in message


def test_offline_picker_keeps_active_model_when_availability_is_unknown(monkeypatch, tmp_path):
    import os
    import time

    import coderai.openrouter as oro
    from coderai.ui.shell.session_picker import get_available_models

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    oro._write_cache(_payload(["fresh/model:free"])["data"])
    expired = time.time() - oro.CACHE_TTL_SECONDS - 60
    os.utime(oro._cache_path(), (expired, expired))
    current = "openrouter/stealth/space-bunny-alpha"
    names = [n for n, _, _ in get_available_models(current, refresh_openrouter=False)]
    assert current in names
    assert oro.openrouter_model_available(current) is None


def test_configured_aliases_use_wire_id_for_catalog_availability(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    import coderai.openrouter as oro
    from coderai.ui.shell.session_picker import get_available_models

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    oro._write_cache(_payload(["paid/available"], paid=["other/paid"])["data"])
    configured = NS(
        providers={"router": NS(type="openrouter")},
        models={
            key: NS(model=wire, provider="router", display_name=key)
            for key, wire in [
                ("retired-alias", "stealth/space-bunny-alpha"),
                ("openrouter/custom-alias", "other/paid"),
            ]
        },
    )
    monkeypatch.setattr("coderai.config.load_typed_config", lambda: configured)
    names = [n for n, _, _ in get_available_models("retired-alias", refresh_openrouter=False)]
    assert "retired-alias" not in names
    assert "openrouter/custom-alias" in names


@pytest.mark.parametrize("effort,wire_effort", [("max", "max"), ("xhigh", "high"), ("low", "low")])
def test_openrouter_uses_advertised_reasoning_efforts(monkeypatch, tmp_path, effort, wire_effort):
    import coderai.openrouter as oro
    from coderai.utils.common.openai_thinking import build_thinking_request_options

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    entry = _payload(["vendor/reasoner:free"])["data"][0]
    entry["reasoning"] = {"mandatory": True, "supported_efforts": ["max", "high", "low"]}
    oro._write_cache([entry])
    assert build_thinking_request_options(True, model=entry["id"], reasoning_effort=effort) == {
        "extra_body": {"reasoning": {"enabled": True, "effort": wire_effort}}
    }


def test_reasoning_details_replayed_for_openrouter_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored reasoning blocks ride along on OpenRouter turns, nowhere else."""
    from coderai.soul.session.models import SessionMessage
    from coderai.utils.common.message_converter import OpenAIMessageConverter

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assistant = SessionMessage(
        id="a1",
        session_id="s",
        role="assistant",
        content="done",
        meta={"reasoningDetails": [{"type": "reasoning.text", "text": "hmm"}]},
    )
    conv = OpenAIMessageConverter()
    wire_or = conv.convert_session_messages([assistant], model="stealth/space-bunny-alpha")
    assert wire_or[0]["reasoning_details"] == [{"type": "reasoning.text", "text": "hmm"}]
    wire_oa = conv.convert_session_messages([assistant], model="gpt-6-luna")
    assert "reasoning_details" not in wire_oa


def test_stream_assembler_collects_reasoning_details() -> None:
    """Streaming deltas carrying reasoning_details blocks end up in the message."""
    from coderai.soul.session.streaming import assemble_stream_response

    class _Delta:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Choice:
        def __init__(self, delta):
            self.delta = delta

    class _Chunk:
        def __init__(self, delta=None, usage=None):
            self.choices = [_Choice(delta)] if delta is not None else []
            self.usage = usage

    chunks = [
        _Chunk(_Delta(reasoning_details=[{"type": "reasoning.text", "text": "step1"}])),
        _Chunk(_Delta(content="hi")),
    ]
    res = assemble_stream_response(iter(chunks), "reasoning_content")
    msg = res["choices"][0]["message"]
    assert msg["reasoning_details"] == [{"type": "reasoning.text", "text": "step1"}]
    assert msg["content"] == "hi"
