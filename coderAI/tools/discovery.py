"""Tool discovery mechanism for CoderAI."""

import importlib
import inspect
import logging
import pkgutil
from typing import Set, Type

from coderAI.tools.base import Tool, ToolRegistry

logger = logging.getLogger(__name__)


def discover_tools(registry: ToolRegistry, package_name: str = "coderAI.tools") -> None:
    """Dynamically discover and register all Tool subclasses in the given package.

    This function scans the specified package for any subclasses of Tool and
    attempts to instantiate and register them. Tools that require initialization
    arguments (like ManageContextTool) are skipped and should be registered
    manually in the Agent.
    """
    try:
        pkg = importlib.import_module(package_name)
    except ImportError as e:
        logger.error(f"Could not import package {package_name}: {e}")
        return

    # Keep track of what we've registered to avoid duplicates if multiple
    # paths lead to the same class.
    registered_classes: Set[Type[Tool]] = set()

    for loader, module_name, is_pkg in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        # git_extended tools are served only via the bundled MCP server — skip
        # native auto-registration so they don't inflate the default tool list.
        if (
            is_pkg
            or module_name.endswith(".base")
            or module_name.endswith(".discovery")
            or module_name.endswith(".git_extended")
        ):
            continue

        try:
            module = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Tool)
                    and obj is not Tool
                    and obj not in registered_classes
                ):
                    try:
                        # Attempt to instantiate with no arguments.
                        # This works for 95% of our tools.
                        tool_instance = obj()
                        registry.register(tool_instance)
                        registered_classes.add(obj)
                        logger.debug(f"Registered tool: {tool_instance.name} from {module_name}")
                    except TypeError as e:
                        # Constructor argument errors are expected for tools
                        # registered manually; other TypeErrors are still
                        # isolated to this class rather than aborting the module.
                        logger.warning(
                            "Skipping auto-registration for %s.%s: %s",
                            module_name,
                            name,
                            e,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to register tool class %s.%s: %s",
                            module_name,
                            name,
                            e,
                        )
        except (ImportError, TypeError, ValueError, AttributeError) as e:
            # Don't let one bad module kill the whole discovery.
            logger.warning(f"Failed to load tools from {module_name}: {e}")
