"""Jev calibration/boundary tests against the real typesafe_sdk response schema.

Uses httpx2.MockTransport so no network or key is needed.
"""

from __future__ import annotations

import json

import pytest

ts = pytest.importorskip("typesafe_sdk")
httpx2 = pytest.importorskip("httpx2")

import coderai.jev.client as jev_client  # noqa: E402
import coderai.triage.engine as eng  # noqa: E402

_RESETS = (
    eng.reset_jev_question_cache,
    eng.reset_jev_shared_clients,
    eng.reset_triage_engine,
    jev_client.jev_cache_clear,
    jev_client.jev_status_clear,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path_factory):
    for var in (
        "TYPESAFE_API_KEY",
        "JEV_API_KEY",
        "CODERAI_JEV_MIN_CONFIDENCE",
        "CODERAI_JEV_MAX_DIFF_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path_factory.mktemp("share")))
    for fn in _RESETS:
        fn()
    yield
    for fn in _RESETS:
        fn()


def _triage_body(noul=0.5, choice="documentation", cconf=0.36, cprobs=None, sprobs=None):
    return {
        "model": "jev-system-one",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "answers": {
            "needs_review": {"type": "noul", "noul": noul},
            "change_type": {
                "type": "choice",
                "choice": choice,
                "confidence": cconf,
                "probabilities": cprobs
                or {"core_logic": 0.34, "boilerplate": 0.30, "documentation": 0.36},
            },
            "priority": {
                "type": "score",
                "score": 1.7,
                "confidence": 0.4,
                "legend": {"0": "Trivial", "1": "Low", "2": "Medium", "3": "Critical"},
                "probabilities": sprobs or {"0": 0.05, "1": 0.25, "2": 0.65, "3": 0.05},
            },
        },
    }


def _gate_body(is_bug=0.2, spec=0.1, accept=0.9):
    return {
        "model": "jev-system-one",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "answers": {
            "is_actionable_bug": {"type": "noul", "noul": is_bug},
            "is_speculative_or_nit": {"type": "noul", "noul": spec},
            "will_developer_accept": {"type": "noul", "noul": accept},
        },
    }


def _json(body):
    raw = json.dumps(body).encode()
    return lambda req: httpx2.Response(
        200, content=raw, headers={"content-type": "application/json"}
    )


def _engine(handler) -> eng.TriageEngine:
    e = eng.TriageEngine(api_key="k")
    e._client = ts.TypeSafeClient(api_key="k", transport=httpx2.MockTransport(handler))
    return e


def _patch_shared_transport(api_key: str, handler) -> None:
    client = eng._get_shared_client(api_key)
    client._http_client = httpx2.Client(transport=httpx2.MockTransport(handler))


def test_low_confidence_documentation_does_not_suppress_review():
    r = _engine(_json(_triage_body())).screen_diff_hunk("static/site.css", "body {}")
    assert r.category == "documentation"
    assert r.uncertain is True
    assert r.should_review is True


def test_confident_documentation_still_suppresses():
    body = _triage_body(
        cconf=0.9, cprobs={"core_logic": 0.05, "boilerplate": 0.05, "documentation": 0.9}
    )
    r = _engine(_json(body)).screen_diff_hunk("static/site.css", "/* comment */")
    assert r.uncertain is False
    assert r.should_review is False


def test_priority_uses_argmax_and_exposes_distributions():
    r = _engine(_json(_triage_body())).screen_diff_hunk("src/a.py", "d")
    assert r.priority == 2
    assert r.priority_expected == pytest.approx(1.7)
    assert r.priority_probabilities == {0: 0.05, 1: 0.25, 2: 0.65, 3: 0.05}
    assert r.category_probabilities["documentation"] == pytest.approx(0.36)
    assert r.category_confidence == pytest.approx(0.36)


def test_nan_noul_fails_closed():
    body = _triage_body(noul=float("nan"), choice="core_logic", cconf=0.9)
    r = _engine(_json(body)).screen_diff_hunk("static/site.css", "x")
    assert r.should_review is True
    assert r.reason.startswith("Triage error fallback")


def test_nan_gate_fails_open():
    g = _engine(_json(_gate_body(is_bug=float("nan")))).gate_candidate_comment("a.py", "d", "c")
    assert g.passed is True
    assert g.reason.startswith("Gate error fallback")


def test_unknown_choice_label_fails_closed():
    body = _triage_body(choice="refactor", cconf=0.9, cprobs={"refactor": 0.9})
    r = _engine(_json(body)).screen_diff_hunk("static/site.css", "x")
    assert r.should_review is True
    assert "unknown label" in r.reason


def test_truncated_diff_forces_review(monkeypatch):
    monkeypatch.setenv("CODERAI_JEV_MAX_DIFF_CHARS", "512")
    body = _triage_body(
        noul=0.01,
        cconf=0.95,
        cprobs={"core_logic": 0.0, "boilerplate": 0.05, "documentation": 0.95},
    )
    r = _engine(_json(body)).screen_diff_hunk("static/site.css", "x" * 2000)
    assert r.truncated is True
    assert r.should_review is True


def test_truncated_diff_gate_fails_open(monkeypatch):
    monkeypatch.setenv("CODERAI_JEV_MAX_DIFF_CHARS", "512")
    e = _engine(_json(_gate_body(is_bug=0.2)))
    assert e.gate_candidate_comment("a.py", "x" * 100, "c").passed is False
    g = e.gate_candidate_comment("a.py", "x" * 2000, "c")
    assert g.truncated is True
    assert g.passed is True
    assert g.is_speculative == pytest.approx(0.1)


def test_gate_comment_is_truncated_before_dispatch(monkeypatch):
    monkeypatch.setenv("CODERAI_JEV_MAX_DIFF_CHARS", "512")
    seen = {}

    def handler(req):
        seen["comment"] = json.loads(req.content)["state"]["suggested_review_comment"]
        return _json(_gate_body(is_bug=0.9))(req)

    _engine(handler).gate_candidate_comment("a.py", "d", "c" * 5000)
    assert len(seen["comment"]) == 512


def test_shell_and_tsx_are_code_files():
    for ext in (".sh", ".tsx", ".jsx", ".sql", ".toml"):
        assert ext in eng.CODE_EXTENSIONS
    assert not (set(eng.DOC_OR_BOILERPLATE_EXTENSIONS) & set(eng.CODE_EXTENSIONS))


def test_shared_client_does_not_retry():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx2.Response(503, json={"detail": "down"})

    _patch_shared_transport("k-retry", handler)
    r = eng.TriageEngine(api_key="k-retry").screen_diff_hunk("a.py", "x")
    assert r.reason.startswith("Triage error fallback")
    assert calls["n"] == 1


def test_status_reports_unavailable_on_auth_failure(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "bad-key")
    _patch_shared_transport("bad-key", lambda req: httpx2.Response(401, json={"detail": "bad key"}))
    s = jev_client.jev_status(use_cache=False)
    assert s["configured"] is True
    assert s["available"] is False
    assert s["latency_ms"] is None


def test_connectivity_probe_reports_auth_failure():
    from coderai.llm import probe_provider_connectivity

    _patch_shared_transport("bad-key", lambda req: httpx2.Response(401, json={"detail": "bad key"}))
    ok, msg = probe_provider_connectivity("jev-system-one", api_key="bad-key")
    assert ok is False
    assert "401" in msg


def test_min_confidence_is_part_of_triage_cache_key(monkeypatch):
    k1 = jev_client._triage_key("a.css", "d", 0.2, True, "k")
    monkeypatch.setenv("CODERAI_JEV_MIN_CONFIDENCE", "0.9")
    assert jev_client._triage_key("a.css", "d", 0.2, True, "k") != k1


def test_jev_not_offered_as_chat_model():
    from coderai.utils.common.model_capabilities import CURATED_MODELS, is_jev_model

    assert not any(is_jev_model(name) for name, _, _ in CURATED_MODELS)
