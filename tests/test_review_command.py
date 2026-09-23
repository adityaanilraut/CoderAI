"""/review pipeline: diff collection, Jev triage/gate wiring, and rendering."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

import coderai.jev.client as jev_client
import coderai.triage.engine as eng
import coderai.triage.review as review
from coderai.triage.engine import GateResult, TriageResult
from coderai.triage.review import FileDiff, ReviewError, ReviewFinding


@pytest.fixture(autouse=True)
def _no_jev(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "JEV_API_KEY", "CODERAI_REVIEW_MAX_CHARS"):
        monkeypatch.delenv(var, raising=False)
    eng.reset_triage_engine()
    jev_client.jev_cache_clear()
    yield
    eng.reset_triage_engine()
    jev_client.jev_cache_clear()


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("def f(x):\n    return x\n")
    (tmp_path / "README.md").write_text("# Title\n")
    (tmp_path / ".gitignore").write_text("ignored.log\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _json_reply(findings):
    return lambda messages: json.dumps({"findings": findings})


SAMPLE_DIFF = """\
diff --git a/src/a.py b/src/a.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-x = 1
+x = None
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-y = 2
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
diff --git a/dir with space/f.ts b/dir with space/f.ts
new file mode 100644
--- /dev/null
+++ b/dir with space/f.ts
@@ -0,0 +1 @@
+export const z = 1;
"""


def test_split_diff_by_file_handles_rename_delete_binary_and_spaces():
    files = review.split_diff_by_file(SAMPLE_DIFF)
    assert [f.path for f in files] == [
        "src/a.py",
        "new.py",
        "gone.py",
        "logo.png",
        "dir with space/f.ts",
    ]
    assert files[0].diff.startswith("diff --git a/src/a.py")
    assert "+x = None" in files[0].diff
    assert "gone.py" not in files[0].diff


def test_collect_git_diff_includes_modified_and_untracked_but_not_ignored(repo):
    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")
    (repo / "new_mod.py").write_text("import os\n")
    (repo / "ignored.log").write_text("noise\n")
    paths = [f.path for f in review.split_diff_by_file(review.collect_git_diff(str(repo)))]
    assert "app.py" in paths
    assert "new_mod.py" in paths
    assert "ignored.log" not in paths
    no_untracked = review.collect_git_diff(str(repo), include_untracked=False)
    assert "new_mod.py" not in no_untracked


def test_collect_git_diff_works_before_first_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "a.py")
    paths = [f.path for f in review.split_diff_by_file(review.collect_git_diff(str(tmp_path)))]
    assert paths == ["a.py"]


def test_collect_git_diff_against_base_uses_merge_base(repo):
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.py").write_text("y = 2\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "main_only.py").write_text("z = 3\n")
    _git(repo, "add", "main_only.py")
    _git(repo, "commit", "-q", "-m", "main moves on")
    _git(repo, "checkout", "-q", "feature")
    paths = [f.path for f in review.split_diff_by_file(review.collect_git_diff(str(repo), "main"))]
    assert paths == ["feature.py"]


def test_collect_git_diff_errors(tmp_path, repo):
    with pytest.raises(ReviewError, match="not a git repository"):
        review.collect_git_diff(str(tmp_path / "nowhere"))
    with pytest.raises(ReviewError, match="cannot diff against"):
        review.collect_git_diff(str(repo), "no-such-branch")


def _t(path, should_review, risk=0.5, priority=1):
    return TriageResult(path, risk, "core_logic", priority, should_review, "Jev screening: x")


def test_select_for_review_ranks_skips_and_budgets():
    files = [FileDiff("low.py", "a" * 10), FileDiff("hot.py", "b" * 10), FileDiff("big.py", "c" * 50),
             FileDiff("skip.css", "d" * 10)]
    triage = [_t("low.py", True, 0.3, 0), _t("hot.py", True, 0.9, 3), _t("big.py", True, 0.5, 2),
              _t("skip.css", False)]
    selected, over = review.select_for_review(files, triage, budget=30)
    assert [f.path for f in selected] == ["hot.py", "low.py"]
    assert over == ["big.py"]
    selected_all, _ = review.select_for_review(files, triage, review_all=True, budget=1000)
    assert "skip.css" in [f.path for f in selected_all]


def test_select_for_review_truncates_single_oversized_file():
    selected, over = review.select_for_review(
        [FileDiff("big.py", "x" * 100)], [_t("big.py", True)], budget=40
    )
    assert len(selected[0].diff) == 40
    assert over == []


def test_parse_findings_accepts_fenced_json_and_normalizes():
    text = (
        "Here you go:\n```json\n"
        + json.dumps({"findings": [
            {"file": "a.py", "line": 3, "severity": "CRITICAL", "comment": "Null deref."},
            {"file": "b.py", "line": True, "severity": "blocker", "comment": "Leak."},
            {"file": "", "comment": "no file"},
            "junk",
        ]})
        + "\n```"
    )
    findings = review.parse_findings(text)
    assert findings == [
        ReviewFinding("a.py", 3, "critical", "Null deref."),
        ReviewFinding("b.py", None, "minor", "Leak."),
    ]


@pytest.mark.parametrize("text", ["", "no json here", "{not json}", '{"other": []}'])
def test_parse_findings_returns_none_without_usable_json(text):
    assert review.parse_findings(text) is None


def test_run_review_without_jev_reviews_code_and_keeps_every_comment(repo):
    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")
    (repo / "README.md").write_text("# New title\n")
    seen = {}

    def complete(messages):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({"findings": [
            {"file": "app.py", "line": 2, "severity": "major", "comment": "Division by zero."}
        ]})

    report = asyncio.run(review.run_review(str(repo), complete))
    assert report.reviewed == ["app.py"]
    assert "README.md" not in seen["prompt"]
    assert [f.comment for f in report.findings] == ["Division by zero."]
    assert report.findings[0].gate is not None and report.findings[0].gate.passed
    assert report.dropped == []


def test_run_review_uses_jev_triage_and_gate(repo, monkeypatch):
    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")
    (repo / "util.py").write_text("y = 1\n")

    async def fake_screen(items, **kw):
        return [
            TriageResult(p, 0.9 if p == "app.py" else 0.05, "core_logic", 2, p == "app.py",
                         "Jev screening: stub")
            for p, _ in items
        ]

    gated = []

    async def fake_gate(path, diff, comment, **kw):
        gated.append((path, comment))
        keep = "real" in comment
        return GateResult(0.9 if keep else 0.2, 0.9 if keep else 0.3, keep, "Jev gate: stub", 0.1)

    monkeypatch.setattr(review, "jev_screen_many_async", fake_screen)
    monkeypatch.setattr(review, "jev_gate_comment_async", fake_gate)
    report = asyncio.run(review.run_review(str(repo), _json_reply([
        {"file": "app.py", "line": 2, "severity": "major", "comment": "A real bug."},
        {"file": "./app.py", "line": None, "severity": "minor", "comment": "Speculative nit."},
        {"file": "elsewhere.py", "line": 1, "severity": "minor", "comment": "Unmatched file."},
    ])))
    assert report.reviewed == ["app.py"]
    assert [f.comment for f in report.findings] == ["A real bug.", "Unmatched file."]
    assert [f.comment for f in report.dropped] == ["Speculative nit."]
    assert report.findings[1].gate is None
    assert ("app.py", "Line 2: A real bug.") in gated
    assert all(path != "elsewhere.py" for path, _ in gated)


def test_run_review_unparseable_reply_is_shown_ungated(repo):
    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")
    report = asyncio.run(review.run_review(str(repo), lambda m: "Looks risky: divides by zero."))
    assert report.parse_failed is True
    assert report.raw_review == "Looks risky: divides by zero."
    assert report.findings == []


def test_run_review_wraps_model_errors(repo):
    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")

    def boom(messages):
        raise ConnectionError("down")

    with pytest.raises(ReviewError, match="main model call failed: ConnectionError"):
        asyncio.run(review.run_review(str(repo), boom))


def test_run_review_no_changes(repo):
    report = asyncio.run(review.run_review(str(repo), _json_reply([])))
    assert report.files == [] and report.reviewed == []


def test_parse_review_args():
    from coderai.ui.shell.review_cmd import parse_review_args

    assert parse_review_args("") == (None, False, True)
    assert parse_review_args("main --all --no-untracked") == ("main", True, False)
    assert parse_review_args("--bogus") is None
    assert parse_review_args("main dev") is None


def test_completer_refuses_jev_and_missing_key():
    from coderai.ui.shell.review_cmd import make_completer

    class Mgr:
        def __init__(self, info):
            self.info = info

        def create_openai_client(self):
            return self.info

    with pytest.raises(ReviewError, match="Jev System-One"):
        make_completer(Mgr({"client": None, "baseURL": "jev://system-one"}))([])
    with pytest.raises(ReviewError, match="no API key"):
        make_completer(Mgr({"client": None, "baseURL": None}))([])


def test_review_command_registered_and_documented():
    from coderai.ui.shell.dispatch import registry
    from coderai.ui.shell.slash import COMMAND_CATALOG, COMMAND_HELP_DETAILS

    assert registry.find_command("review") is not None
    assert "review" in COMMAND_CATALOG
    assert "review" in COMMAND_HELP_DETAILS


def test_review_command_renders_plain_output(repo, capsys, monkeypatch):
    from coderai.ui.shell import review_cmd

    (repo / "app.py").write_text("def f(x):\n    return x / 0\n")

    class Mgr:
        project_root = str(repo)

    monkeypatch.setattr(review_cmd, "make_completer", lambda mgr: _json_reply([
        {"file": "app.py", "line": 2, "severity": "critical", "comment": "Divides by [zero]."}
    ]))
    asyncio.run(review_cmd.run_review_command(Mgr(), None, ""))
    out = capsys.readouterr().out
    assert "Jev System-One is not configured" in out
    assert "app.py: review" in out
    assert "[critical] app.py:2" in out
    assert "Divides by [zero]." in out


def test_render_triage_collapses_skipped_rows_and_flags_budget(capsys):
    from coderai.ui.shell import review_cmd
    from coderai.triage.review import ReviewReport

    n = review_cmd.COLLAPSE_SKIPPED_ABOVE + 5
    files = [FileDiff(f"f{i}.py", "d") for i in range(n)]
    report = ReviewReport(
        base="HEAD",
        files=files,
        triage=[_t(f.path, i < 2) for i, f in enumerate(files)],
        reviewed=["f0.py"],
        over_budget=["f1.py"],
    )
    review_cmd.render_triage(None, report)
    out = capsys.readouterr().out
    assert "f0.py: review" in out
    assert "f1.py: not sent (over budget)" in out
    assert "f2.py" not in out
    assert f"{n - 2} more file(s) skipped" in out
    assert "1 flagged file(s) did not fit the review budget" in out


def test_review_command_reports_git_errors(tmp_path, capsys):
    from coderai.ui.shell import review_cmd

    class Mgr:
        project_root = str(tmp_path)

    asyncio.run(review_cmd.run_review_command(Mgr(), None, ""))
    assert "Review failed: not a git repository" in capsys.readouterr().out
