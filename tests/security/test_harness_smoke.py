"""Phase 0 smoke tests: prove the security scaffolding itself works.

These are not red-team tests against product code — they only verify that the
shared fixtures build correctly, are importable by phase test files, and that
`pytest -m security` selects this package. Later phases replace/extend the
coverage here with real vulnerability regressions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coderAI.tools.web._http import _is_ip_public
from .conftest import INTERNAL_IP_TARGETS


def test_security_marker_is_applied(request: pytest.FixtureRequest) -> None:
    """The conftest auto-marks everything under tests/security/ as `security`."""
    assert request.node.get_closest_marker("security") is not None


# ── malicious_repo ──────────────────────────────────────────────────────────


def test_malicious_repo_builds_all_surfaces(malicious_repo) -> None:
    repo = malicious_repo()

    # Every attacker-controlled ingestion surface exists and is well-formed.
    assert repo.hooks_path.exists()
    assert repo.config_path.exists()
    assert repo.rule_path.exists()
    assert repo.skill_path.exists()
    assert repo.agents_path.exists()

    # The unique marker is planted in the data-driven surfaces so a test can
    # assert it is never treated as an instruction.
    assert repo.marker in repo.rule_path.read_text()
    assert repo.marker in repo.skill_path.read_text()
    assert repo.marker in repo.agents_path.read_text()

    # Building the repo must NOT execute the payload: the hook sentinel is the
    # observable side effect a trust test asserts is absent.
    assert not repo.sentinel.exists()

    # __fspath__ makes the repo usable anywhere a path is expected.
    assert os.fspath(repo) == str(repo.path)
    assert Path(repo).is_dir()


def test_malicious_repo_hooks_json_shape(malicious_repo) -> None:
    import json

    repo = malicious_repo()
    data = json.loads(repo.hooks_path.read_text())
    types = {h["type"] for h in data["hooks"]}
    # Mirrors the real hooks.json contract (hooks_manager.py).
    assert "PreToolUse" in types
    assert "permission.ask" in types
    assert all("command" in h and "tool" in h for h in data["hooks"])


def test_malicious_repo_is_parametrizable(malicious_repo) -> None:
    repo = malicious_repo(
        marker="CUSTOM-MARK",
        hook_command="echo custom",
        config_overrides={"budget_limit": 42.0},
        permission_status="deny",
    )
    import json

    assert repo.marker == "CUSTOM-MARK"
    assert repo.hook_command == "echo custom"
    cfg = json.loads(repo.config_path.read_text())
    assert cfg["budget_limit"] == 42.0
    hooks = json.loads(repo.hooks_path.read_text())
    ask = next(h for h in hooks["hooks"] if h["type"] == "permission.ask")
    assert "deny" in ask["command"]

    # Distinct calls produce distinct directories.
    other = malicious_repo()
    assert other.path != repo.path


# ── isolated_home ───────────────────────────────────────────────────────────


def test_isolated_home_redirects_home_and_config(isolated_home: Path) -> None:
    from coderAI.system.config import config_manager

    assert Path.home() == isolated_home
    assert os.environ["HOME"] == str(isolated_home)
    assert os.path.expanduser("~") == str(isolated_home)

    # The config singleton now writes under the isolated home, not real ~/.coderAI.
    assert config_manager.config_dir == isolated_home / ".coderAI"
    assert isolated_home.parents  # sanity: it's a real tmp path
    assert str(isolated_home).startswith(str(Path(isolated_home).anchor))


# ── internal_ip_targets ─────────────────────────────────────────────────────


def test_internal_ip_targets_fixture_matches_constant(
    internal_ip_targets,
) -> None:
    assert internal_ip_targets == INTERNAL_IP_TARGETS
    assert "169.254.169.254" in internal_ip_targets  # cloud metadata IMDS


@pytest.mark.parametrize(
    "ip",
    ["169.254.169.254", "127.0.0.1", "10.0.0.5", "::1"],
)
def test_internal_ip_literals_are_not_public(ip: str) -> None:
    """Sanity-check the SSRF classifier the phase-6 tests depend on."""
    assert _is_ip_public(ip) is False


# ── ssrf_redirect_server ────────────────────────────────────────────────────


@pytest.mark.enable_socket  # exercises a real localhost aiohttp redirect server
async def test_ssrf_redirect_server_302_to_target(ssrf_redirect_server) -> None:
    import aiohttp

    server = ssrf_redirect_server
    target = "https://example.com/final"
    redirect_url = server.redirect_to(target)

    async with aiohttp.ClientSession() as session:
        # allow_redirects=False so we observe the raw 302 + Location, exactly
        # like the SSRF-safe manual redirect loop in web/_http.py does.
        async with session.get(redirect_url, allow_redirects=False) as resp:
            assert resp.status == 302
            assert resp.headers["Location"] == target

        async with session.get(server.ok_url) as resp:
            assert resp.status == 200
            assert (await resp.text()) == "OK"

    # The server recorded the hits (used by phase-6 "was the target fetched?").
    assert any("/redirect" in h for h in server.hits)
    assert any("/ok" in h for h in server.hits)


@pytest.mark.enable_socket  # exercises a real localhost aiohttp redirect server
async def test_ssrf_redirect_server_chain(ssrf_redirect_server) -> None:
    import aiohttp

    server = ssrf_redirect_server
    hops = ["https://a.example/1", "https://b.example/2"]
    async with aiohttp.ClientSession() as session:
        async with session.get(server.redirect_chain(hops), allow_redirects=False) as resp:
            assert resp.status == 302
            # First hop points back at /chain with the remaining target.
            assert "/chain?" in resp.headers["Location"]
