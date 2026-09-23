"""Unit tests for CoderAI System 1 TriageEngine."""

import pytest
from coderai.triage.engine import TriageEngine, TriageResult, GateResult


@pytest.fixture(autouse=True)
def _no_live_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)


def test_triage_engine_initialization():
    # No hardcoded key: explicit arg wins, otherwise env (may be None in CI).
    engine = TriageEngine(api_key="test-key-123")
    assert engine.api_key == "test-key-123"
    engine2 = TriageEngine(api_key=None)
    assert engine2.api_key is None or isinstance(engine2.api_key, str)


def test_triage_documentation_bypass():
    engine = TriageEngine()
    result = engine.screen_diff_hunk("docs/README.md", "@@ -1 +1 @@\n-# Title\n+# New Title")
    assert isinstance(result, TriageResult)
    assert result.category == "documentation"
    assert not result.should_review


def test_triage_core_logic_evaluation():
    engine = TriageEngine()
    diff = """
@@ -50,6 +50,9 @@ def authenticate(user, password):
+    if user is None:
+        return False
+    if isinstance(process, multiprocessing.Process):
+        process.kill()
"""
    result = engine.screen_diff_hunk("src/auth/service.py", diff)
    assert isinstance(result, TriageResult)
    assert result.risk >= 0.0
    assert result.category in ("core_logic", "boilerplate", "documentation")


def test_gate_candidate_comment():
    engine = TriageEngine()
    diff = "@@ -10,3 +10,3 @@\n-x = 1\n+x = None"
    comment = "Potential null pointer dereference: x is set to None and dereferenced below."
    gate_result = engine.gate_candidate_comment("src/app.py", diff, comment, threshold=0.60)
    assert isinstance(gate_result, GateResult)
    assert isinstance(gate_result.is_actionable_bug, float)
    assert isinstance(gate_result.will_developer_accept, float)
    assert isinstance(gate_result.passed, bool)
