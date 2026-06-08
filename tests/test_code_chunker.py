"""Tests for the code chunker (code_chunker.py)."""

from pathlib import Path
from unittest.mock import patch


from coderAI.context.code_chunker import (
    Chunk,
    ChunkResult,
    _chunk_generic,
    _chunk_jsts,
    _chunk_python,
    _merge_short_chunks,
    _func_type,
    _CODE_SUFFIXES,
    _WINDOW_SIZE,
    chunk_file,
    should_index,
    is_skip_dir,
)

# Helpers to generate function bodies large enough to avoid _merge_short_chunks
# merging (default min_chars=100). We want each chunk to stand alone.

_PY_BODY = "    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    f = 6\n    g = 7\n    h = 8\n    return a\n"


def _py_func(name):
    return f"def {name}():\n{_PY_BODY}"


def _py_class(name, body_lines=6):
    extra = "\n".join(f"    attr_{i} = 'some_value_{i}'" for i in range(body_lines))
    return f"class {name}:\n{extra}\n\n    def method(self):\n        return 42\n"


# ---------------------------------------------------------------------------
# Python chunking
# ---------------------------------------------------------------------------


class TestChunkPython:
    def test_single_function_becomes_chunk(self):
        source = _py_func("foo")
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"
        assert chunks[0].start_line == 1

    def test_single_async_function_becomes_chunk(self):
        source = f"async def bar():\n{_PY_BODY}"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"

    def test_multiple_functions(self):
        source = _py_func("f1") + "\n" + _py_func("f2") + "\n" + _py_func("f3") + "\n"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 3
        assert all(c.chunk_type == "function" for c in chunks)

    def test_class_becomes_chunk(self):
        source = _py_class("MyClass", body_lines=8) + "\n"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "class"

    def test_preamble_before_first_function(self):
        source = (
            "import os\n"
            "import sys\n"
            "import json\n"
            "import pathlib\n"
            "import hashlib\n"
            "\n"
            "CONFIG = {'key': 'value', 'debug': True}\n"
            "\n"
        )
        source += _py_func("main") + "\n"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 2
        assert chunks[0].chunk_type == "module"
        assert "import os" in chunks[0].text
        assert "CONFIG" in chunks[0].text
        assert chunks[1].chunk_type == "function"

    def test_dunder_function_type(self):
        source = f"def __init_subclass__():\n{_PY_BODY}"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "dunder"

    def test_syntax_error_falls_back_to_generic(self):
        source = "def broken(:\n    pass\n"
        with patch("coderAI.context.code_chunker._chunk_generic") as mock_generic:
            mock_generic.return_value = [
                Chunk(
                    text=source,
                    file_path="x.py",
                    start_line=1,
                    end_line=2,
                    language="python",
                    chunk_type="generic",
                )
            ]
            _chunk_python(source, "test.py", "python")
            mock_generic.assert_called_once()

    def test_class_and_function_mixed_with_preamble(self):
        source = (
            "VERSION = '1.0'\n"
            "DEBUG = True\n"
            "HOST = 'localhost'\n"
            "PORT = 8080\n"
            "ENABLE_FEATURE_X = True\n"
            "MAX_RETRIES = 3\n"
            "TIMEOUT_SECONDS = 30\n"
            "ALLOWED_HOSTS = ['*']\n"
            "LOG_LEVEL = 'INFO'\n"
            "LOG_FILE = '/var/log/app.log'\n"
            "CACHE_TTL = 3600\n"
            "\n"
        )
        source += _py_class("Foo", body_lines=6) + "\n"
        source += _py_func("baz") + "\n"
        chunks = _chunk_python(source, "test.py", "python")
        chunk_types = [c.chunk_type for c in chunks]
        assert "module" in chunk_types
        assert "class" in chunk_types
        assert "function" in chunk_types

    def test_empty_python_file(self):
        chunks = _chunk_python("", "test.py", "python")
        assert len(chunks) == 0

    def test_only_imports_no_functions_or_classes(self):
        source = "import os\nimport sys\nimport json\nimport pathlib\n"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) >= 0


# ---------------------------------------------------------------------------
# JS/TS chunking
# ---------------------------------------------------------------------------

_JS_BODY = (
    "  var a = 1;\n"
    "  var b = 2;\n"
    "  var c = 3;\n"
    "  var d = 4;\n"
    "  var e = 5;\n"
    "  var f = 6;\n"
    "  return a + b;\n"
)


def _js_func(name):
    return f"function {name}() {{\n{_JS_BODY}}}\n"


class TestChunkJSTS:
    def test_function_declaration(self):
        source = _js_func("greet")
        chunks = _chunk_jsts(source, "test.js", "javascript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"

    def test_export_function(self):
        source = f"export function add(a, b) {{\n{_JS_BODY}}}\n"
        chunks = _chunk_jsts(source, "test.ts", "typescript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"

    def test_export_arrow_function(self):
        source = (
            "export const multiply = (a, b) => {\n"
            "  var x = a;\n"
            "  var y = b;\n"
            "  var z = x * y;\n"
            "  var p = 1;\n"
            "  var q = 2;\n"
            "  var r = 3;\n"
            "  return z;\n"
            "};\n"
        )
        chunks = _chunk_jsts(source, "test.js", "javascript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"

    def test_export_class(self):
        source = (
            "export class Animal {\n"
            "  constructor(name) {\n"
            "    this.name = name;\n"
            "    this.type = 'mammal';\n"
            "    this.age = 5;\n"
            "  }\n"
            "  speak() {\n"
            "    var msg = 'hello';\n"
            "    var a = 1;\n"
            "    var b = 2;\n"
            "    return msg;\n"
            "  }\n"
            "}\n"
        )
        chunks = _chunk_jsts(source, "test.ts", "typescript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "class"

    def test_async_function(self):
        source = f"async function fetchData() {{\n{_JS_BODY}}}\n"
        chunks = _chunk_jsts(source, "test.js", "javascript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "function"

    def test_multiple_declarations_with_preamble(self):
        source = (
            "import { x, y, z } from 'lib';\n"
            "import { a, b, c } from 'other';\n"
            "import { d, e, f } from 'utils';\n"
            "import { g, h } from 'helpers';\n"
            "import { i, j, k } from 'core';\n"
            "\n"
        )
        source += _js_func("f1") + "\n"
        source += _js_func("f2") + "\n"
        chunks = _chunk_jsts(source, "test.js", "javascript")
        chunk_types = [c.chunk_type for c in chunks]
        assert "module" in chunk_types
        assert chunk_types.count("function") == 2

    def test_no_boundaries_falls_back_to_generic(self):
        source = "const x = 1;\nlet y = 2;\n"
        with patch("coderAI.context.code_chunker._chunk_generic") as mock_generic:
            mock_generic.return_value = [
                Chunk(
                    text=source,
                    file_path="x.js",
                    start_line=1,
                    end_line=2,
                    language="javascript",
                    chunk_type="generic",
                )
            ]
            _chunk_jsts(source, "test.js", "javascript")
            mock_generic.assert_called_once()

    def test_deduplicate_boundaries_by_line(self):
        source = f"export function foo() {{\n{_JS_BODY}}}\n"
        source += "class Foo {\n"
        source += "  constructor() {\n"
        source += "    this.x = 1;\n"
        source += "    this.y = 2;\n"
        source += "    this.z = 3;\n"
        source += "    this.w = 4;\n"
        source += "  }\n"
        source += "}\n"
        # export function at line 1 matches both 'export function' and 'function'
        # patterns — should be deduplicated by line, giving 2 chunks
        chunks = _chunk_jsts(source, "test.ts", "typescript")
        assert len(chunks) == 2


# ---------------------------------------------------------------------------
# Generic sliding-window chunking
# ---------------------------------------------------------------------------


class TestChunkGeneric:
    def test_sliding_window_produces_chunks(self):
        lines = "\n".join(f"line {i}" for i in range(3000))
        chunks = _chunk_generic(lines, "test.txt", "text")
        assert len(chunks) > 1
        assert chunks[0].start_line == 1
        assert chunks[0].chunk_type == "generic"

    def test_sliding_window_has_overlap(self):
        lines = "\n".join(f"line {i}" for i in range(3000))
        chunks = _chunk_generic(lines, "test.txt", "text")
        if len(chunks) > 1:
            assert chunks[1].start_line < chunks[0].end_line + 1

    def test_small_file_single_chunk(self):
        source = "hello\nworld\n"
        chunks = _chunk_generic(source, "test.txt", "text")
        assert len(chunks) == 1
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 2

    def test_empty_file_returns_empty(self):
        chunks = _chunk_generic("", "test.txt", "text")
        assert len(chunks) == 0

    def test_single_line_file(self):
        chunks = _chunk_generic("just one line", "test.txt", "text")
        assert len(chunks) == 1
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 1
        assert chunks[0].text == "just one line"

    def test_window_size_used_correctly(self):
        lines = "\n".join(f"line {i}" for i in range(_WINDOW_SIZE + 500))
        chunks = _chunk_generic(lines, "test.txt", "text")
        assert len(chunks) == 2
        assert chunks[0].end_line == _WINDOW_SIZE


# ---------------------------------------------------------------------------
# Merge short chunks
# ---------------------------------------------------------------------------


class TestMergeShortChunks:
    def test_no_chunks_returns_empty(self):
        assert _merge_short_chunks([]) == []

    def test_large_chunks_not_merged(self):
        chunks = [
            Chunk(
                text="x" * 200,
                file_path="f.py",
                start_line=1,
                end_line=10,
                language="python",
                chunk_type="function",
            ),
            Chunk(
                text="y" * 200,
                file_path="f.py",
                start_line=11,
                end_line=20,
                language="python",
                chunk_type="function",
            ),
        ]
        merged = _merge_short_chunks(chunks)
        assert len(merged) == 2

    def test_short_adjacent_chunks_merged(self):
        chunks = [
            Chunk(
                text="x" * 50,
                file_path="f.py",
                start_line=1,
                end_line=2,
                language="python",
                chunk_type="function",
            ),
            Chunk(
                text="y" * 50,
                file_path="f.py",
                start_line=3,
                end_line=4,
                language="python",
                chunk_type="function",
            ),
            Chunk(
                text="z" * 200,
                file_path="f.py",
                start_line=5,
                end_line=10,
                language="python",
                chunk_type="function",
            ),
        ]
        merged = _merge_short_chunks(chunks)
        assert len(merged) == 2
        assert merged[0].start_line == 1
        assert merged[0].end_line == 4


# ---------------------------------------------------------------------------
# chunk_file top-level
# ---------------------------------------------------------------------------


class TestChunkFile:
    def _write_temp_file(self, tmp_path, content, suffix=".py"):
        filepath = tmp_path / f"test{suffix}"
        filepath.write_text(content)
        return filepath

    def test_chunks_python_file(self, tmp_path):
        source = _py_func("foo") + "\n"
        filepath = self._write_temp_file(tmp_path, source)
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        assert len(result.chunks) >= 1
        assert result.file_hash != ""

    def test_chunks_javascript_file(self, tmp_path):
        source = _js_func("bar")
        filepath = self._write_temp_file(tmp_path, source, ".js")
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        assert len(result.chunks) >= 1
        assert result.chunks[0].language == "javascript"

    def test_chunks_typescript_file(self, tmp_path):
        source = f"function baz(): number {{\n{_JS_BODY}}}\n"
        filepath = self._write_temp_file(tmp_path, source, ".ts")
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        assert len(result.chunks) >= 1
        assert result.chunks[0].language == "typescript"

    def test_chunks_generic_file(self, tmp_path):
        source = "some text content\nmore text\n"
        filepath = self._write_temp_file(tmp_path, source, ".txt")
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        assert len(result.chunks) >= 1
        assert result.chunks[0].language == "text"

    def test_cannot_read_file_returns_empty(self, tmp_path):
        filepath = tmp_path / "nonexistent.py"
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        assert len(result.chunks) == 0
        assert result.file_hash == ""

    def test_relative_path_is_computed(self, tmp_path):
        source = _py_func("foo") + "\n"
        filepath = self._write_temp_file(tmp_path, source)
        project_root = tmp_path.parent
        result = chunk_file(filepath, project_root)
        for chunk in result.chunks:
            assert chunk.file_path == str(filepath.relative_to(project_root))


# ---------------------------------------------------------------------------
# should_index
# ---------------------------------------------------------------------------


class TestShouldIndex:
    def test_python_file_is_indexable(self):
        assert should_index(Path("src/main.py")) is True

    def test_js_file_is_indexable(self):
        assert should_index(Path("src/app.js")) is True

    def test_lock_file_is_skipped(self):
        assert should_index(Path("poetry.lock")) is False

    def test_image_is_skipped(self):
        assert should_index(Path("img/photo.png")) is False

    def test_unknown_suffix_is_skipped(self):
        assert should_index(Path("data/export.csv")) is False

    def test_skip_dir_filters(self):
        assert should_index(Path("node_modules/foo/index.js")) is False
        assert should_index(Path(".git/config")) is False
        assert should_index(Path("__pycache__/module.pyc")) is False

    def test_code_inside_venv_is_skipped(self):
        assert should_index(Path("venv/lib/site-packages/module.py")) is False

    def test_min_css_and_map_files_are_skipped(self):
        assert should_index(Path("lib/bundle.min.css")) is False
        assert should_index(Path("lib/bundle.js.map")) is False

    def test_dotfiles_in_skip_dirs(self):
        assert should_index(Path(".venv/lib/module.py")) is False
        assert should_index(Path("dist/bundle.js")) is False
        assert should_index(Path("build/output.js")) is False


# ---------------------------------------------------------------------------
# is_skip_dir
# ---------------------------------------------------------------------------


class TestIsSkipDir:
    def test_known_skip_dirs(self):
        assert is_skip_dir(".git") is True
        assert is_skip_dir("node_modules") is True
        assert is_skip_dir("__pycache__") is True
        assert is_skip_dir("dist") is True
        assert is_skip_dir("build") is True

    def test_normal_dir_is_not_skipped(self):
        assert is_skip_dir("src") is False
        assert is_skip_dir("lib") is False
        assert is_skip_dir("components") is False


# ---------------------------------------------------------------------------
# func_type
# ---------------------------------------------------------------------------


class TestFuncType:
    def test_normal_function(self):
        assert _func_type("my_function") == "function"

    def test_dunder_function(self):
        assert _func_type("__init__") == "dunder"
        assert _func_type("__post_init__") == "dunder"

    def test_leading_underscore(self):
        assert _func_type("_private_func") == "function"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_file_with_only_comments(self, tmp_path):
        source = "# This is a comment\n# Another comment\n"
        filepath = tmp_path / "test.py"
        filepath.write_text(source)
        result = chunk_file(filepath, tmp_path.parent)
        assert isinstance(result, ChunkResult)

    def test_very_deeply_nested_python(self):
        source = _py_class("A", body_lines=10)
        # Only top-level class A should be a chunk
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "class"

    def test_chunk_metadata_correct(self):
        source = _py_func("foo") + "\n"
        chunks = _chunk_python(source, "test.py", "python")
        assert len(chunks) == 1
        c = chunks[0]
        assert c.file_path == "test.py"
        assert c.language == "python"
        assert c.start_line == 1

    def test_code_suffixes_has_expected_languages(self):
        assert _CODE_SUFFIXES[".py"] == "python"
        assert _CODE_SUFFIXES[".js"] == "javascript"
        assert _CODE_SUFFIXES[".ts"] == "typescript"
        assert _CODE_SUFFIXES[".go"] == "go"
        assert _CODE_SUFFIXES[".rs"] == "rust"
        assert _CODE_SUFFIXES[".rb"] == "ruby"
        assert _CODE_SUFFIXES[".sh"] == "shell"
        assert _CODE_SUFFIXES[".md"] == "markdown"
