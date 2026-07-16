"""Coverage for coderAI/tui/project.py file scanning."""

from coderAI.tui.project import async_scan_project_files, scan_project_files


def _make_tree(root):
    (root / "a.py").write_text("x")
    (root / "README.md").write_text("readme")
    (root / "image.png").write_text("binary")  # skipped extension
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "b.py").write_text("y")
    skip = root / "node_modules"
    skip.mkdir()
    (skip / "dep.js").write_text("ignored")  # skipped directory
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "c.pyc").write_text("ignored")


def test_scan_project_files_filters_and_sorts(tmp_path):
    _make_tree(tmp_path)
    files = scan_project_files(str(tmp_path))
    assert files == sorted(files)
    assert "a.py" in files
    assert "README.md" in files
    assert "pkg/b.py" in files
    # Skipped extension and skipped directories are excluded.
    assert "image.png" not in files
    assert not any(f.startswith("node_modules") for f in files)
    assert not any("__pycache__" in f for f in files)


def test_scan_project_files_empty_dir(tmp_path):
    assert scan_project_files(str(tmp_path)) == []


async def test_async_scan_project_files(tmp_path):
    _make_tree(tmp_path)
    files = await async_scan_project_files(str(tmp_path))
    assert "a.py" in files
    assert "pkg/b.py" in files
