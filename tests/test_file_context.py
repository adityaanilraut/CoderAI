"""File context adapters accept explicit path values and enforce isolation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coderai.tools.file.utils import check_file_write_access, get_effective_workdir


@pytest.mark.parametrize("as_dict", [False, True])
def test_context_path_precedence_and_isolation(tmp_path, as_dict):
    root = tmp_path / "root"
    isolated = root / "isolated"
    isolated.mkdir(parents=True)
    values = {
        "project_root": str(root),
        "isolated_cwd": isolated,
        "sandbox_mode": "workspace-write",
    }
    context = values if as_dict else SimpleNamespace(**values)
    assert get_effective_workdir(context) == str(isolated.resolve())
    assert check_file_write_access(context, "new.txt") is None
    assert "SANDBOX_VIOLATION" in check_file_write_access(context, "../outside.txt")


@pytest.mark.parametrize("invalid", [None, 0, object()])
def test_invalid_optional_path_falls_back_to_project_root(tmp_path, invalid):
    context = SimpleNamespace(project_root=Path(tmp_path), isolated_cwd=invalid)
    assert get_effective_workdir(context) == str(tmp_path.resolve())


def test_path_subclass_name_does_not_change_isolation(tmp_path):
    class MagicMockPath(str):
        pass

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    context = {
        "project_root": str(tmp_path),
        "isolated_cwd": MagicMockPath(str(isolated)),
        "sandbox_mode": "workspace-write",
    }
    assert get_effective_workdir(context) == str(isolated.resolve())
    assert "SANDBOX_VIOLATION" in check_file_write_access(context, "../outside")
