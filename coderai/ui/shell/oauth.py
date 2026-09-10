# Ported from coderai/cli/login_cmd.py - kimi structure (ui/shell/oauth.py).
"""``coderai login`` / ``coderai logout`` (Kimi OAuth device-flow parity).

``login`` runs the RFC 8628 device flow against the configured OAuth host,
persists the token (0600 file), and writes the managed provider + synced
models into the typed config. ``logout`` removes tokens and the managed
provider. Both support ``--json`` for scripted use.
"""

from __future__ import annotations

import json
import webbrowser
from contextlib import suppress
from typing import Any

from coderai.core.oauth import (
    KIMI_CODE_PLATFORM_ID,
    OAuthDeviceExpired,
    OAuthError,
    apply_login_config,
    clear_login_config,
    get_platform_by_id,
    list_remote_models,
    logout_all,
    request_device_authorization,
    save_token,
    KIMI_CODE_OAUTH_KEY,
    wait_for_device_token,
)


def _emit(event: Any, as_json: bool) -> None:
    if as_json:
        print(event.json, flush=True)
    elif event.type == "verification_url":
        data = event.data or {}
        print(f"\n  Verification URL: {data.get('verification_url', '')}")
        print(f"  User code: {data.get('user_code', '')}\n", flush=True)
    elif event.type == "error":
        print(f"Error: {event.message}", flush=True)
    else:
        print(event.message, flush=True)


def perform_login_sync(*, open_browser: bool = True, as_json: bool = False) -> int:
    """Run the device flow synchronously (subcommand + setup wizard share this)."""
    from coderai.core.oauth import OAuthEvent

    platform = get_platform_by_id(KIMI_CODE_PLATFORM_ID)
    if platform is None:
        print("Error: login platform is unavailable.")
        return 1
    while True:
        try:
            auth = request_device_authorization()
        except Exception as exc:
            _emit(OAuthEvent("error", f"Login failed: {exc}"), as_json)
            return 1
        _emit(OAuthEvent("info", "Visit the URL below to authorize this device."), as_json)
        _emit(
            OAuthEvent(
                "verification_url",
                f"Verification URL: {auth.verification_uri_complete}",
                data={
                    "verification_url": auth.verification_uri_complete,
                    "user_code": auth.user_code,
                },
            ),
            as_json,
        )
        if open_browser:
            with suppress(Exception):
                webbrowser.open(auth.verification_uri_complete)
        try:
            token = wait_for_device_token(
                auth,
                on_waiting=lambda _code: _emit(
                    OAuthEvent(
                        "waiting", f"Waiting for authorization... {auth.user_code}"
                    ),
                    as_json,
                ),
            )
        except OAuthDeviceExpired:
            _emit(OAuthEvent("info", "Device code expired, restarting login..."), as_json)
            continue
        except OAuthError as exc:
            _emit(OAuthEvent("error", f"Login failed: {exc}"), as_json)
            return 1
        break
    save_token(KIMI_CODE_OAUTH_KEY, token)
    try:
        models = list_remote_models(platform, token.access_token)
    except Exception as exc:
        _emit(OAuthEvent("error", f"Logged in, but listing models failed: {exc}"), as_json)
        return 1
    if not models:
        _emit(OAuthEvent("error", "No models available for this account."), as_json)
        return 1
    _emit(
        OAuthEvent(
            "success",
            f"Logged in ({len(models)} models available).",
            data={"models": [m.id for m in models]},
        ),
        as_json,
    )
    try:
        default = apply_login_config(models)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}) if as_json else f"Error saving config: {exc}")
        return 1
    msg = f"Default model: {default}"
    print(json.dumps({"default_model": default}) if as_json else msg, flush=True)
    return 0


def cmd_login(argv: list[str]) -> int:
    """Implement ``coderai login [--json] [--no-browser]``."""
    as_json = "--json" in argv
    open_browser = "--no-browser" not in argv
    unknown = [a for a in argv if a not in ("--json", "--no-browser", "-h", "--help")]
    if unknown or "-h" in argv or "--help" in argv:
        print("Usage: coderai login [--json] [--no-browser]")
        return 0 if not unknown else 2
    try:
        return perform_login_sync(open_browser=open_browser, as_json=as_json)
    except KeyboardInterrupt:
        print("Login cancelled.")
        return 1


def cmd_logout(argv: list[str]) -> int:
    """Implement ``coderai logout [--json]``."""
    as_json = "--json" in argv
    unknown = [a for a in argv if a not in ("--json", "-h", "--help")]
    if unknown or "-h" in argv or "--help" in argv:
        print("Usage: coderai logout [--json]")
        return 0 if not unknown else 2
    removed = logout_all()
    cleared = clear_login_config()
    if as_json:
        print(json.dumps({"removed_tokens": removed, "cleared_provider": cleared}))
    else:
        print(
            f"Logged out ({len(removed)} token file(s) removed"
            + (", managed provider cleared" if cleared else "") + ")."
        )
    return 0


def run_login(argv: list[str]) -> int:
    return cmd_login(argv)


def run_logout(argv: list[str]) -> int:
    return cmd_logout(argv)
