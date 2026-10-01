"""Offline credential routing and private secret persistence boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from coderai.auth import oauth
from coderai.config import _write_settings_file
from coderai.llm import resolve_model_provider_routing


@pytest.mark.parametrize("caller", ["settings", "oauth"])
@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing-public"])
def test_secret_temporary_and_replacement_are_private(tmp_path, monkeypatch, caller, existing):
    target = tmp_path / "secret.json"
    if existing:
        target.write_text("{}")
        target.chmod(0o644)
    monkeypatch.setattr(oauth, "credentials_path", lambda _: target)
    opened = []
    replaced = []
    real_open, real_replace = os.open, os.replace

    def observe_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        if str(path).endswith(".tmp"):
            opened.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    def observe_replace(src, dst):
        replaced.append(stat.S_IMODE(Path(src).stat().st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "replace", observe_replace)
    old_umask = os.umask(0)
    try:
        if caller == "settings":
            _write_settings_file(str(target), {"apiKey": "test-secret"})
        else:
            oauth.save_token("test", oauth.OAuthToken("test-access", "test-refresh", 9999999999))
    finally:
        os.umask(old_umask)
    assert opened == replaced == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("model", ["kimi-code", "moonshot-v1"])
@pytest.mark.parametrize(
    "explicit,generic",
    [("sk-proj-rejected", None), (None, "sk-proj-generic"), (None, "generic-key")],
)
def test_kimi_never_inherits_generic_openai_credentials(
    isolated_home, monkeypatch, model, explicit, generic
):
    # No disk/keyring OAuth access and no provider calls.
    monkeypatch.setattr(oauth.OAuthManager, "resolve_api_key", lambda *args: None)
    if generic:
        monkeypatch.setenv("OPENAI_API_KEY", generic)
    base, key = resolve_model_provider_routing(model, explicit_api_key=explicit)
    assert base == "https://api.kimi.com/coding/v1"
    assert key is None


@pytest.mark.parametrize(
    "mapping,ambient,token,explicit,expected",
    [
        (
            {"KIMI_API_KEY": "dedicated"},
            {"KIMI_API_KEY": "ambient"},
            "oauth",
            "explicit",
            "dedicated",
        ),
        (
            {},
            {"KIMI_API_KEY": "ambient", "MOONSHOT_API_KEY": "moonshot"},
            "oauth",
            "explicit",
            "ambient",
        ),
        ({"MOONSHOT_API_KEY": "moonshot"}, {}, "oauth", "explicit", "moonshot"),
        ({}, {}, "oauth", "explicit", "oauth"),
        ({}, {}, None, "dedicated-explicit", "dedicated-explicit"),
    ],
    ids=["mapping-kimi", "ambient-kimi", "mapping-moonshot", "oauth", "explicit"],
)
def test_kimi_dedicated_and_oauth_precedence(
    isolated_home, monkeypatch, mapping, ambient, token, explicit, expected
):
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(oauth.OAuthManager, "resolve_api_key", lambda *args: token)
    assert (
        resolve_model_provider_routing("kimi-code", env=mapping, explicit_api_key=explicit)[1]
        == expected
    )


def test_kimi_oauth_can_be_disabled(isolated_home, monkeypatch):
    calls = []
    monkeypatch.setattr(oauth.OAuthManager, "resolve_api_key", lambda *args: calls.append(args))
    assert (
        resolve_model_provider_routing("kimi-code", explicit_api_key="explicit", use_oauth=False)[1]
        == "explicit"
    )
    assert calls == []
