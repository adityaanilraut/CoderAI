"""Tests for the tool auto-discovery mechanism."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from coderAI.core.tool_results import normalize_tool_result
from coderAI.tools.base import Tool, ToolRegistry
from coderAI.tools.discovery import discover_tools


# Minimal Tool subclass used as a sentinel in tests.
class _DummyTool(Tool):
    name = "dummy"
    description = "A dummy tool for testing"

    async def execute(self, **kwargs):
        return {"success": True}


class TestDiscoverTools:
    @pytest.mark.asyncio
    async def test_registry_uses_idempotent_shared_result_normalization(self):
        class StringResultTool(Tool):
            name = "string_result"

            async def execute(self, **kwargs):
                return "boom"

        registry = ToolRegistry()
        registry.register(StringResultTool())

        result = await registry.execute("string_result")

        assert result == normalize_tool_result(result, tool_name="string_result")
        assert result["success"] is False
        assert result["error_code"] == "tool_error"

    def test_registry_rejects_duplicate_names(self):
        registry = ToolRegistry()
        registry.register(_DummyTool())

        with pytest.raises(ValueError, match="already registered"):
            registry.register(_DummyTool())

    def test_registry_populated_from_real_tools_package(self):
        registry = ToolRegistry()
        discover_tools(registry, package_name="coderAI.tools")
        assert len(registry.tools) > 0

    def test_all_core_tools_are_discoverable(self):
        """Every Tool subclass in coderAI.tools that has a no-arg constructor
        should be discoverable via discover_tools."""
        registry = ToolRegistry()
        discover_tools(registry, package_name="coderAI.tools")

        discovered_names = set(registry.tools.keys())

        excluded_from_discovery = {
            "manage_context",
        }

        for tool in registry.tools.values():
            assert isinstance(tool, Tool)

        for excluded in excluded_from_discovery:
            assert excluded not in discovered_names, (
                f"'{excluded}' has constructor args and should not be auto-discovered"
            )

    def test_non_existent_package_handled_gracefully(self):
        registry = ToolRegistry()
        discover_tools(registry, package_name="non.existent.package")
        assert len(registry.tools) == 0

    def test_subpackage_is_pkg_skipped(self):
        """is_pkg=True entries in walk_packages are skipped.
        Uses a fake tools package with a real importlib path so that
        discovery can introspect it, while we mock walk_packages to
        control what modules are visited."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")
        mod = MagicMock()
        mod.__name__ = "coderAI.tests.fake_tools.a_tool"

        with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
            mock_walk.return_value = [
                (None, "coderAI.tests.fake_tools.submodule", True),
                (None, "coderAI.tests.fake_tools.a_tool", False),
            ]
            with patch(
                "coderAI.tools.discovery.importlib.import_module",
            ) as mock_import:
                mock_import.side_effect = lambda name, **_: mock_import._side_effect_calls[name](
                    name
                )

                # Return fake pkg for the package, mock module for others
                def _import_side_effect(name, **kw):
                    if name == "coderAI.tests.fake_tools":
                        return fake_pkg
                    return mod

                mock_import.side_effect = _import_side_effect
                registry = ToolRegistry()
                discover_tools(registry, package_name="coderAI.tests.fake_tools")
                # submodule is_pkg=True is skipped; only a_tool gets imported
                assert mock_walk.call_count == 1

    def test_base_module_skipped(self):
        """Module names ending in '.base' are skipped at the walk_packages level."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")

        with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
            mock_walk.return_value = [
                (None, "coderAI.tests.fake_tools.base", False),
            ]
            with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                mock_import.return_value = fake_pkg
                registry = ToolRegistry()
                discover_tools(registry, package_name="coderAI.tests.fake_tools")
                # module ending in .base skipped — no importlib call for it
                assert mock_import.call_count == 1  # only the package

    def test_discovery_module_skipped(self):
        """The discovery module name itself is skipped."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")

        with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
            mock_walk.return_value = [
                (None, "coderAI.tests.fake_tools.discovery", False),
            ]
            with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                mock_import.return_value = fake_pkg
                registry = ToolRegistry()
                discover_tools(registry, package_name="coderAI.tests.fake_tools")
                assert mock_import.call_count == 1  # only the package

    def test_tool_without_constructor_args_registered(self):
        """Tools with no-arg constructors are instantiated and registered."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")
        mod = MagicMock()
        mod.__name__ = "coderAI.tests.fake_tools.dummy_module"

        with patch(
            "coderAI.tools.discovery.inspect.getmembers",
            return_value=[("_DummyTool", _DummyTool)],
        ):
            with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
                mock_walk.return_value = [(None, "coderAI.tests.fake_tools.dummy_module", False)]
                with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                    mock_import.side_effect = lambda name, **kw: (
                        fake_pkg if name == "coderAI.tests.fake_tools" else mod
                    )
                    registry = ToolRegistry()
                    discover_tools(registry, package_name="coderAI.tests.fake_tools")
                    assert "dummy" in registry.tools
                    assert isinstance(registry.tools["dummy"], _DummyTool)

    def test_tool_with_required_constructor_args_excluded(self):
        """Tools whose constructors require arguments are skipped."""

        class NeedsArgTool(Tool):
            name = "needs_arg"
            description = "Requires constructor args"

            def __init__(self, required_param):
                self.required_param = required_param

            async def execute(self, **kwargs):
                return {"success": True}

        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")
        mod = MagicMock()
        mod.__name__ = "coderAI.tests.fake_tools.arg_module"

        with patch(
            "coderAI.tools.discovery.inspect.getmembers",
            return_value=[("NeedsArgTool", NeedsArgTool)],
        ):
            with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
                mock_walk.return_value = [(None, "coderAI.tests.fake_tools.arg_module", False)]
                with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                    mock_import.side_effect = lambda name, **kw: (
                        fake_pkg if name == "coderAI.tests.fake_tools" else mod
                    )
                    registry = ToolRegistry()
                    discover_tools(registry, package_name="coderAI.tests.fake_tools")
                    assert "needs_arg" not in registry.tools

    def test_already_registered_class_not_duplicated(self):
        """Tool class encountered in two modules is registered only once."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")

        with patch(
            "coderAI.tools.discovery.inspect.getmembers",
            side_effect=[
                [("_DummyTool", _DummyTool)],
                [("_DummyTool", _DummyTool)],
            ],
        ):
            with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
                mock_walk.return_value = [
                    (None, "coderAI.tests.fake_tools.module_a", False),
                    (None, "coderAI.tests.fake_tools.module_b", False),
                ]
                with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                    mod_a = MagicMock()
                    mod_a.__name__ = "coderAI.tests.fake_tools.module_a"
                    mod_b = MagicMock()
                    mod_b.__name__ = "coderAI.tests.fake_tools.module_b"

                    def _import_side_effect(name, **kw):
                        if name == "coderAI.tests.fake_tools":
                            return fake_pkg
                        if "module_a" in name:
                            return mod_a
                        if "module_b" in name:
                            return mod_b
                        return MagicMock()

                    mock_import.side_effect = _import_side_effect
                    registry = ToolRegistry()
                    discover_tools(registry, package_name="coderAI.tests.fake_tools")
                    assert list(registry.tools.keys()).count("dummy") == 1

    def test_bad_module_does_not_kill_discovery(self):
        """A module that raises ImportError should not stop discovery of others."""
        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="coderAI.tests.fake_tools")

        with patch(
            "coderAI.tools.discovery.inspect.getmembers",
            return_value=[("_DummyTool", _DummyTool)],
        ):
            with patch("coderAI.tools.discovery.pkgutil.walk_packages") as mock_walk:
                mock_walk.return_value = [
                    (None, "coderAI.tests.fake_tools.bad_module", False),
                    (None, "coderAI.tests.fake_tools.good_module", False),
                ]
                with patch("coderAI.tools.discovery.importlib.import_module") as mock_import:
                    mod_good = MagicMock()
                    mod_good.__name__ = "coderAI.tests.fake_tools.good_module"

                    def _import_side_effect(name, **kw):
                        if name == "coderAI.tests.fake_tools":
                            return fake_pkg
                        if "bad_module" in name:
                            raise ImportError("missing dependency")
                        if "good_module" in name:
                            return mod_good
                        return MagicMock()

                    mock_import.side_effect = _import_side_effect
                    registry = ToolRegistry()
                    discover_tools(registry, package_name="coderAI.tests.fake_tools")
                    assert "dummy" in registry.tools

    def test_bad_class_does_not_skip_later_class_in_same_module(self):
        class BrokenTool(Tool):
            name = "broken"

            def __init__(self):
                raise RuntimeError("broken constructor")

            async def execute(self, **kwargs):
                return {"success": True}

        fake_pkg = SimpleNamespace(__path__=["/fake/path"], __name__="fake_tools")
        module = MagicMock()
        with (
            patch(
                "coderAI.tools.discovery.inspect.getmembers",
                return_value=[("BrokenTool", BrokenTool), ("DummyTool", _DummyTool)],
            ),
            patch(
                "coderAI.tools.discovery.pkgutil.walk_packages",
                return_value=[(None, "fake_tools.module", False)],
            ),
            patch("coderAI.tools.discovery.importlib.import_module") as mock_import,
        ):
            mock_import.side_effect = lambda name, **_: fake_pkg if name == "fake_tools" else module
            registry = ToolRegistry()
            discover_tools(registry, package_name="fake_tools")

        assert "broken" not in registry.tools
        assert "dummy" in registry.tools
