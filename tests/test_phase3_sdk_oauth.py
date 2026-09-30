"""Production SDK types and runtime OAuth boundaries, with offline providers."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from coderai.auth.oauth import (
    KIMI_CODE_OAUTH_KEY,
    OAuthManager,
    OAuthToken,
    OAuthUnauthorized,
    load_token,
    save_token,
)
from coderai.acp.engine import SessionManagerEngine
from coderai.soul.session.manager import SessionManager
from coderai.wire.types import (
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
    ToolReturnValue,
    deserialize_wire_message,
    serialize_wire_message,
)


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    import coderai.auth.oauth as module

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path / "credentials"))
    module._REJECTED_REFRESH_TOKENS.clear()
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "sdks/coderai-sdk/src"))
    yield
    module._REJECTED_REFRESH_TOKENS.clear()


def token(access="old", refresh="refresh", expired=True):
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=time.time() + (-3600 if expired else 3600),
    )


class Engine:
    session_id = "sdk"

    def __init__(self, events):
        self.events = events

    async def run(self, parts, cancel):
        for event in self.events:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["concrete", "envelope", "deserialized"])
@pytest.mark.parametrize("error", [False, True])
async def test_sdk_production_call_and_result(representation, error):
    from coderai_sdk import CoderAIClient
    from kosong.message import TextPart

    events = [
        ToolCall(
            id="call", function=ToolCall.FunctionBody(name="read", arguments='{"file_path":"a"}')
        ),
        ToolResult(
            tool_call_id="call",
            return_value=ToolReturnValue(
                is_error=error,
                output=[TextPart(text="first"), TextPart(text="second")],
                message="failed" if error else "",
                display=[],
            ),
        ),
    ]
    if representation != "concrete":
        events = [serialize_wire_message(e) for e in events]
    if representation == "deserialized":
        events = [deserialize_wire_message(e) for e in events]
        assert isinstance(events[0], ToolCall) and isinstance(events[1], ToolResult)
    result = await CoderAIClient(engine=Engine(events)).prompt("read")
    assert result.tool_calls == [{"id": "call", "name": "read", "arguments": '{"file_path":"a"}'}]
    output = result.tool_results[0]
    assert output["tool_call_id"] == "call" and output["is_error"] is error
    assert output["error"] == ("failed" if error else "")
    assert result.messages[0].content == "firstsecond"
    assert result.messages[0].tool_call_id == "call"
    assert result.messages[0].error == output["error"]


@pytest.mark.asyncio
async def test_sdk_partial_argument_chunks_and_flat_result():
    from coderai_sdk import CoderAIClient
    from kosong.message import ToolCallPart as ArgumentPart

    result = await CoderAIClient(
        engine=Engine(
            [
                ToolCallPart(id="call", name="read", arguments="{"),
                ArgumentPart(arguments_part='"file_path":"a"}'),
                ToolResultPart(tool_call_id="call", output="partial", error="denied"),
            ]
        )
    ).prompt("read")
    assert result.tool_calls == [{"id": "call", "name": "read", "arguments": '{"file_path":"a"}'}]
    assert result.tool_results == [
        {"tool_call_id": "call", "output": "partial", "error": "denied", "is_error": True}
    ]


def reply(text="", calls=None):
    return {"choices": [{"message": {"content": text, "tool_calls": calls}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_sdk_real_manager_matches_persisted_tool_history(tmp_path, missing):
    from coderai_sdk import CoderAIClient

    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("a.txt").write_text("persisted tool output")
    args = '{"file_path":"' + ("missing.txt" if missing else "a.txt") + '"}'
    calls = [
        {"id": "real-call", "type": "function", "function": {"name": "read", "arguments": args}}
    ]
    replies = iter([reply("checking", calls), reply("complete")])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: next(replies)))
    )
    manager = SessionManager(
        project_root=str(project),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "mergeAllAvailableSkills": False,
            "permissions": {"defaultMode": "allowAll", "allow": ["read-in-cwd"]},
        },
    )
    try:
        result = await CoderAIClient(engine=SessionManagerEngine(manager)).prompt("read")
        history = manager.list_session_messages(result.session_id)
        assert result.text == "".join(m.content for m in history if m.role == "assistant")
        saved_tools = [m for m in history if m.role == "tool"]
        assert len(saved_tools) == 1
        persisted = json.loads(saved_tools[0].content)
        assert result.messages[1].content == (persisted.get("output") or persisted.get("error", ""))
        assert result.tool_results[0]["error"] == persisted.get("error", "")
        assert result.tool_results[0]["tool_call_id"] == "real-call"
        assert result.tool_results[0]["is_error"] is missing
        assert result.tool_calls == [{"id": "real-call", "name": "read", "arguments": args}]
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_public_refresh_rotates_expired_token(monkeypatch):
    from coderai.llm import ensure_oauth_fresh

    save_token(KIMI_CODE_OAUTH_KEY, token())
    calls = []

    def refresh(value):
        calls.append(value)
        return token("new", "rotated", expired=False)

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", refresh)
    await ensure_oauth_fresh()
    assert calls == ["refresh"]
    assert load_token(KIMI_CODE_OAUTH_KEY).access_token == "new"


@pytest.mark.asyncio
async def test_concurrent_runtime_refresh_happens_once(monkeypatch):
    save_token("oauth/shared", token())
    calls = []

    def refresh(value):
        calls.append(value)
        time.sleep(0.03)
        return token("new", "rotated", expired=False)

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", refresh)
    managers = [OAuthManager(["oauth/shared"]) for _ in range(2)]
    await asyncio.gather(*(m.ensure_fresh() for m in managers), managers[0].ensure_fresh())
    assert calls == ["refresh"]
    assert [m.resolve_api_key("static", "oauth/shared") for m in managers] == ["new", "new"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_token", ["refresh", ""])
async def test_expired_or_rejected_never_shadows_static_key(monkeypatch, refresh_token):
    from coderai.llm import resolve_model_provider_routing

    save_token(KIMI_CODE_OAUTH_KEY, token(refresh=refresh_token))

    def reject(value):
        raise OAuthUnauthorized("rejected")

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", reject)
    manager = OAuthManager([KIMI_CODE_OAUTH_KEY])
    await manager.ensure_fresh()
    assert manager.resolve_api_key("static", KIMI_CODE_OAUTH_KEY) == "static"
    assert resolve_model_provider_routing("kimi-code", explicit_api_key="static")[1] == "static"
    if refresh_token:
        with pytest.raises(OAuthUnauthorized):
            await manager.ensure_fresh(force=True)


@pytest.mark.asyncio
async def test_request_boundary_updates_created_client_and_static_fallback(monkeypatch):
    from coderai.llm import prepare_oauth_request

    key = "oauth/request"
    save_token(key, token("initial", expired=False))
    manager = OAuthManager([key])
    client = SimpleNamespace(
        api_key="initial",
        _coderai_oauth_manager=manager,
        _coderai_oauth_key=key,
        _coderai_static_api_key="static",
    )
    save_token(key, token())
    monkeypatch.setattr(
        "coderai.auth.oauth.refresh_access_token", lambda value: token("rotated", expired=False)
    )
    await prepare_oauth_request(client)
    assert client.api_key == "rotated"
    save_token(key, token(refresh="rejected"))

    def reject(value):
        raise OAuthUnauthorized("rejected")

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", reject)
    await prepare_oauth_request(client)
    assert client.api_key == "static"
    client._coderai_static_api_key = ""
    with pytest.raises(OAuthUnauthorized):
        await prepare_oauth_request(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["direct", "sdk", "wire", "acp"])
@pytest.mark.parametrize("rejected", [False, True])
async def test_shared_factory_refreshes_before_first_turn(tmp_path, monkeypatch, adapter, rejected):
    from coderai.cli.session_factory import build_session_manager
    from coderai.config import Config, LLMModel, LLMProvider, OAuthRef

    save_token(KIMI_CODE_OAUTH_KEY, token())
    typed = Config(
        default_model="kimi-code",
        providers={
            "kimi": LLMProvider(
                type="kimi",
                base_url="https://api.kimi.com/coding/v1",
                api_key=SecretStr("static"),
                oauth=OAuthRef(storage="file", key=KIMI_CODE_OAUTH_KEY),
            )
        },
        models={"kimi-code": LLMModel(provider="kimi", model="kimi-code", max_context_size=128000)},
    )
    monkeypatch.setattr("coderai.config.load_typed_config", lambda *args, **kwargs: typed)
    monkeypatch.setattr(
        "coderai.cli.session_factory.resolve_current_settings",
        lambda root: {"model": "kimi-code", "mergeAllAvailableSkills": False},
    )
    monkeypatch.setattr(
        "coderai.config.resolve_current_settings", lambda root: {"model": "kimi-code"}
    )
    seen = []

    def refresh(value):
        if rejected:
            raise OAuthUnauthorized("rejected")
        return token("new", "rotated", expired=False)

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", refresh)

    class Client:
        def __init__(self, api_key, **kw):
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            seen.append(self.api_key)
            return reply("answer")

    monkeypatch.setattr("openai.OpenAI", Client)
    project = tmp_path / "project"
    project.mkdir()
    manager = build_session_manager(str(project), non_interactive=True)
    try:
        if adapter == "sdk":
            from coderai_sdk import CoderAIClient

            await CoderAIClient(engine=SessionManagerEngine(manager)).prompt("hello")
        elif adapter == "wire":
            from coderai.wire.server import WireServer

            server = WireServer(manager)
            server._initialized = True
            result = await server._handle_prompt("turn", {"user_input": "hello"})
            assert result["result"]["status"] == "finished"
            server._fallback_emitter.close()
        elif adapter == "acp":
            import acp
            from coderai.acp.session import ACPSession

            async def update(**kwargs):
                pass

            session = ACPSession(
                "acp", SessionManagerEngine(manager), SimpleNamespace(session_update=update)
            )
            result = await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])
            assert result.stop_reason == "end_turn"
        else:
            await manager.create_session("hello")
        assert seen == ["static" if rejected else "new"]
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_unrelated_static_turn_ignores_stale_oauth(tmp_path, monkeypatch):
    from coderai.auth.oauth import OAuthError

    save_token(KIMI_CODE_OAUTH_KEY, token())

    def broken(value):
        raise OAuthError("network down")

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", broken)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: reply("static answer"))
        )
    )
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o", "mergeAllAvailableSkills": False},
    )
    try:
        from coderai_sdk import CoderAIClient

        assert (
            await CoderAIClient(engine=SessionManagerEngine(manager)).prompt("hello")
        ).text == "static answer"
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_manager_refreshes_when_token_expires_between_requests(tmp_path, monkeypatch):
    key = "oauth/request-turn"
    save_token(key, token("first", expired=False))
    owner = OAuthManager([key])
    seen = []

    class Client:
        api_key = "first"
        _coderai_oauth_manager = owner
        _coderai_oauth_key = key
        _coderai_static_api_key = ""

        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            seen.append(self.api_key)
            if len(seen) == 1:
                save_token(key, token())
                return reply(
                    "check",
                    [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"file_path":"missing"}'},
                        }
                    ],
                )
            return reply("done")

    monkeypatch.setattr(
        "coderai.auth.oauth.refresh_access_token", lambda value: token("new", expired=False)
    )
    client = Client()
    manager = SessionManager(
        project_root=str(tmp_path),
        oauth_manager=owner,
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "mergeAllAvailableSkills": False,
            "permissions": {"defaultMode": "allowAll"},
        },
    )
    try:
        await manager.create_session("read")
        assert seen == ["first", "new"]
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_child_runner_refreshes_expired_credentials(tmp_path, monkeypatch):
    from coderai.subagents.runner import SubAgentManager
    from coderai.subagents.builder import SubAgentSpec

    key = "oauth/child"
    save_token(key, token())
    owner = OAuthManager([key])
    client = SimpleNamespace(
        api_key="expired",
        _coderai_oauth_manager=owner,
        _coderai_oauth_key=key,
        _coderai_static_api_key="static",
    )
    seen = []

    def complete(actual_client, request):
        seen.append(actual_client.api_key)
        return reply("child complete")

    monkeypatch.setattr("coderai.subagents.runner._call_llm_sync", complete)
    monkeypatch.setattr(
        "coderai.auth.oauth.refresh_access_token", lambda value: token("new", expired=False)
    )
    runner = SubAgentManager(
        str(tmp_path),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o", "oauthKey": key},
    )
    result = await runner._run_subagent_loop(
        SubAgentSpec(description="child", prompt="hello"), "child-session", asyncio.Event()
    )
    assert result.status == "completed" and seen == ["new"]


@pytest.mark.asyncio
async def test_unavailable_refresh_lock_never_rotates(monkeypatch):
    from coderai.auth.oauth import OAuthError

    save_token("oauth/lock", token())

    async def unavailable(self, **kwargs):
        return False

    monkeypatch.setattr("coderai.auth.oauth.CrossProcessLock.acquire_with_retry", unavailable)
    monkeypatch.setattr(
        "coderai.auth.oauth.refresh_access_token", lambda value: pytest.fail("refresh without lock")
    )
    with pytest.raises(OAuthError, match="lock"):
        await OAuthManager(["oauth/lock"]).ensure_fresh()


@pytest.mark.asyncio
@pytest.mark.parametrize("enveloped", [True, False])
async def test_serialized_partial_events_keep_call_identity(enveloped):
    from coderai_sdk import CoderAIClient
    from kosong.message import ToolCallPart as ArgumentPart

    events = [
        ToolCallPart(id="call", name="read", arguments="{"),
        ArgumentPart(arguments_part='"file_path":"a"}'),
        ToolResultPart(tool_call_id="call", output="okay"),
    ]
    envelopes = [serialize_wire_message(event) for event in events]
    decoded = [deserialize_wire_message(event) for event in envelopes]
    assert decoded[0].id == "call" and decoded[0].name == "read"
    result = await CoderAIClient(engine=Engine(envelopes if enveloped else decoded)).prompt("read")
    assert result.tool_calls == [{"id": "call", "name": "read", "arguments": '{"file_path":"a"}'}]
    assert result.tool_results[0]["output"] == "okay"


@pytest.mark.asyncio
async def test_force_refresh_rotates_unexpired_credentials(monkeypatch):
    save_token("oauth/forced", token("valid", expired=False))
    calls = []

    def refresh(value):
        calls.append(value)
        return token("forced", "rotated", expired=False)

    monkeypatch.setattr("coderai.auth.oauth.refresh_access_token", refresh)
    manager = OAuthManager(["oauth/forced"])
    await manager.ensure_fresh(force=True)
    assert calls == ["refresh"] and manager.resolve_api_key("", "oauth/forced") == "forced"


@pytest.mark.asyncio
async def test_project_scoped_alias_refreshes_without_static_fallback(tmp_path, monkeypatch):
    from coderai.llm import ensure_oauth_fresh, create_oauth_manager, create_openai_client
    from coderai.config import Config, LLMProvider, LLMModel, OAuthRef

    project = tmp_path / "project"
    project.mkdir()
    key = "oauth/project-only"
    save_token(key, token())
    typed = Config(
        default_model="local-alias",
        providers={
            "local": LLMProvider(
                type="kimi",
                base_url="https://kimi.invalid/v1",
                api_key=SecretStr(""),
                oauth=OAuthRef(storage="file", key=key),
            )
        },
        models={
            "local-alias": LLMModel(provider="local", model="kimi-code", max_context_size=128000)
        },
    )
    roots = []

    def load(*args, project_root=None):
        roots.append(project_root)
        return typed if str(project_root) == str(project) else Config()

    monkeypatch.setattr("coderai.config.load_typed_config", load)
    monkeypatch.setattr(
        "coderai.config.resolve_current_settings", lambda root: {"model": "local-alias"}
    )
    monkeypatch.setattr(
        "coderai.auth.oauth.refresh_access_token", lambda value: token("fresh", expired=False)
    )
    owner = create_oauth_manager(str(project))
    await ensure_oauth_fresh(oauth=owner, model="local-alias", project_root=str(project))
    info = create_openai_client(str(project), oauth=owner)
    assert info["client"].api_key == "fresh"
    assert info["oauthKey"] == key
    assert roots and all(str(root) == str(project) for root in roots)


@pytest.mark.asyncio
async def test_explicit_kimi_env_key_is_not_replaced_at_request(monkeypatch):
    from coderai.config import Config
    from coderai.llm import create_openai_client, prepare_oauth_request, create_oauth_manager

    save_token(KIMI_CODE_OAUTH_KEY, token("login", expired=False))
    monkeypatch.setenv("KIMI_API_KEY", "explicit-env-key")
    monkeypatch.setattr("coderai.config.load_typed_config", lambda *args, **kwargs: Config())
    monkeypatch.setattr(
        "coderai.config.resolve_current_settings", lambda root: {"model": "kimi-code"}
    )
    owner = create_oauth_manager()
    client = create_openai_client(oauth=owner)["client"]
    assert client.api_key == "explicit-env-key"
    await prepare_oauth_request(client, oauth=owner)
    assert client.api_key == "explicit-env-key"
