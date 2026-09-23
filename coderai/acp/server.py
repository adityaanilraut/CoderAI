"""In-process multi-session ACP server implementing the Agent Control Protocol."""

from __future__ import annotations

import contextlib
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import acp
from kaos.path import KaosPath

from coderai.acp.engine import SessionManagerEngine
from coderai.acp.kaos import ACPKaos
from coderai.acp.mcp import acp_mcp_servers_to_mcp_config
from coderai.acp.session import (
    ACPSession,
    load_engine_session_id,
    save_engine_session_id,
)
from coderai.acp.types import ACPContentBlock, MCPServer
from coderai.acp.version import ACPVersionSpec, negotiate_version
from coderai.auth.oauth import KIMI_CODE_OAUTH_KEY, load_tokens
from coderai.config import LLMModel, OAuthRef, load_config
from coderai.constant import NAME, VERSION
from coderai.llm import derive_model_capabilities
from coderai.session import Session
from coderai.utils.logging import logger

#: Page size for ``session/list`` cursor pagination (cursor = integer offset).
_SESSION_LIST_PAGE_SIZE = 50

#: Methods served by :meth:`ACPServer.ext_method` (``terminal/<op>`` handled
#: separately via :data:`coderai.acp.terminal.TERMINAL_METHODS`).
_EXT_METHOD_ALLOWLIST = frozenset({"ping", "version", "list_roles", "terminal/bridge"})


def _mcp_configs_to_servers(mcp_configs: list[Any] | None) -> dict[str, dict[str, Any]]:
    """Flatten ACP MCP configs to plain ``{name: cfg}`` server dicts.

    Accepts :class:`~fastmcp.mcp_config.MCPConfig` objects (what
    :func:`coderai.acp.mcp.acp_mcp_servers_to_mcp_config` returns) as well as
    plain dicts, so ACP-supplied servers actually reach the engine. Returns
    an empty dict when the client supplied no servers.
    """
    servers: dict[str, dict[str, Any]] = {}
    for config in mcp_configs or []:
        if config is None:
            continue
        payload: Any = None
        if isinstance(config, dict):
            payload = config.get("mcpServers", config)
        else:
            to_dict = getattr(config, "to_dict", None)
            if callable(to_dict):
                try:
                    payload = (to_dict() or {}).get("mcpServers", {})
                except Exception:
                    payload = None
            if payload is None:
                model_dump = getattr(config, "model_dump", None)
                if callable(model_dump):
                    try:
                        payload = (model_dump(exclude_none=True) or {}).get("mcpServers", {})
                    except Exception:
                        payload = None
            if payload is None:
                raw = getattr(config, "mcpServers", None)
                payload = raw if isinstance(raw, dict) else None
        if isinstance(payload, dict):
            for name, cfg in payload.items():
                if isinstance(name, str) and name and isinstance(cfg, dict):
                    servers[name] = cfg
    return servers


def _build_engine(
    session: Session,
    mcp_configs: list[Any] | None = None,
) -> SessionManagerEngine:
    """Build a Stack A (``SessionManager``) engine for one ACP session.

    ACP-supplied MCP servers are injected session-scoped through
    :func:`coderai.cli.session_factory.build_session_manager`'s
    ``mcp_servers`` overlay. The previous process-global
    ``CODERAI_MCP_CONFIG_JSON`` overlay is deliberately not used here so
    concurrent ACP sessions with different server sets cannot collide.
    """
    from coderai.cli.session_factory import build_session_manager
    from coderai.config import load_config

    manager = build_session_manager(
        str(session.work_dir),
        non_interactive=True,
        mcp_servers=_mcp_configs_to_servers(mcp_configs) or None,
    )
    return SessionManagerEngine(manager, config=load_config(), session=session)


class ACPServer:
    """Multi-session Agent Control Protocol server."""

    def __init__(self) -> None:
        self.client_capabilities: acp.schema.ClientCapabilities | None = None
        self.conn: acp.Client | None = None
        self.sessions: dict[str, tuple[ACPSession, _ModelIDConv]] = {}
        self.negotiated_version: ACPVersionSpec | None = None
        self._auth_methods: list[acp.schema.AuthMethod] = []
        self._terminal_bridges: dict[str, Any] = {}

    def on_connect(self, conn: acp.Client) -> None:
        logger.info("ACP client connected")
        self.conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: acp.schema.ClientCapabilities | None = None,
        client_info: acp.schema.Implementation | None = None,
        **kwargs: Any,
    ) -> acp.InitializeResponse:
        self.negotiated_version = negotiate_version(protocol_version)
        logger.info(
            "ACP server initialized with client protocol version: %s, "
            "negotiated version: %s, "
            "client capabilities: %s, client info: %s",
            protocol_version,
            self.negotiated_version,
            client_capabilities,
            client_info,
        )
        self.client_capabilities = client_capabilities

        command = sys.argv[0]
        terminal_args = ["login"]

        self._auth_methods = [
            acp.schema.AuthMethod(
                id="login",
                name=f"Login with {NAME} account",
                description=(
                    "Run `coderai login` command in the terminal, "
                    "then follow the instructions to finish login."
                ),
                field_meta={
                    "terminal-auth": {
                        "command": command,
                        "args": terminal_args,
                        "label": f"{NAME} Login",
                        "env": {},
                        "type": "terminal",
                    }
                },
            ),
        ]

        return acp.InitializeResponse(
            protocol_version=self.negotiated_version.protocol_version,
            agent_capabilities=acp.schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=acp.schema.PromptCapabilities(
                    embedded_context=True, image=True, audio=False
                ),
                mcp_capabilities=acp.schema.McpCapabilities(http=True, sse=False),
                session_capabilities=acp.schema.SessionCapabilities(
                    list=acp.schema.SessionListCapabilities(),
                    resume=acp.schema.SessionResumeCapabilities(),
                    fork=acp.schema.SessionForkCapabilities(),
                ),
            ),
            auth_methods=self._auth_methods,
            agent_info=acp.schema.Implementation(name=NAME, version=VERSION),
        )

    @staticmethod
    def _check_token_usable() -> str | None:
        """Return None if credentials or OAuth tokens are usable, else a reason string."""
        ref = OAuthRef(storage="file", key=KIMI_CODE_OAUTH_KEY)
        try:
            token = load_tokens(ref)
        except Exception:
            token = None

        if token is not None and token.access_token:
            if token.expires_at and token.expires_at < time.time() and not token.refresh_token:
                return "token expired and no refresh token available"
            return None

        # Fallback: check if API keys are configured in providers
        try:
            config = load_config()
            for provider in config.providers.values():
                if provider.api_key and provider.api_key.get_secret_value():
                    return None
        except Exception:
            pass

        return "no credentials configured (run `coderai login` first)"

    def _check_auth(self) -> None:
        """Check if authentication is complete. Raise AUTH_REQUIRED if not."""
        reason = self._check_token_usable()
        if reason:
            auth_methods_data: list[dict[str, Any]] = []
            for m in self._auth_methods:
                if m.field_meta and "terminal-auth" in m.field_meta:
                    terminal_auth = m.field_meta["terminal-auth"]
                    auth_methods_data.append(
                        {
                            "id": m.id,
                            "name": m.name,
                            "description": m.description,
                            "type": terminal_auth.get("type", "terminal"),
                            "command": terminal_auth.get("command", ""),
                            "args": terminal_auth.get("args", []),
                            "env": terminal_auth.get("env", {}),
                            "label": terminal_auth.get("label", ""),
                        }
                    )

            logger.warning("Authentication required: %s", reason)
            raise acp.RequestError.auth_required({"authMethods": auth_methods_data})

    async def new_session(
        self, cwd: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.NewSessionResponse:
        logger.info("Creating new session for working directory: %s", cwd)
        if self.conn is None:
            raise acp.RequestError.invalid_request({"connection": "ACP client not connected"})
        if self.client_capabilities is None:
            raise acp.RequestError.invalid_request({"connection": "ACP connection not initialized"})

        self._check_auth()

        session = await Session.create(KaosPath.unsafe_from_local_path(Path(cwd)))
        # ACP session identity lives in the session store, which hides
        # sessions with no history. Title it so it is listable/resumable from
        # the moment it is created, before the first prompt lands.
        try:
            from coderai.session_state import save_session_state

            session.state.custom_title = f"ACP {session.id[:8]}"
            save_session_state(session.state, session.dir)
        except Exception:
            logger.warning("Failed to title ACP session: %s", session.id)

        mcp_config = acp_mcp_servers_to_mcp_config(mcp_servers or [])
        engine = _build_engine(session, mcp_configs=[mcp_config])
        config = engine.config
        acp_kaos = ACPKaos(self.conn, session.id, self.client_capabilities)
        acp_session = ACPSession(session.id, engine, self.conn, kaos=acp_kaos)
        model_id_conv = _ModelIDConv(config.default_model or "", bool(config.default_thinking))
        self.sessions[session.id] = (acp_session, model_id_conv)

        # Advertise the live CoderAI slash catalog (Stack A), not the retired
        # Soul-slash registry.
        from coderai.ui.shell.slash import completion_entries

        available_commands = [
            acp.schema.AvailableCommand(name=name, description=description)
            for name, description in completion_entries()
        ]
        # Awaited (best-effort) instead of fire-and-forget so delivery
        # failures surface in logs instead of as unretrieved exceptions.
        try:
            await self.conn.session_update(
                session_id=session.id,
                update=acp.schema.AvailableCommandsUpdate(
                    session_update="available_commands_update",
                    available_commands=available_commands,
                ),
            )
        except Exception:
            logger.warning("Failed to publish available commands for %s", session.id)
        return acp.NewSessionResponse(
            session_id=session.id,
            modes=acp.schema.SessionModeState(
                available_modes=[
                    acp.schema.SessionMode(
                        id="default",
                        name="Default",
                        description="The default mode.",
                    ),
                ],
                current_mode_id="default",
            ),
            models=acp.schema.SessionModelState(
                available_models=_expand_llm_models(config.models),
                current_model_id=model_id_conv.to_acp_model_id(),
            ),
        )

    async def _setup_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[MCPServer] | None = None,
    ) -> tuple[ACPSession, _ModelIDConv]:
        if self.conn is None:
            raise acp.RequestError.invalid_request({"connection": "ACP client not connected"})
        if self.client_capabilities is None:
            raise acp.RequestError.invalid_request({"connection": "ACP connection not initialized"})

        work_dir = KaosPath.unsafe_from_local_path(Path(cwd))
        session = await Session.find(work_dir, session_id)
        if session is None:
            logger.error("Session not found: %s for working directory: %s", session_id, cwd)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        mcp_config = acp_mcp_servers_to_mcp_config(mcp_servers or [])
        engine = _build_engine(session, mcp_configs=[mcp_config])
        # Re-attach to an existing SessionManager session so resumed/forked ACP
        # sessions keep their conversation history instead of starting blank.
        # The ACP<->engine id binding is persisted in the session directory
        # (see coderai.acp.session.save_engine_session_id), which is what makes
        # resume work across process restarts. The same-id check is kept as a
        # fallback for sessions created before the binding was persisted.
        bound = False
        stored_id = load_engine_session_id(session.dir)
        if stored_id is not None and engine.manager.get_session(stored_id) is not None:
            engine.bind_session(stored_id)
            bound = True
        if not bound and engine.manager.get_session(session.id) is not None:
            engine.bind_session(session.id)
        config = engine.config
        acp_kaos = ACPKaos(self.conn, session.id, self.client_capabilities)
        acp_session = ACPSession(session.id, engine, self.conn, kaos=acp_kaos)
        model_id_conv = _ModelIDConv(config.default_model or "", bool(config.default_thinking))
        self.sessions[session.id] = (acp_session, model_id_conv)

        return acp_session, model_id_conv

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> None:
        logger.info("Loading session: %s for working directory: %s", session_id, cwd)
        if session_id in self.sessions:
            logger.warning("Session already loaded: %s", session_id)
            return

        self._check_auth()

        acp_session, _ = await self._setup_session(cwd, session_id, mcp_servers)
        wire_file = getattr(getattr(acp_session.cli, "session", None), "wire_file", None)
        if wire_file is not None:
            await acp_session.replay_history(wire_file)

    async def resume_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.schema.ResumeSessionResponse:
        logger.info("Resuming session: %s for working directory: %s", session_id, cwd)

        self._check_auth()

        if session_id not in self.sessions:
            await self._setup_session(cwd, session_id, mcp_servers)

        acp_session, model_id_conv = self.sessions[session_id]
        config = acp_session.cli.config
        return acp.schema.ResumeSessionResponse(
            modes=acp.schema.SessionModeState(
                available_modes=[
                    acp.schema.SessionMode(
                        id="default",
                        name="Default",
                        description="The default mode.",
                    ),
                ],
                current_mode_id="default",
            ),
            models=acp.schema.SessionModelState(
                available_models=_expand_llm_models(config.models),
                current_model_id=model_id_conv.to_acp_model_id(),
            ),
        )

    async def fork_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.schema.ForkSessionResponse:
        logger.info("Forking session: %s for working directory: %s", session_id, cwd)
        self._check_auth()
        if self.conn is None:
            raise acp.RequestError.invalid_request({"connection": "ACP client not connected"})
        if self.client_capabilities is None:
            raise acp.RequestError.invalid_request({"connection": "ACP connection not initialized"})

        work_dir = KaosPath.unsafe_from_local_path(Path(cwd))
        # Validate the parent before creating anything: forking an unknown
        # session must not leave an orphan session behind.
        parent_session = self.sessions.get(session_id)
        if parent_session is None and await Session.find(work_dir, session_id) is None:
            logger.error("Session not found: %s", session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        forked_session = await Session.create(work_dir)

        mcp_config = acp_mcp_servers_to_mcp_config(mcp_servers or [])
        engine = _build_engine(forked_session, mcp_configs=[mcp_config])
        config = engine.config
        acp_kaos = ACPKaos(self.conn, forked_session.id, self.client_capabilities)
        acp_session = ACPSession(forked_session.id, engine, self.conn, kaos=acp_kaos)
        model_id_conv = _ModelIDConv(config.default_model or "", bool(config.default_thinking))
        if parent_session is not None and parent_session[1].model_key:
            # Carry the parent's session-scoped model choice across; a fresh
            # engine would otherwise silently reset to the global default.
            parent_conv = parent_session[1]
            try:
                engine.set_model(parent_conv.model_key)
                set_thinking = getattr(engine, "set_thinking", None)
                if callable(set_thinking):
                    set_thinking(parent_conv.thinking)
                model_id_conv = parent_conv
            except Exception:
                logger.warning("Failed to inherit parent model for forked session")
        self.sessions[forked_session.id] = (acp_session, model_id_conv)

        # Carry the parent conversation across with a real SessionManager fork
        # (clones message history, event log, and file checkpoint).
        source_id = None
        if parent_session is not None:
            source_id = getattr(parent_session[0].cli, "session_id", None)
        else:
            # Fallback 1: Resolve the engine binding from the parent's real
            # session directory (where save_engine_session_id persists it).
            try:
                parent = await Session.find(work_dir, session_id)
                if parent is not None:
                    source_id = load_engine_session_id(parent.dir)
            except Exception:
                pass
            # Fallback 2: Check if session_id is directly a SessionManager session ID
            if not source_id:
                try:
                    if engine.manager.get_session(session_id):
                        source_id = session_id
                except Exception:
                    pass

        forked_id = None
        if source_id:
            try:
                forked_id = engine.manager.fork_session(source_id)
            except Exception as exc:
                logger.warning("SessionManager fork failed: %s", exc)
        if forked_id:
            engine.bind_session(forked_id)
            save_engine_session_id(forked_session.dir, forked_id)
        else:
            logger.warning(
                "Forked ACP session %s started without conversation history",
                forked_session.id,
            )

        return acp.schema.ForkSessionResponse(
            session_id=forked_session.id,
            modes=acp.schema.SessionModeState(
                available_modes=[
                    acp.schema.SessionMode(
                        id="default",
                        name="Default",
                        description="The default mode.",
                    ),
                ],
                current_mode_id="default",
            ),
            models=acp.schema.SessionModelState(
                available_models=_expand_llm_models(config.models),
                current_model_id=model_id_conv.to_acp_model_id(),
            ),
        )

    async def list_sessions(
        self, cursor: str | None = None, cwd: str | None = None, **kwargs: Any
    ) -> acp.schema.ListSessionsResponse:
        logger.info("Listing sessions for working directory: %s", cwd)
        if cwd is None:
            return acp.schema.ListSessionsResponse(sessions=[], next_cursor=None)
        offset = 0
        if cursor is not None:
            try:
                offset = max(0, int(cursor))
            except (TypeError, ValueError) as err:
                raise acp.RequestError.invalid_params(
                    {"cursor": "Cursor must be an integer offset"}
                ) from err
        work_dir = KaosPath.unsafe_from_local_path(Path(cwd))
        sessions = await Session.list(work_dir)
        page = sessions[offset : offset + _SESSION_LIST_PAGE_SIZE]
        next_cursor = (
            str(offset + _SESSION_LIST_PAGE_SIZE)
            if offset + _SESSION_LIST_PAGE_SIZE < len(sessions)
            else None
        )
        return acp.schema.ListSessionsResponse(
            sessions=[
                acp.schema.SessionInfo(
                    cwd=cwd,
                    session_id=s.id,
                    title=s.title,
                    updated_at=datetime.fromtimestamp(s.updated_at).astimezone().isoformat(),
                )
                for s in page
            ],
            next_cursor=next_cursor,
        )

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> None:
        if mode_id != "default":
            raise acp.RequestError.invalid_params({"mode_id": "Only default mode is supported"})

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        logger.info("Setting session model to %s for session: %s", model_id, session_id)
        if session_id not in self.sessions:
            logger.error("Session not found: %s", session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        acp_session, current_model_id = self.sessions[session_id]
        engine = acp_session.cli
        model_id_conv = _ModelIDConv.from_acp_model_id(model_id)
        if model_id_conv == current_model_id:
            return

        config = engine.config
        models = getattr(config, "models", None) or {}
        new_model = models.get(model_id_conv.model_key)
        if new_model is None:
            logger.error("Model not found: %s", model_id_conv.model_key)
            raise acp.RequestError.invalid_params({"model_id": "Model not found"})
        providers = getattr(config, "providers", None) or {}
        new_provider = providers.get(new_model.provider)
        if new_provider is None:
            logger.error(
                "Provider not found: %s for model: %s", new_model.provider, model_id_conv.model_key
            )
            raise acp.RequestError.invalid_params({"model_id": "Model's provider not found"})

        # Session-scoped Stack A overrides: the manager resolves the provider
        # per turn from these, so one session's choice never leaks into the
        # global config file or sibling sessions. The ",thinking" suffix maps
        # onto the same ``thinkingEnabled`` knob ``SessionManager`` turns use.
        engine.set_model(model_id_conv.model_key)
        set_thinking = getattr(engine, "set_thinking", None)
        if callable(set_thinking):
            set_thinking(model_id_conv.thinking)
        self.sessions[session_id] = (acp_session, model_id_conv)

    async def authenticate(self, method_id: str, **kwargs: Any) -> acp.AuthenticateResponse | None:
        if method_id == "login":
            reason = self._check_token_usable()
            if reason is None:
                logger.info("Authentication successful for method: %s", method_id)
                return acp.AuthenticateResponse()
            else:
                logger.warning("Authentication not complete for method: %s (%s)", method_id, reason)
                raise acp.RequestError.auth_required(
                    {
                        "message": "Please complete login in terminal first",
                        "authMethods": self._auth_methods,
                    }
                )

        logger.error("Unknown auth method: %s", method_id)
        raise acp.RequestError.invalid_params({"method_id": "Unknown auth method"})

    async def prompt(
        self, prompt: list[ACPContentBlock], session_id: str, **kwargs: Any
    ) -> acp.PromptResponse:
        logger.info("Received prompt request for session: %s", session_id)
        if session_id not in self.sessions:
            logger.error("Session not found: %s", session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})
        acp_session, *_ = self.sessions[session_id]
        return await acp_session.prompt(prompt)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        logger.info("Received cancel request for session: %s", session_id)
        if session_id not in self.sessions:
            logger.error("Session not found: %s", session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})
        acp_session, *_ = self.sessions[session_id]
        await acp_session.cancel()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        logger.info("Handling ext_method: %s", method)
        self._check_auth()
        if method == "ping":
            return {"status": "ok", "timestamp": time.time()}
        if method == "version":
            return {"version": VERSION, "name": NAME}
        if method == "list_roles":
            from coderai.subagents.registry import discover_markdown_agents

            roles = [d.name for d in discover_markdown_agents(Path.cwd())]
            return {"roles": roles}
        if method == "terminal/bridge":
            from coderai.acp.terminal import TERMINAL_METHODS

            return {"operations": [f"terminal/{op}" for op in TERMINAL_METHODS]}
        if method.startswith("terminal/"):
            return await self._terminal_ext_method(method[len("terminal/") :], params)
        if method not in _EXT_METHOD_ALLOWLIST:
            logger.warning("Unsupported ext_method: %s", method)
            raise acp.RequestError.method_not_found(method)
        # Fail closed: every allowlisted method returns above, so reaching
        # here means the allowlist and dispatch drifted apart.
        raise acp.RequestError.method_not_found(method)

    def _terminal_bridge(self, session_id: str) -> Any:
        """Per-ACP-session terminal bridge (lazily created, session-scoped)."""
        from coderai.acp.terminal import TerminalBridge

        bridge = self._terminal_bridges.get(session_id)
        if bridge is None:
            work_dir = "."
            entry = self.sessions.get(session_id)
            if entry is not None:
                with contextlib.suppress(Exception):
                    work_dir = str(entry[0].cli.session.work_dir)
            bridge = TerminalBridge(session_id, work_dir=work_dir)
            self._terminal_bridges[session_id] = bridge
        return bridge

    async def _terminal_ext_method(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        """Drive server-side PTY terminals for an ACP client (see acp/terminal.py)."""
        from coderai.acp.terminal import TERMINAL_METHODS

        if operation not in TERMINAL_METHODS:
            raise acp.RequestError.method_not_found(f"terminal/{operation}")
        session_id = str((params or {}).get("session_id") or (params or {}).get("sessionId") or "")
        if not session_id:
            raise acp.RequestError.invalid_params(
                {"session_id": "terminal/* methods require a session_id param"}
            )
        bridge = self._terminal_bridge(session_id)
        try:
            if operation == "list":
                return bridge.list_terminals()
            if operation == "open":
                return bridge.open_terminal(
                    command=(params or {}).get("command"),
                    name=(params or {}).get("name"),
                    cwd=(params or {}).get("cwd"),
                    env=(params or {}).get("env"),
                )
            terminal_id = str(
                (params or {}).get("terminal_id") or (params or {}).get("terminalId") or ""
            )
            if not terminal_id:
                raise acp.RequestError.invalid_params(
                    {"terminal_id": "terminal/* methods require a terminal_id param"}
                )
            if operation == "send":
                return bridge.send_terminal(
                    terminal_id,
                    str((params or {}).get("text") or ""),
                    submit=bool((params or {}).get("submit", True)),
                    timeout_ms=(params or {}).get("timeout_ms"),
                )
            if operation == "read":
                return bridge.read_terminal(terminal_id, (params or {}).get("timeout_ms"))
            if operation == "signal":
                return bridge.signal_terminal(
                    terminal_id, str((params or {}).get("signal") or "SIGINT")
                )
            return bridge.close_terminal(terminal_id)
        except acp.RequestError:
            raise
        except Exception as exc:
            logger.warning("terminal/%s failed: %s", operation, exc)
            raise acp.RequestError.internal_error(
                {"operation": operation, "error": str(exc)}
            ) from exc

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.info("Received ext_notification: %s", method)


class _ModelIDConv(NamedTuple):
    model_key: str
    thinking: bool

    @classmethod
    def from_acp_model_id(cls, model_id: str) -> _ModelIDConv:
        if model_id.endswith(",thinking"):
            return _ModelIDConv(model_id[: -len(",thinking")], True)
        return _ModelIDConv(model_id, False)

    def to_acp_model_id(self) -> str:
        if self.thinking:
            return f"{self.model_key},thinking"
        return self.model_key


def _expand_llm_models(models: dict[str, LLMModel]) -> list[acp.schema.ModelInfo]:
    expanded_models: list[acp.schema.ModelInfo] = []
    for model_key, model in models.items():
        capabilities = derive_model_capabilities(model)
        if "thinking" in model.model or "reason" in model.model:
            expanded_models.append(
                acp.schema.ModelInfo(
                    model_id=_ModelIDConv(model_key, True).to_acp_model_id(),
                    name=f"{model.model}",
                )
            )
        else:
            expanded_models.append(
                acp.schema.ModelInfo(
                    model_id=model_key,
                    name=model.model,
                )
            )
            if "thinking" in capabilities:
                expanded_models.append(
                    acp.schema.ModelInfo(
                        model_id=_ModelIDConv(model_key, True).to_acp_model_id(),
                        name=f"{model.model} (thinking)",
                    )
                )
    return expanded_models


__all__ = ["ACPServer"]
