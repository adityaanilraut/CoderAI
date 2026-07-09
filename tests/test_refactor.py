"""Tests for RefactorTool — cross-file symbol renaming and reference finding."""

import asyncio
import sys
from pathlib import Path

import pytest

from coderAI.tools.refactor import RefactorTool


class TestRefactorToolProperties:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = RefactorTool()

    def test_tool_properties(self):
        assert self.tool.name == "refactor"
        assert self.tool.is_read_only is False
        assert self.tool.requires_confirmation is True

    def test_unknown_action_returns_error(self):
        result = asyncio.run(self.tool.execute(action="invalid_action_xyz", symbol="foo"))
        assert not result["success"]
        assert "Unknown action" in result["error"]

    def test_rename_without_new_name_returns_error(self):
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_func",
            )
        )
        assert not result["success"]
        assert result["error_code"] == "validation_error"

    def test_symbol_is_required_in_schema_and_runtime(self):
        schema = self.tool.get_schema()["function"]["parameters"]
        assert "symbol" in schema.get("required", [])

        result = asyncio.run(self.tool.execute(action="find_references"))
        assert not result["success"]
        assert result["error_code"] == "validation_error"

    def test_invalid_new_name_returns_validation_error(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("def old_name():\n    return old_name()\n")
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_name",
                new_name="not-valid",
                path=str(tmp_path),
            )
        )
        assert not result["success"]
        assert result["error_code"] == "validation_error"

    def test_path_outside_project_is_rejected_without_opt_out(self, tmp_path, monkeypatch):
        from coderAI.system.config import config_manager

        monkeypatch.delenv("CODERAI_ALLOW_OUTSIDE_PROJECT", raising=False)
        config_manager._config = None
        (tmp_path / "module.py").write_text("x = 1\n")

        result = asyncio.run(
            self.tool.execute(action="find_references", symbol="x", path=str(tmp_path))
        )

        assert not result["success"]
        assert result["error_code"] == "scope"

    def test_extract_to_module_returns_unsupported(self):
        # ``extract_to_module`` was never implemented; the refactor tool now
        # rejects it as an unknown action rather than advertising a stub.
        result = asyncio.run(
            self.tool.execute(
                action="extract_to_module",
                symbol="my_func",
            )
        )
        assert not result["success"]
        assert "Unknown action" in result["error"]
        assert "extract_to_module" in result["error"]


class TestRefactorPython:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = RefactorTool()

    def test_find_references_python_function(self, tmp_path):
        py_file = tmp_path / "example.py"
        py_file.write_text("""\
def greet(name):
    return f"Hello, {name}"

def main():
    greeting = greet("World")
    another = greet("Alice")
    print(greeting)
""")
        result = asyncio.run(
            self.tool.execute(
                action="find_references",
                symbol="greet",
                path=str(tmp_path),
                kind="any",
            )
        )
        assert result["success"]
        assert result["action"] == "find_references"
        assert result["total_references"] >= 3  # 1 def + 2 calls

    def test_find_references_python_class(self, tmp_path):
        py_file = tmp_path / "models.py"
        py_file.write_text("""\
class User:
    def __init__(self, name):
        self.name = name

class Admin(User):
    pass

def create_user():
    u = User("test")
    return u
""")
        result = asyncio.run(
            self.tool.execute(
                action="find_references",
                symbol="User",
                path=str(tmp_path),
                kind="class",
            )
        )
        assert result["success"]
        assert result["total_references"] >= 2  # class def + usage + inheritance

    def test_find_references_nonexistent_symbol(self, tmp_path):
        py_file = tmp_path / "empty.py"
        py_file.write_text("x = 1\n")
        result = asyncio.run(
            self.tool.execute(
                action="find_references",
                symbol="nonexistent_func_xyz",
                path=str(tmp_path),
            )
        )
        assert result["success"]
        assert result["total_references"] == 0

    def test_rename_python_function_dry_run(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("""\
def old_name():
    return 42

def test_it():
    assert old_name() == 42
""")
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_name",
                new_name="new_name",
                path=str(tmp_path),
                dry_run=True,
            )
        )
        assert result["success"]
        assert result["dry_run"] is True
        assert result["total_changes"] >= 1

    def test_rename_python_function_apply(self, tmp_path):
        py_file = tmp_path / "module.py"
        content = """\
def old_name():
    return 42

def test_it():
    assert old_name() == 42
"""
        py_file.write_text(content)
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_name",
                new_name="new_name",
                path=str(tmp_path),
                dry_run=False,
            )
        )
        assert result["success"]
        assert result["dry_run"] is False
        assert result["files_modified"] >= 1

        new_content = py_file.read_text()
        assert "new_name" in new_content
        assert "old_name" not in new_content

    def test_rename_python_class(self, tmp_path):
        py_file = tmp_path / "models.py"
        content = """\
class OldWidget:
    def render(self):
        return "widget"

def make_widget():
    return OldWidget()
"""
        py_file.write_text(content)
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="OldWidget",
                new_name="NewWidget",
                path=str(tmp_path),
                dry_run=False,
            )
        )
        assert result["success"]
        new_content = py_file.read_text()
        assert "NewWidget" in new_content
        assert "OldWidget" not in new_content

    def test_rename_python_attribute_access(self, tmp_path):
        py_file = tmp_path / "models.py"
        py_file.write_text("""\
class User:
    def __init__(self):
        self.old_attr = 1

def read(user):
    return user.old_attr
""")
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_attr",
                new_name="new_attr",
                path=str(tmp_path),
                dry_run=False,
            )
        )
        assert result["success"]
        new_content = py_file.read_text()
        assert "self.new_attr = 1" in new_content
        assert "user.new_attr" in new_content
        assert "old_attr" not in new_content


class TestRefactorJavaScript:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = RefactorTool()

    def test_find_references_js_function(self, tmp_path):
        js_file = tmp_path / "utils.js"
        js_file.write_text("""\
function greet(name) {
  return `Hello, ${name}`;
}

const result = greet("World");
const another = greet("Alice");
""")
        result = asyncio.run(
            self.tool.execute(
                action="find_references",
                symbol="greet",
                path=str(tmp_path),
                kind="any",
            )
        )
        assert result["success"]
        assert result["action"] == "find_references"
        assert result["total_references"] >= 1

    def test_rename_js_function(self, tmp_path):
        js_file = tmp_path / "utils.js"
        content = """\
function oldFunc() {
  return "hello";
}

function main() {
  console.log(oldFunc());
}
"""
        js_file.write_text(content)
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="oldFunc",
                new_name="newFunc",
                path=str(tmp_path),
                dry_run=False,
            )
        )
        assert result["success"]
        new_content = js_file.read_text()
        assert "newFunc" in new_content
        assert "oldFunc" not in new_content

    def test_rename_js_ignores_strings_and_comments(self, tmp_path):
        js_file = tmp_path / "utils.js"
        js_file.write_text("""\
// oldFunc should stay in this comment
const label = "oldFunc should stay in this string";
function oldFunc() {
  return oldFunc();
}
""")
        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="oldFunc",
                new_name="newFunc",
                path=str(tmp_path),
                dry_run=False,
            )
        )
        assert result["success"]
        new_content = js_file.read_text()
        assert "// oldFunc should stay in this comment" in new_content
        assert '"oldFunc should stay in this string"' in new_content
        assert "function newFunc()" in new_content
        assert "return newFunc()" in new_content

    def test_find_references_no_matches(self, tmp_path):
        js_file = tmp_path / "empty.js"
        js_file.write_text("const x = 1;\n")
        result = asyncio.run(
            self.tool.execute(
                action="find_references",
                symbol="nonexistent_func_xyz",
                path=str(tmp_path),
            )
        )
        assert result["success"]
        assert result["total_references"] == 0


class TestRefactorWriteGuards:
    """Rename delegates writes to WriteFileTool, so its guards apply per file."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = RefactorTool()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
    def test_rename_skips_symlink_leaf(self, tmp_path):
        # The real file lives outside the search base; only a symlink to it is
        # inside. WriteFileTool must refuse to write through the symlink, and the
        # refactor tool records it in files_skipped instead of clobbering it.
        real = tmp_path / "real.py"
        real.write_text("def old_name():\n    return old_name()\n")
        base = tmp_path / "src"
        base.mkdir()
        (base / "alias.py").symlink_to(real)

        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_name",
                new_name="new_name",
                path=str(base),
                dry_run=False,
            )
        )
        assert result["success"] is True, result
        assert result["files_modified"] == 0
        skipped = result.get("files_skipped")
        assert skipped and skipped[0]["error_code"] == "symlink", result
        # The real file behind the symlink is untouched.
        assert "old_name" in real.read_text()

    def test_rename_skips_protected_path(self, tmp_path, monkeypatch):
        # A source file living under a protected home dir (~/.config) must not be
        # rewritten; WriteFileTool refuses and it is recorded as skipped.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        base = tmp_path / ".config" / "proj"
        base.mkdir(parents=True)
        src = base / "module.py"
        src.write_text("def old_name():\n    return old_name()\n")

        result = asyncio.run(
            self.tool.execute(
                action="rename_symbol",
                symbol="old_name",
                new_name="new_name",
                path=str(base),
                dry_run=False,
            )
        )
        assert result["success"] is True, result
        assert result["files_modified"] == 0
        skipped = result.get("files_skipped")
        assert skipped and skipped[0]["error_code"] == "permission_denied", result
        # The protected file is unchanged.
        assert "old_name" in src.read_text()
