"""Tests for PackageManagerTool and detect_package_manager."""

import asyncio
import shutil
import pytest

from coderAI.tools.package_manager import (
    PackageManagerTool,
    detect_package_manager,
    PACKAGE_MANAGERS,
    _validate_package_name,
)


class TestDetectPackageManager:
    def test_detects_pip_for_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
        result = detect_package_manager(str(tmp_path))
        # pip or pip3 depending on environment
        assert result in ("pip", "pip3", None)

    def test_returns_none_for_empty_dir(self, tmp_path):
        result = detect_package_manager(str(tmp_path))
        assert result is None

    def test_all_registered_managers_have_required_keys(self):
        required = {"cmd", "install_cmd", "uninstall_cmd", "list_cmd", "detect_files", "timeout"}
        for name, config in PACKAGE_MANAGERS.items():
            missing = required - set(config.keys())
            assert not missing, f"Manager {name} missing keys: {missing}"

    def test_detects_npm_for_node_project(self, tmp_path):
        if shutil.which("npm"):
            (tmp_path / "package.json").write_text('{"name":"test"}')
            result = detect_package_manager(str(tmp_path))
            assert result in ("npm", "yarn", "pnpm", "bun", None)

    def test_detects_cargo_for_rust_project(self, tmp_path):
        if shutil.which("cargo"):
            (tmp_path / "Cargo.toml").write_text("[package]\nname = \"test\"\n")
            result = detect_package_manager(str(tmp_path))
            assert result == "cargo"


class TestValidatePackageName:
    def test_valid_package_names(self):
        assert _validate_package_name("requests", "pip") is None
        assert _validate_package_name("lodash", "npm") is None
        assert _validate_package_name("serde", "cargo") is None
        assert _validate_package_name("test-pkg", "npm") is None
        assert _validate_package_name("@scope/package", "npm") is None

    def test_empty_package_name(self):
        assert _validate_package_name("", "pip") is not None
        assert _validate_package_name("   ", "pip") is not None

    def test_dangerous_characters(self):
        assert _validate_package_name("pkg; rm -rf /", "pip") is not None
        assert _validate_package_name("pkg | cat", "pip") is not None
        assert _validate_package_name("pkg$HOME", "npm") is not None
        assert _validate_package_name("pkg`ls`", "npm") is not None
        assert _validate_package_name("pkg\nfoo", "pip") is not None

    def test_too_long_package_name(self):
        assert _validate_package_name("a" * 300, "pip") is not None


class TestPackageManagerTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = PackageManagerTool()

    def test_tool_properties(self):
        assert self.tool.name == "package_manager"
        assert self.tool.is_read_only is False
        assert self.tool.requires_confirmation is True

    def test_unknown_manager_returns_error(self):
        result = asyncio.run(
            self.tool.execute(
                action="list",
                manager="nonexistent_manager_xyz",
            )
        )
        assert not result["success"]
        assert "Unknown package manager" in result["error"]

    def test_invalid_action_returns_error(self):
        result = asyncio.run(
            self.tool.execute(action="invalid_action_xyz")
        )
        assert not result["success"]
        assert "Unknown action" in result["error"]

    def test_install_without_package_returns_error(self):
        result = asyncio.run(
            self.tool.execute(action="install")
        )
        assert not result["success"]

    def test_uninstall_without_package_returns_error(self):
        result = asyncio.run(
            self.tool.execute(action="uninstall")
        )
        assert not result["success"]

    def test_dangerous_package_name_rejected(self):
        result = asyncio.run(
            self.tool.execute(
                action="install",
                package="safe; rm -rf /",
                manager="pip",
            )
        )
        assert not result["success"]
        assert "unsafe" in result["error"].lower() or ";" in result["error"]

    def test_pip_list_succeeds(self, tmp_path):
        if not shutil.which("pip") and not shutil.which("pip3"):
            pytest.skip("pip not installed")

        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")

        result = asyncio.run(
            self.tool.execute(
                action="list",
            )
        )
        # list should succeed even in a temp dir
        assert result["success"]
        assert result["manager"] in ("pip", "pip3")

    def test_npm_list_succeeds(self, tmp_path):
        if not shutil.which("npm"):
            pytest.skip("npm not installed")

        (tmp_path / "package.json").write_text('{"name":"test"}')

        result = asyncio.run(
            self.tool.execute(
                action="list",
            )
        )
        assert result["success"]
        assert result["manager"] in ("npm", "yarn", "pnpm", "bun", "pip", "pip3", None)
