"""Deterministic contracts for release archives and installed-wheel probing."""

from __future__ import annotations

import email.message
from pathlib import Path
import re

import click
import pytest

from coderAI import __version__
from coderAI._version import __version__ as source_version
from coderAI.cli.main import cli
from scripts import verify_wheel


def _click_paths(group: click.Group, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    paths = {prefix}
    for name, command in group.commands.items():
        path = (*prefix, name)
        paths.add(path)
        if isinstance(command, click.Group):
            paths.update(_click_paths(command, path))
    return paths


def _metadata(*, extras: tuple[str, ...] = verify_wheel.ADVERTISED_EXTRAS) -> email.message.Message:
    message = email.message.Message()
    message["Metadata-Version"] = "2.4"
    message["Name"] = verify_wheel.DIST_NAME
    message["Version"] = source_version
    message["Requires-Python"] = ">=3.10"
    requirements = {
        "semantic": "chromadb>=0.4.22",
        "local-embeddings": "sentence-transformers>=3.0.0",
        "web": "pypdf>=3.0.0",
        "browser": "playwright>=1.45.0",
    }
    for extra in extras:
        message["Provides-Extra"] = extra
        message["Requires-Dist"] = f'{requirements[extra]}; extra == "{extra}"'
    return message


def test_distribution_version_has_one_runtime_source() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert __version__ == source_version
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "coderAI._version.__version__"}' in pyproject
    project_table = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
    assert re.search(r"(?m)^version\s*=", project_table) is None


def test_wheel_probe_covers_every_click_entry_path() -> None:
    assert set(verify_wheel.CLI_HELP_PATHS) == _click_paths(cli)


def test_metadata_probe_requires_every_advertised_extra() -> None:
    metadata = _metadata(extras=("semantic", "local-embeddings", "web"))

    with pytest.raises(SystemExit, match="missing extras: browser"):
        verify_wheel._verify_metadata(
            metadata,
            label="test wheel",
            expected_version=source_version,
        )


def test_metadata_probe_requires_exact_version_and_dependency_binding() -> None:
    metadata = _metadata()
    metadata.replace_header(
        "Requires-Dist",
        'wrong-package>=1; extra == "semantic"',
    )

    with pytest.raises(SystemExit, match="does not bind chromadb to extra semantic"):
        verify_wheel._verify_metadata(
            metadata,
            label="test wheel",
            expected_version=source_version,
        )

    with pytest.raises(SystemExit, match="does not match"):
        verify_wheel._verify_metadata(
            _metadata(),
            label="test wheel",
            expected_version="999.0.0",
        )


def test_release_workflow_has_candidate_only_run_and_optional_exact_wheel_gates() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "Package version to validate without publishing" in workflow
    assert "extra: [semantic, local-embeddings, web, browser]" in workflow
    assert "--install-browser" in workflow
    assert workflow.count("if: github.event_name == 'push'") == 2
    assert "needs: [build, smoke-wheel, smoke-optionals]" in workflow
    assert "skip-existing" not in workflow


def test_sdist_manifest_keeps_release_docs_and_probe() -> None:
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert "include CHANGELOG.md" in manifest
    assert "include SECURITY.md" in manifest
    assert "include scripts/verify_wheel.py" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "prune tests" in manifest
