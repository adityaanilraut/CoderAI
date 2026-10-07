"""Protect review ingestion from progress text and benchmark scoring regressions."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "local_review_runner", Path(__file__).resolve().parents[1] / "scripts/run_coderai_local.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_final_review_uses_session_message_instead_of_rendered_output(tmp_path):
    sessions = tmp_path / ".coderai/sessions"
    sessions.mkdir(parents=True)
    rows = [
        {"role": "assistant", "content": "I will inspect the diff."},
        {"role": "tool", "content": "<review>source text</review>"},
        {"role": "assistant", "content": "<review>1. Concrete defect.</review>"},
    ]
    (sessions / "session.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    assert runner.parse_final(tmp_path, "wrapped terminal output") == "1. Concrete defect."


def test_incomplete_review_is_not_injected(tmp_path):
    with pytest.raises(ValueError, match="complete review tags"):
        runner.parse_final(tmp_path, "Still reviewing <review>partial result")


def test_core_score_excludes_matched_style_and_rejects_judge_errors():
    result = {
        "true_positives": [{"category": "bug"}, {"category": "style"}],
        "false_negatives": [{"category": "test_gap"}, {"category": "speculative"}],
        "false_positives": [{"candidate": "Noise"}],
    }
    evaluations = {"pr": {"coderai": result}}
    assert runner.score(evaluations, ["pr"]) == {
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "precision": 50,
        "recall": 50,
        "f1": 50,
    }
    assert runner.score(evaluations, ["pr"], core_only=False)["tp"] == 2
    assert runner.score(evaluations, ["pr"], core_only=False)["fn"] == 2
    result["errors"] = ["Judge request failed"]
    with pytest.raises(ValueError, match="Incomplete evaluation"):
        runner.score(evaluations, ["pr"])


def test_grouping_preserves_every_file_and_never_splits_its_hunks():
    files = [
        "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n@@ -5 +5 @@\n-z\n+w\n",
        "diff --git a/test_b.py b/test_b.py\n@@ -1 +1 @@\n+assert y\n",
    ]
    diff = "".join(files)
    chunks = runner.chunk_diff(diff, 60)
    assert "".join(chunks) == diff
    assert all(any(file in chunk for chunk in chunks) for file in files)
    assert runner.chunk_diff(diff, 0) == [diff]


def test_final_session_review_without_tags_is_accepted(tmp_path):
    sessions = tmp_path / ".coderai/sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": "1. file.py:42 — Concrete defect.",
            }
        )
    )
    assert (
        runner.parse_final(tmp_path, "untrusted terminal progress")
        == "1. file.py:42 — Concrete defect."
    )


def test_luna_review_with_reasoning_off_omits_unsupported_temperature(tmp_path, monkeypatch):
    from types import SimpleNamespace

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        captured["command"] = command
        sessions = kwargs["cwd"] / ".coderai/sessions"
        sessions.mkdir(parents=True)
        (sessions / "session.jsonl").write_text(
            json.dumps(
                {"role": "assistant", "content": "<review>No actionable issues found.</review>"}
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    body = runner.invoke_review(
        tmp_path / "work",
        tmp_path / "artifact",
        "Review source",
        "coderai",
        "gpt-6-luna",
        60,
        "off",
    )
    assert body == "No actionable issues found."
    assert captured["env"]["CODERAI_TEMPERATURE"] == ""
    assert "--no-thinking" in captured["command"]


def test_requested_luna_reasoning_cannot_silently_fall_back_to_none(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def fake_run(command, **kwargs):
        sessions = kwargs["cwd"] / ".coderai/sessions"
        sessions.mkdir(parents=True)
        (sessions / "session.jsonl").write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "<review>No actionable issues found.</review>",
                    "meta": {"usage": {"reasoning_tokens": 0}},
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    artifact = tmp_path / "artifact"
    with pytest.raises(RuntimeError, match="recorded none"):
        runner.invoke_review(
            tmp_path / "work", artifact, "Review source", "coderai", "gpt-6-luna", 60, "high"
        )
    assert (artifact / "session/sessions/session.jsonl").exists()
    assert json.loads((artifact / "usage.json").read_text())["reasoning_tokens"] == 0


@pytest.mark.parametrize("supplement", ["1. New quality defect.", "No actionable issues found."])
def test_supplement_retains_previous_blind_findings(tmp_path, monkeypatch, supplement):
    import hashlib
    from types import SimpleNamespace

    url = "https://github.com/example/repo/pull/1"
    artifact = tmp_path / "reviews" / hashlib.sha256(url.encode()).hexdigest()[:16]
    artifact.mkdir(parents=True)
    diff = "diff --git a/file.py b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    previous = "1. Original runtime finding that must be retained verbatim."
    (artifact / "diff.patch").write_text(diff)
    (artifact / "refinement-draft.txt").write_text(previous)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=diff))
    calls = []

    def fake_review(work, output, prompt, *args):
        calls.append(prompt)
        return supplement

    monkeypatch.setattr(runner, "invoke_review", fake_review)
    _, body = runner.review_one(
        url,
        tmp_path,
        "coderai",
        "gpt-6-luna",
        60,
        "xhigh",
        0,
        verification_effort="off",
        refine=True,
    )
    assert len(calls) == 1
    assert previous in calls[0] and diff in calls[0]
    assert body.startswith(previous)
    if supplement.startswith("1."):
        assert "New quality defect." in body
    else:
        assert body == previous
    metadata = json.loads((artifact / "review.json").read_text())
    assert metadata["refine"] is True
    assert metadata["verification_effort"] == "off"


def test_two_verifiers_receive_same_blind_draft_and_combine_uniformly(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import hashlib

    diff = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=diff))
    calls = []
    outputs = iter(["1. Discovery.", "1. First validated issue.", "1. Second validated issue."])

    def invoke(work, artifact, prompt, *args):
        artifact.mkdir(parents=True, exist_ok=True)
        calls.append(prompt)
        return next(outputs)

    monkeypatch.setattr(runner, "invoke_review", invoke)
    url = "https://github.com/example/repo/pull/1"
    _, body = runner.review_one(
        url, tmp_path, "coderai", "gpt-6-luna", 60, "xhigh", 0, dual_verification=True
    )
    assert len(calls) == 3
    assert calls[1].endswith("<draft>\n1. Discovery.\n</draft>\n")
    assert calls[2].endswith("<draft>\n1. Discovery.\n</draft>\n")
    assert "First validated issue" not in calls[2]
    assert body == "1. First validated issue.\n\n2. Second validated issue."
    metadata = json.loads(
        (
            tmp_path / "reviews" / hashlib.sha256(url.encode()).hexdigest()[:16] / "review.json"
        ).read_text()
    )
    assert metadata["dual_verification"] is True
    assert metadata["diff_sha256"] == hashlib.sha256(diff.encode()).hexdigest()
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=diff + "+changed\n")
    )
    with pytest.raises(ValueError, match="diff changed"):
        runner.review_one(
            url, tmp_path, "coderai", "gpt-6-luna", 60, "xhigh", 0, dual_verification=True
        )


def test_partial_review_rejects_changed_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="diff"))
    monkeypatch.setattr(
        runner, "invoke_review", lambda *a: (_ for _ in ()).throw(RuntimeError("interrupted"))
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        runner.review_one("pr", tmp_path, "coderai", "gpt-6-luna", 60, "xhigh", 0)
    with pytest.raises(ValueError, match="Partial review provenance"):
        runner.review_one("pr", tmp_path, "coderai", "gpt-6-luna", 60, "high", 0)


@pytest.mark.parametrize(
    "previous,second,expected",
    [
        ("No actionable issues found.", "1. New.", "1. New."),
        ("1. Old.", "No actionable issues found.", "1. Old."),
        ("1. Old.\n2. Other.", "1. New.", "1. Old.\n2. Other.\n\n3. New."),
    ],
)
def test_uniform_combination_handles_empty_reviews(previous, second, expected):
    assert runner.append_findings(previous, second) == expected


def test_diff_comparison_accepts_only_equivalent_blob_abbreviations():
    first = (
        "diff --git a/a b/a\nindex abcdef1234567..012345678abcd 100644\n@@ -1 +1 @@\n-old\n+new\n"
    )
    short = first.replace("abcdef1234567", "abcdef123456").replace("012345678abcd", "012345678abc")
    assert runner.same_diff(first, short)
    assert not runner.same_diff(first, short.replace("+new", "+changed"))
    assert not runner.same_diff(first, short.replace("abcdef123456", "abcdef999999"))
    assert not runner.same_diff(first, short.replace("100644", "100755"))
    assert not runner.same_diff(first, short.replace("abcdef123456", "abc"))


def test_timeout_preserves_diagnostics_without_injecting_partial_review(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        raise runner.subprocess.TimeoutExpired(
            command, 60, output=b"<review>partial", stderr=b"waiting"
        )

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    artifact = tmp_path / "artifact"
    with pytest.raises(RuntimeError, match="timed out after 60s"):
        runner.invoke_review(
            tmp_path / "work", artifact, "Review source", "coderai", "gpt-6-luna", 60, "xhigh"
        )
    assert (artifact / "stdout.txt").read_text() == "<review>partial"
    assert (artifact / "stderr.txt").read_text() == "waiting"
    assert json.loads((artifact / "timeout.json").read_text())["timeout_seconds"] == 60
    assert not (artifact / "review.json").exists()
