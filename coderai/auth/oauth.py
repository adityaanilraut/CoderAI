# Ported from coderai/core/oauth.py - kimi structure (auth/oauth.py).
"""OAuth device-flow engine (Kimi ``auth/oauth.py`` + ``platforms.py`` parity, slim).

Covers the interoperable subset without new dependencies (``requests`` is
already required):

- ``OAuthToken`` model + 0600 file store under ``~/.coderai/credentials/``
  with optional keyring-read migration (keyring itself stays optional)
- cross-process refresh lock (``fcntl``/``msvcrt``)
- RFC 8628 device authorization + polling + refresh with retries
- ``OAuthManager``: cached/persisted resolution, expiry-threshold refresh,
  rejection tombstones, ``resolve_api_key`` fallback
- platform definitions (Kimi Code + Moonshot, env-overridable) + model sync
- ``login``/``logout`` flows yielding ``OAuthEvent``s

Deliberately out of scope: the 60s background refresh task (refresh happens
at startup and on demand; a periodic task is follow-up).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import socket
import sys
import time
import uuid
import webbrowser
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

import requests

from coderai.utils.io import atomic_json_write
from coderai.share import get_share_dir

from coderai.auth.platforms import (
    KIMI_CODE_PLATFORM_ID,
    PLATFORMS,
    Platform,
    RemoteModelInfo,
    get_platform_by_id,
    list_remote_models,
    managed_model_key,
    managed_provider_key,
)
KIMI_CODE_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CODE_OAUTH_KEY = "oauth/kimi-code"
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
KEYRING_SERVICE = "coderai"
REFRESH_THRESHOLD_SECONDS = 300
REFRESH_THRESHOLD_RATIO = 0.5
UNAUTHORIZED_RETRY_COOLDOWN_S = 300
_RETRYABLE_REFRESH_STATUSES = {429, 500, 502, 503, 504}


def oauth_client_id() -> str:
    return os.getenv("CODERAI_OAUTH_CLIENT_ID") or KIMI_CODE_CLIENT_ID


def _oauth_host() -> str:
    return (
        os.getenv("CODERAI_OAUTH_HOST")
        or os.getenv("KIMI_CODE_OAUTH_HOST")
        or os.getenv("KIMI_OAUTH_HOST")
        or DEFAULT_OAUTH_HOST
    )


class OAuthError(RuntimeError):
    """OAuth flow error."""


class OAuthUnauthorized(OAuthError):
    """OAuth credentials rejected."""


class OAuthDeviceExpired(OAuthError):
    """Device authorization expired."""


class _RetryableRefreshError(OAuthError):
    """Transient HTTP error during token refresh."""


OAuthEventKind = Literal["info", "error", "waiting", "verification_url", "success"]


@dataclass(slots=True, frozen=True)
class OAuthEvent:
    type: OAuthEventKind
    message: str
    data: dict[str, Any] | None = None

    @property
    def json(self) -> str:
        payload: dict[str, Any] = {"type": self.type, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False)


@dataclass(slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    token_type: str = ""
    expires_in: float = 0.0

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> OAuthToken:
        expires_in = float(payload.get("expires_in") or 0)
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=time.time() + expires_in,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or ""),
            expires_in=expires_in,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OAuthToken:
        expires_at = payload.get("expires_at")
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=float(expires_at) if expires_at is not None else 0.0,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or ""),
            expires_in=float(payload.get("expires_in") or 0),
        )

    @property
    def needs_refresh(self) -> bool:
        if not self.refresh_token:
            return False
        return self.expires_at - time.time() < _refresh_threshold(self.expires_in)


def _refresh_threshold(expires_in: float) -> float:
    if expires_in > 0:
        return max(REFRESH_THRESHOLD_SECONDS, expires_in * REFRESH_THRESHOLD_RATIO)
    return REFRESH_THRESHOLD_SECONDS


# -- credential store -------------------------------------------------------


def _credentials_dir() -> Path:
    path = get_share_dir() / "credentials"
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    return path


def credentials_path(key: str) -> Path:
    name = key.removeprefix("oauth/").split("/")[-1] or key
    return _credentials_dir() / f"{name}.json"


def _lock_path(key: str) -> Path:
    name = key.removeprefix("oauth/").split("/")[-1] or key
    return _credentials_dir() / f"{name}.lock"


class CrossProcessLock:
    """File lock coordinating token refresh across processes (Kimi parity)."""

    def __init__(self, key: str) -> None:
        self._path = _lock_path(key)
        self._fd: int | None = None

    def _acquire_nowait(self) -> bool:
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"\0")
                    os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    with suppress(OSError):
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            finally:
                with suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    async def acquire_with_retry(self, retries: int = 5) -> bool:
        for _ in range(retries):
            try:
                if self._acquire_nowait():
                    return True
            except OSError:
                return False
            await asyncio.sleep(1 + random.random())
        try:
            return self._acquire_nowait()
        except OSError:
            return False

    async def __aenter__(self) -> bool:
        return await self.acquire_with_retry()

    async def __aexit__(self, *args: object) -> None:
        self.release()


def _load_from_keyring(key: str) -> OAuthToken | None:
    try:
        import keyring  # type: ignore[import-not-found]

        raw = keyring.get_password(KEYRING_SERVICE, key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return OAuthToken.from_dict(payload) if isinstance(payload, dict) else None


def _delete_from_keyring(key: str) -> None:
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.delete_password(KEYRING_SERVICE, key)
    except Exception:
        return


def load_token(key: str) -> OAuthToken | None:
    """Load a token from file, migrating legacy keyring entries (Kimi parity)."""
    path = credentials_path(key)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return OAuthToken.from_dict(payload)
        except (ValueError, OSError):
            return None
        return None
    token = _load_from_keyring(key)
    if token is None:
        return None
    try:
        save_token(key, token)
    except OSError:
        return token
    with suppress(Exception):
        _delete_from_keyring(key)
    return token


def save_token(key: str, token: OAuthToken) -> None:
    atomic_json_write(token.to_dict(), credentials_path(key))
    with suppress(OSError):
        os.chmod(credentials_path(key), 0o600)


def delete_token(key: str) -> None:
    _delete_from_keyring(key)
    with suppress(OSError):
        credentials_path(key).unlink()


# Kimi parity aliases
def load_tokens(ref: Any) -> OAuthToken | None:
    key = getattr(ref, "key", ref) if not isinstance(ref, str) else ref
    return load_token(str(key))


def save_tokens(ref: Any, token: OAuthToken) -> Any:
    key = getattr(ref, "key", ref) if not isinstance(ref, str) else ref
    save_token(str(key), token)
    return ref



# -- device flow HTTP --------------------------------------------------------


def _common_headers() -> dict[str, str]:
    from coderai._version import __version__

    return {
        "X-Client-Name": "coderai",
        "X-Client-Version": __version__,
        "X-Device-Id": _device_id(),
        "X-Device-Name": (platform.node() or socket.gethostname())[:64],
    }


def _device_id() -> str:
    path = get_share_dir() / "device_id"
    if path.exists():
        with suppress(OSError):
            if text := path.read_text(encoding="utf-8").strip():
                return text
    device_id = uuid.uuid4().hex
    with suppress(OSError):
        path.write_text(device_id, encoding="utf-8")
        os.chmod(path, 0o600)
    return device_id


@dataclass(slots=True)
class DeviceAuthorization:
    user_code: str
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int | None
    interval: int


def request_device_authorization() -> DeviceAuthorization:
    try:
        resp = requests.post(
            f"{_oauth_host().rstrip('/')}/api/oauth/device_authorization",
            data={"client_id": oauth_client_id()},
            headers=_common_headers(),
            timeout=30,
        )
        data = resp.json()
    except Exception as exc:
        raise OAuthError(f"Device authorization request failed: {exc}") from exc
    if resp.status_code != 200 or not isinstance(data, dict):
        raise OAuthError(f"Device authorization failed: {data}")
    try:
        return DeviceAuthorization(
            user_code=str(data["user_code"]),
            device_code=str(data["device_code"]),
            verification_uri=str(data.get("verification_uri") or ""),
            verification_uri_complete=str(data["verification_uri_complete"]),
            expires_in=int(data.get("expires_in") or 0) or None,
            interval=int(data.get("interval") or 5),
        )
    except (KeyError, ValueError) as exc:
        raise OAuthError(f"Unexpected device authorization response: {data}") from exc


def wait_for_device_token(
    auth: DeviceAuthorization, on_waiting: Any = None
) -> OAuthToken:
    """Poll until the user authorizes (sync; async callers use ``to_thread``)."""
    interval = max(auth.interval, 1)
    printed_wait = False
    while True:
        status, data = _poll_device_token(auth)
        if status == 200 and "access_token" in data:
            return OAuthToken.from_response(data)
        error_code = str(data.get("error") or "unknown_error")
        if error_code == "expired_token":
            raise OAuthDeviceExpired("Device code expired.")
        if not printed_wait:
            printed_wait = True
            if on_waiting is not None:
                try:
                    on_waiting(error_code)
                except Exception:
                    pass
        time.sleep(interval)


def _poll_device_token(auth: DeviceAuthorization) -> tuple[int, dict[str, Any]]:
    try:
        resp = requests.post(
            f"{_oauth_host().rstrip('/')}/api/oauth/token",
            data={
                "client_id": oauth_client_id(),
                "device_code": auth.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers=_common_headers(),
            timeout=30,
        )
        data = resp.json()
    except Exception as exc:
        raise OAuthError("Token polling request failed.") from exc
    if not isinstance(data, dict):
        raise OAuthError("Unexpected token polling response.")
    if resp.status_code >= 500:
        raise OAuthError(f"Token polling server error: {resp.status_code}.")
    return resp.status_code, data


def refresh_access_token(refresh_token: str, *, max_retries: int = 3) -> OAuthToken:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{_oauth_host().rstrip('/')}/api/oauth/token",
                data={
                    "client_id": oauth_client_id(),
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers=_common_headers(),
                timeout=30,
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code in (401, 403):
                raise OAuthUnauthorized(
                    data.get("error_description") or "Token refresh unauthorized."
                )
            if resp.status_code != 200:
                desc = data.get("error_description") or f"Token refresh failed ({resp.status_code})."
                if resp.status_code in _RETRYABLE_REFRESH_STATUSES:
                    raise _RetryableRefreshError(desc)
                raise OAuthError(desc)
            return OAuthToken.from_response(data)
        except OAuthUnauthorized:
            raise
        except (requests.RequestException, OSError, _RetryableRefreshError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise OAuthError("Token refresh failed after retries.") from last_exc


# -- manager ------------------------------------------------------------------


@dataclass(slots=True)
class _RejectedRefreshState:
    refresh_token: str
    retry_after: float


_REJECTED_REFRESH_TOKENS: dict[str, _RejectedRefreshState] = {}


class OAuthManager:
    """Resolve + refresh OAuth-backed provider credentials (Kimi parity, sync core)."""

    def __init__(self, oauth_keys: list[str] | None = None) -> None:
        self._access_tokens: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._keys = list(oauth_keys or [])
        for key in self._keys:
            token = load_token(key)
            if token is not None and token.access_token:
                self._access_tokens[key] = token.access_token

    @classmethod
    def from_typed_config(cls, typed: Any) -> OAuthManager:
        keys: list[str] = []
        try:
            for provider in typed.providers.values():
                if getattr(provider, "oauth", None):
                    keys.append(provider.oauth.key)
            for service in (
                typed.services.moonshot_search,
                typed.services.moonshot_fetch,
            ):
                if service is not None and getattr(service, "oauth", None):
                    keys.append(service.oauth.key)
        except Exception:
            pass
        return cls(keys)

    def get_cached_access_token(self, key: str) -> str | None:
        return self._access_tokens.get(key)

    def resolve_api_key(self, api_key: str, oauth_key: str | None) -> str:
        """Prefer the OAuth access token; fall back to the static key (Kimi parity).

        A recently-rejected token stays suppressed so a dead credential never
        shadows a configured static fallback.
        """
        if oauth_key:
            token = self._access_tokens.get(oauth_key)
            if token is None:
                persisted = load_token(oauth_key)
                if persisted is not None and persisted.access_token:
                    if self._tombstone(oauth_key, persisted.refresh_token) is None:
                        self._access_tokens[oauth_key] = persisted.access_token
                        token = persisted.access_token
            if token:
                return token
        return api_key

    def _tombstone(self, key: str, refresh_token: str | None) -> _RejectedRefreshState | None:
        if not refresh_token:
            return None
        state = _REJECTED_REFRESH_TOKENS.get(key)
        if state is not None and state.refresh_token != refresh_token:
            _REJECTED_REFRESH_TOKENS.pop(key, None)
            return None
        return state

    def _can_retry_rejected(self, key: str, refresh_token: str | None) -> bool:
        state = self._tombstone(key, refresh_token)
        return state is None or time.time() >= state.retry_after

    async def ensure_fresh(self, *, force: bool = False) -> None:
        """Refresh cached tokens near expiry (startup / on-demand, Kimi parity)."""
        for key in self._keys:
            token = load_token(key)
            if token is None or not token.refresh_token:
                continue
            state = self._tombstone(key, token.refresh_token)
            if state is not None and time.time() < state.retry_after and not force:
                continue
            if not force and not token.needs_refresh:
                self._access_tokens[key] = token.access_token
                continue
            async with self._refresh_lock:
                fresh = load_token(key)
                current = fresh or token
                if not force and fresh is not None and not fresh.needs_refresh:
                    self._access_tokens[key] = fresh.access_token
                    continue
                lock = CrossProcessLock(key)
                acquired = await lock.acquire_with_retry()
                try:
                    if acquired:
                        locked = load_token(key)
                        if (
                            locked is not None
                            and locked.refresh_token != current.refresh_token
                        ):
                            _REJECTED_REFRESH_TOKENS.pop(key, None)
                            self._access_tokens[key] = locked.access_token
                            continue
                    # Kimi parity: a recently-rejected token is not retried
                    # within the cooldown; force re-raises instead.
                    if not self._can_retry_rejected(key, current.refresh_token):
                        self._access_tokens.pop(key, None)
                        if force:
                            raise OAuthUnauthorized(
                                "Refresh token was recently rejected."
                            )
                        return
                    try:
                        refreshed = await asyncio.to_thread(
                            refresh_access_token, current.refresh_token
                        )
                    except OAuthUnauthorized:
                        latest = load_token(key)
                        if latest is not None and latest.refresh_token != current.refresh_token:
                            _REJECTED_REFRESH_TOKENS.pop(key, None)
                            self._access_tokens[key] = latest.access_token
                            continue
                        _REJECTED_REFRESH_TOKENS[key] = _RejectedRefreshState(
                            refresh_token=current.refresh_token,
                            retry_after=time.time() + UNAUTHORIZED_RETRY_COOLDOWN_S,
                        )
                        self._access_tokens.pop(key, None)
                        if force:
                            raise
                        continue
                    _REJECTED_REFRESH_TOKENS.pop(key, None)
                    save_token(key, refreshed)
                    self._access_tokens[key] = refreshed.access_token
                finally:
                    lock.release()


# -- login / logout flows -------------------------------------------------------


async def login_device_flow(
    *, open_browser: bool = True
) -> AsyncIterator[OAuthEvent]:
    """Run the RFC 8628 device flow, yielding progress events (Kimi parity)."""
    platform = get_platform_by_id(KIMI_CODE_PLATFORM_ID)
    if platform is None:
        yield OAuthEvent("error", "Login platform is unavailable.")
        return
    try:
        auth = await asyncio.to_thread(request_device_authorization)
    except Exception as exc:
        yield OAuthEvent("error", f"Login failed: {exc}")
        return
    yield OAuthEvent("info", "Visit the URL below to authorize this device.")
    yield OAuthEvent(
        "verification_url",
        f"Verification URL: {auth.verification_uri_complete}",
        data={
            "verification_url": auth.verification_uri_complete,
            "user_code": auth.user_code,
        },
    )
    if open_browser:
        with suppress(Exception):
            webbrowser.open(auth.verification_uri_complete)
    token: OAuthToken | None = None
    waiting_event: OAuthEvent | None = None

    def _on_waiting(error_code: str) -> None:
        nonlocal waiting_event
        waiting_event = OAuthEvent(
            "waiting",
            f"Waiting for authorization... {auth.user_code}",
            data={"error": error_code},
        )

    try:
        token = await asyncio.to_thread(wait_for_device_token, auth, _on_waiting)
    except OAuthDeviceExpired:
        yield OAuthEvent("info", "Device code expired, restarting login...")
        async for event in login_device_flow(open_browser=False):
            yield event
        return
    except OAuthError as exc:
        yield OAuthEvent("error", f"Login failed: {exc}")
        return
    if waiting_event is not None:
        yield waiting_event
    assert token is not None
    save_token(KIMI_CODE_OAUTH_KEY, token)
    try:
        models = await asyncio.to_thread(list_remote_models, platform, token.access_token)
    except Exception as exc:
        yield OAuthEvent("error", f"Logged in, but listing models failed: {exc}")
        return
    if not models:
        yield OAuthEvent("error", "No models available for this account.")
        return
    yield OAuthEvent(
        "success",
        f"Logged in ({len(models)} models available).",
        data={"models": [m.id for m in models]},
    )


def apply_login_config(models: list[RemoteModelInfo]) -> str:
    """Persist the managed provider + synced models to the typed config.

    Returns the default model key. Mirrors Kimi ``_apply_kimi_code_config``.
    """
    from pydantic import SecretStr

    from coderai.config import (
        LLMModel,
        LLMProvider,
        OAuthRef,
        get_default_config,
        load_typed_config,
        save_typed_config,
    )

    platform = get_platform_by_id(KIMI_CODE_PLATFORM_ID)
    if platform is None:  # pragma: no cover - static table above
        raise OAuthError("Login platform is unavailable.")
    try:
        config = load_typed_config()
    except Exception:
        config = get_default_config()
    provider_key = managed_provider_key(platform.id)
    oauth_ref = OAuthRef(storage="file", key=KIMI_CODE_OAUTH_KEY)
    config.providers[provider_key] = LLMProvider(
        type="kimi",
        base_url=platform.base_url,
        api_key=SecretStr(""),
        oauth=oauth_ref,
    )
    for key in [k for k, m in config.models.items() if m.provider == provider_key]:
        del config.models[key]
    for info in models:
        caps = info.capabilities or None
        config.models[managed_model_key(platform.id, info.id)] = LLMModel(
            provider=provider_key,
            model=info.id,
            max_context_size=info.context_length,
            capabilities=caps,  # type: ignore[arg-type]
            display_name=info.display_name,
        )
    default = managed_model_key(platform.id, models[0].id)
    config.default_model = default
    save_typed_config(config)

    # Sync to user settings and environment so active model immediately changes
    try:
        from coderai.config import save_active_model_setting, save_setting_key

        save_active_model_setting(default, scope="user")
        save_setting_key("providerType", "kimi", scope="user")
        save_setting_key("baseURL", platform.base_url, scope="user")
    except Exception:
        pass

    return default


def clear_login_config() -> bool:
    """Remove the managed provider + models from the typed config."""
    try:
        config = load_typed_config()
    except Exception:
        return False
    provider_key = managed_provider_key(KIMI_CODE_PLATFORM_ID)
    if provider_key not in config.providers:
        return False
    del config.providers[provider_key]
    removed_default = False
    for key in [k for k, m in config.models.items() if m.provider == provider_key]:
        del config.models[key]
        if config.default_model == key:
            removed_default = True
    if removed_default:
        config.default_model = next(iter(config.models), "")
    save_typed_config(config)
    return True


def logout_all() -> list[str]:
    """Delete stored OAuth tokens; returns the removed keys."""
    removed: list[str] = []
    credentials = get_share_dir() / "credentials"
    if credentials.is_dir():
        for child in sorted(credentials.iterdir()):
            if child.is_file() and child.suffix == ".json":
                with suppress(OSError):
                    child.unlink()
                removed.append(child.stem)
    delete_token(KIMI_CODE_OAUTH_KEY)
    return removed
