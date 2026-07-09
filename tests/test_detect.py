"""Tests for walk_up_detect — the shared detect-and-probe loop (finding 13).

format / lint / testing / package_manager all route their "which tool does this
project use" detection through this helper, so its walk/boundary/ordering
behavior is worth pinning directly.
"""

from coderAI.tools._detect import walk_up_detect

# A minimal tool table: each tool has one indicator file (mirrors the real
# FORMATTERS/LINTERS shape where detect_files is a set).
TABLE = {
    "alpha": {"detect_files": {"alpha.toml"}},
    "beta": {"detect_files": {"beta.toml"}},
}
ORDER = ["alpha", "beta"]


def _always(name, _dir):
    """Availability callback that accepts any detected tool."""
    return name


def test_start_dir_hit(tmp_path):
    (tmp_path / "beta.toml").touch()
    assert walk_up_detect(str(tmp_path), TABLE, ORDER, _always) == "beta"


def test_parent_walk_hit(tmp_path):
    (tmp_path / "alpha.toml").touch()
    child = tmp_path / "sub" / "deep"
    child.mkdir(parents=True)
    assert walk_up_detect(str(child), TABLE, ORDER, _always) == "alpha"


def test_git_boundary_stops_walk(tmp_path):
    # Indicator lives ABOVE a .git boundary → the walk stops at .git and misses it.
    (tmp_path / "alpha.toml").touch()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    child = repo / "src"
    child.mkdir()
    assert walk_up_detect(str(child), TABLE, ORDER, _always) is None


def test_git_boundary_dir_itself_is_probed(tmp_path):
    # The directory holding .git is probed before the walk breaks.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "alpha.toml").touch()
    assert walk_up_detect(str(repo), TABLE, ORDER, _always) == "alpha"


def test_file_start_probes_parent_dir(tmp_path):
    (tmp_path / "alpha.toml").touch()
    f = tmp_path / "main.py"
    f.write_text("x = 1\n")
    assert walk_up_detect(str(f), TABLE, ORDER, _always) == "alpha"


def test_order_preference_wins(tmp_path):
    # Both indicators present in the same dir → first in `order` wins.
    (tmp_path / "alpha.toml").touch()
    (tmp_path / "beta.toml").touch()
    assert walk_up_detect(str(tmp_path), TABLE, ORDER, _always) == "alpha"


def test_available_none_continues_probing(tmp_path):
    # alpha's indicator exists but alpha is "unavailable" → fall through to beta.
    (tmp_path / "alpha.toml").touch()
    (tmp_path / "beta.toml").touch()

    def _only_beta(name, _dir):
        return name if name == "beta" else None

    assert walk_up_detect(str(tmp_path), TABLE, ORDER, _only_beta) == "beta"


def test_no_indicators_returns_none(tmp_path):
    assert walk_up_detect(str(tmp_path), TABLE, ORDER, _always) is None
