"""Tests for ProjectContextTool."""

import asyncio
import json
import pytest

from coderAI.tools.project import ProjectContextTool


@pytest.fixture
def python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "requirements.txt").write_text("requests>=2.0\nclick>=8.0\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\n")
    return tmp_path


@pytest.fixture
def node_project(tmp_path):
    pkg = {
        "name": "my-app",
        "version": "1.0.0",
        "scripts": {"build": "tsc", "test": "jest"},
        "dependencies": {"react": "^18.0.0"},
        "devDependencies": {"typescript": "^5.0.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "tsconfig.json").write_text("{}")
    return tmp_path


class TestProjectContextTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ProjectContextTool()

    def test_detects_python_project(self, python_project):
        result = asyncio.run(self.tool.execute(path=str(python_project)))
        assert result["success"]
        assert "python" in result["detected_types"]

    def test_detects_node_project(self, node_project):
        result = asyncio.run(self.tool.execute(path=str(node_project)))
        assert result["success"]
        assert "node" in result["detected_types"] or "typescript" in result["detected_types"]

    def test_returns_directory_structure(self, python_project):
        result = asyncio.run(self.tool.execute(path=str(python_project)))
        assert result["success"]
        assert isinstance(result["directory_structure"], list)
        assert len(result["directory_structure"]) > 0

    def test_loads_gitignore_patterns(self, python_project):
        result = asyncio.run(self.tool.execute(path=str(python_project)))
        assert result["success"]
        patterns = result["gitignore_patterns"]
        assert isinstance(patterns, list)
        assert any("pycache" in p or "*.pyc" in p for p in patterns)

    def test_loads_requirements(self, python_project):
        result = asyncio.run(self.tool.execute(path=str(python_project)))
        assert result["success"]
        ctx = result["context"].get("python", {})
        assert "dependencies" in ctx
        assert any("requests" in d for d in ctx["dependencies"])

    def test_loads_node_package_info(self, node_project):
        result = asyncio.run(self.tool.execute(path=str(node_project)))
        assert result["success"]
        ctx = result["context"].get("node", {})
        assert ctx.get("name") == "my-app"
        assert "react" in ctx.get("dependencies", [])

    def test_typescript_flag_set(self, node_project):
        result = asyncio.run(self.tool.execute(path=str(node_project)))
        assert result["success"]
        ctx = result["context"].get("node", {})
        assert ctx.get("has_typescript") is True

    def test_empty_dir_returns_success(self, tmp_path):
        result = asyncio.run(self.tool.execute(path=str(tmp_path)))
        assert result["success"]
        assert result["detected_types"] == []

    def test_invalid_path_returns_error(self):
        result = asyncio.run(self.tool.execute(path="/nonexistent/dir/xyz"))
        assert not result["success"]

    def test_file_path_returns_error(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        result = asyncio.run(self.tool.execute(path=str(f)))
        assert not result["success"]
