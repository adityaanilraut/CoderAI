"""Unit tests verifying the 5-tier hierarchical configuration cascade."""

from __future__ import annotations

import pathlib
import pytest

from coderai.config import (
    load_typed_config,
    resolve_hierarchical_config_path,
)


def test_cascade_cli_explicit_flag_takes_highest_precedence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Tier 1: Explicit CLI flag overrides env vars, local config, and global config."""
    cli_cfg = tmp_path / "cli.toml"
    cli_cfg.write_text(
        """
default_model = "cli-model"
[models.cli-model]
provider = "default"
model = "cli-model"
max_context_size = 100000
[providers.default]
type = "openai_legacy"
base_url = "https://api.cli.test/v1"
api_key = "cli-key"
"""
    )
    # Set env var that would otherwise take precedence
    monkeypatch.setenv("CODERAI_CONFIG_FILE", str(tmp_path / "env.toml"))

    resolved_path, is_default = resolve_hierarchical_config_path(cli_cfg, project_root=tmp_path)
    assert resolved_path == cli_cfg.resolve()
    assert is_default is False

    cfg = load_typed_config(cli_cfg, project_root=tmp_path)
    assert cfg.default_model == "cli-model"


def test_cascade_env_var_takes_precedence_over_local(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Tier 2: CODERAI_CONFIG_FILE overrides local and global configs."""
    env_cfg = tmp_path / "env_config.toml"
    env_cfg.write_text(
        """
default_model = "env-model"
[models.env-model]
provider = "default"
model = "env-model"
max_context_size = 120000
[providers.default]
type = "openai_legacy"
base_url = "https://api.env.test/v1"
api_key = "env-key"
"""
    )
    # Also create local .coderai/config.toml
    local_dir = tmp_path / ".coderai"
    local_dir.mkdir(parents=True)
    (local_dir / "config.toml").write_text('default_model = "local-model"')

    monkeypatch.setenv("CODERAI_CONFIG_FILE", str(env_cfg))
    resolved_path, is_default = resolve_hierarchical_config_path(project_root=tmp_path)
    assert resolved_path == env_cfg.resolve()
    assert is_default is False

    cfg = load_typed_config(project_root=tmp_path)
    assert cfg.default_model == "env-model"


def test_cascade_workspace_local_takes_precedence_and_merges_global(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Tier 3: Local .coderai/config.toml overrides global config and merges missing providers."""
    # Create global share config with providers
    share_dir = tmp_path / "share"
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(share_dir))
    monkeypatch.delenv("CODERAI_CONFIG_FILE", raising=False)

    global_cfg_file = share_dir / "config.toml"
    global_cfg_file.parent.mkdir(parents=True, exist_ok=True)
    global_cfg_file.write_text(
        """
default_model = "global-model"
[models.global-model]
provider = "shared-prov"
model = "global-model"
max_context_size = 64000
[providers.shared-prov]
type = "openai_legacy"
base_url = "https://shared.provider.test/v1"
api_key = "shared-key"
"""
    )

    # Create workspace local config in project_root
    project_root = tmp_path / "my_project"
    local_dir = project_root / ".coderai"
    local_dir.mkdir(parents=True)
    local_cfg = local_dir / "config.toml"
    local_cfg.write_text(
        """
default_model = "project-custom"
[models.project-custom]
provider = "shared-prov"
model = "project-custom"
max_context_size = 200000
"""
    )

    resolved_path, is_default = resolve_hierarchical_config_path(project_root=project_root)
    assert resolved_path == local_cfg.resolve()
    assert is_default is False

    cfg = load_typed_config(project_root=project_root)
    assert cfg.default_model == "project-custom"
    # Verify that shared-prov was cascaded/merged from the global config
    assert "shared-prov" in cfg.providers
    assert cfg.providers["shared-prov"].base_url == "https://shared.provider.test/v1"


def test_cascade_xdg_global_fallback(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Tier 4: ~/.config/coderai/config.toml is picked when no local config exists."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CODERAI_CONFIG_FILE", raising=False)

    xdg_cfg_dir = fake_home / ".config" / "coderai"
    xdg_cfg_dir.mkdir(parents=True)
    xdg_cfg = xdg_cfg_dir / "config.toml"
    xdg_cfg.write_text(
        """
default_model = "xdg-model"
[models.xdg-model]
provider = "default"
model = "xdg-model"
max_context_size = 80000
[providers.default]
type = "openai_legacy"
base_url = "https://xdg.test/v1"
api_key = "xdg-key"
"""
    )

    # Isolated workspace without local .coderai/config.toml
    empty_workspace = tmp_path / "workspace"
    empty_workspace.mkdir()

    resolved_path, is_default = resolve_hierarchical_config_path(project_root=empty_workspace)
    assert resolved_path == xdg_cfg.resolve()
    assert is_default is False

    cfg = load_typed_config(project_root=empty_workspace)
    assert cfg.default_model == "xdg-model"
