"""Project context auto-loading tool."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# Known project indicators and their types
PROJECT_INDICATORS = {
    "package.json": "node",
    "tsconfig.json": "typescript",
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "mix.exs": "elixir",
    "CMakeLists.txt": "cpp",
    "Makefile": "makefile",
    "docker-compose.yml": "docker",
    "Dockerfile": "docker",
    ".flake8": "python",
    ".eslintrc.json": "node",
    ".prettierrc": "node",
}


class ProjectContextParams(BaseModel):
    path: str = Field(".", description="Project root directory (default: current directory)")


class ProjectContextTool(Tool):
    """Tool for auto-detecting and loading project context."""

    name = "project_context"
    description = (
        "Auto-detect project type and load relevant context "
        "(config files, directory structure, dependencies)"
    )
    parameters_model = ProjectContextParams
    is_read_only = True

    async def execute(self, path: str = ".") -> Dict[str, Any]:
        """Detect project type and load context."""
        try:
            project_root = Path(path).expanduser().resolve()
            if not project_root.is_dir():
                return {"success": False, "error": f"Not a directory: {path}"}

            # Detect project type(s)
            detected_types = set()
            detected_files = []

            for name, proj_type in PROJECT_INDICATORS.items():
                indicator = project_root / name
                if indicator.exists():
                    detected_types.add(proj_type)
                    detected_files.append(name)

            # Load key context files based on detected type
            context = {}

            if "python" in detected_types:
                context["python"] = await self._load_python_context(project_root)

            if "node" in detected_types or "typescript" in detected_types:
                context["node"] = await self._load_node_context(project_root)

            if "rust" in detected_types:
                context["rust"] = await self._load_rust_context(project_root)

            if "go" in detected_types:
                context["go"] = await self._load_go_context(project_root)

            # Get directory structure (top 2 levels, skip common ignore dirs)
            structure = self._get_directory_structure(project_root, max_depth=2)

            # Detect .gitignore patterns
            gitignore = self._load_gitignore(project_root)

            return {
                "success": True,
                "project_root": str(project_root),
                "detected_types": list(detected_types),
                "detected_files": detected_files,
                "context": context,
                "directory_structure": structure,
                "gitignore_patterns": gitignore,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _load_python_context(self, root: Path) -> Dict[str, Any]:
        """Load Python project context."""
        ctx = {}

        # pyproject.toml
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            ctx["pyproject_toml"] = pyproject.read_text(errors="replace")[:2000]

        # requirements.txt
        requirements = root / "requirements.txt"
        if requirements.exists():
            ctx["dependencies"] = [
                line.strip()
                for line in requirements.read_text(errors="replace").split("\n")
                if line.strip() and not line.startswith("#")
            ]

        # setup.py (first 50 lines)
        setup = root / "setup.py"
        if setup.exists():
            lines = setup.read_text(errors="replace").split("\n")[:50]
            ctx["setup_py_head"] = "\n".join(lines)

        return ctx

    async def _load_node_context(self, root: Path) -> Dict[str, Any]:
        """Load Node.js/TypeScript project context."""
        ctx = {}

        package_json = root / "package.json"
        if package_json.exists():
            try:
                pkg = json.loads(package_json.read_text())
                ctx["name"] = pkg.get("name", "")
                ctx["version"] = pkg.get("version", "")
                ctx["scripts"] = pkg.get("scripts", {})
                ctx["dependencies"] = list(pkg.get("dependencies", {}).keys())
                ctx["devDependencies"] = list(pkg.get("devDependencies", {}).keys())
            except json.JSONDecodeError:
                ctx["package_json_error"] = "Failed to parse package.json"

        tsconfig = root / "tsconfig.json"
        if tsconfig.exists():
            ctx["has_typescript"] = True

        return ctx

    async def _load_rust_context(self, root: Path) -> Dict[str, Any]:
        """Load Rust project context."""
        ctx = {}
        cargo = root / "Cargo.toml"
        if cargo.exists():
            ctx["cargo_toml"] = cargo.read_text(errors="replace")[:2000]
        return ctx

    async def _load_go_context(self, root: Path) -> Dict[str, Any]:
        """Load Go project context."""
        ctx = {}
        gomod = root / "go.mod"
        if gomod.exists():
            ctx["go_mod"] = gomod.read_text(errors="replace")[:2000]
        return ctx

    def _get_directory_structure(
        self, root: Path, max_depth: int = 2, current_depth: int = 0
    ) -> List[str]:
        """Get directory structure up to max_depth levels."""
        IGNORE_DIRS = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
            ".next", ".nuxt", "target", ".idea", ".vscode",
        }

        entries = []
        if current_depth >= max_depth:
            return entries

        try:
            for item in sorted(root.iterdir()):
                if item.name.startswith(".") and item.name not in (".github", ".gitignore"):
                    continue
                if item.name in IGNORE_DIRS:
                    continue

                indent = "  " * current_depth
                if item.is_dir():
                    entries.append(f"{indent}{item.name}/")
                    entries.extend(
                        self._get_directory_structure(item, max_depth, current_depth + 1)
                    )
                else:
                    entries.append(f"{indent}{item.name}")
        except PermissionError:
            pass

        return entries

    def _load_gitignore(self, root: Path) -> List[str]:
        """Load .gitignore patterns."""
        gitignore = root / ".gitignore"
        if not gitignore.exists():
            return []

        patterns = []
        for line in gitignore.read_text(errors="replace").split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns[:30]  # Limit to 30 patterns
