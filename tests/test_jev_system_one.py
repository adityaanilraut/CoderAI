"""Regression tests for Jev System-One fail-safe contract + fixes.

Every fallback path is stubbed via sys.modules (no network, no key):
- Tier 1 fails CLOSED (should_review=True)
- Tier 3 fails OPEN (passed=True)
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types

import pytest

import coderai.jev.client as jev_client
import coderai.triage.engine as eng


class _Noul:
    def __init__(self, instructions: str = "", **kwargs):
        self.instructions = instructions


class _Choice:
    def __init__(self, instructions: str = "", criteria=None, **kwargs):
        self.instructions = instructions
        self.criteria = criteria


class _Score:
    def __init__(self, instructions: str = "", criteria=None, **kwargs):
        self.instructions = instructions
        self.criteria = criteria


class _Val:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, nouls=None, choices=None, scores=None):
        self.nouls = nouls or {}
        self.choices = choices or {}
        self.scores = scores or {}


def _install_stub(client_cls):
    stub = types.ModuleType("typesafe_sdk")
    stub.Noul = _Noul
    stub.Choice = _Choice
    stub.Score = _Score
    stub.TypeSafeClient = client_cls
    sys.modules["typesafe_sdk"] = stub
    eng.reset_jev_question_cache()
    eng.reset_jev_shared_clients()
    eng.reset_triage_engine()
    jev_client.jev_cache_clear()
    jev_client.jev_status_clear()
    jev_client._reset_executor_for_tests()
    return stub


@pytest.fixture()
def _clean_env():
    saved = {k: os.environ.get(k) for k in (
        "TYPESAFE_API_KEY", "JEV_API_KEY", "CODERAI_JEV_TRIAGE_THRESHOLD",
        "CODERAI_JEV_GATE_THRESHOLD", "CODERAI_JEV_ACCEPT_THRESHOLD",
        "CODERAI_JEV_SPECULATIVE_THRESHOLD", "CODERAI_JEV_MAX_DIFF_CHARS",
        "CODERAI_JEV_TIMEOUT_S", "CODERAI_JEV_STATUS_TTL_S",
    )}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        eng.reset_triage_engine()
        eng.reset_jev_question_cache()
        eng.reset_jev_shared_clients()
        jev_client.jev_cache_clear()
        jev_client.jev_status_clear()
        jev_client._reset_executor_for_tests()


@pytest.fixture()
def _restore_sdk():
    real = sys.modules.get("typesafe_sdk")
    try:
        yield
    finally:
        if real is not None:
            sys.modules["typesafe_sdk"] = real
        else:
            sys.modules.pop("typesafe_sdk", None)
        eng.reset_jev_question_cache()
        eng.reset_jev_shared_clients()
        eng.reset_triage_engine()
        jev_client.jev_cache_clear()
        jev_client.jev_status_clear()
        jev_client._reset_executor_for_tests()


def test_tier1_sdk_missing_fails_closed(_clean_env):
    e = eng.TriageEngine(api_key=None)
    assert not e.is_available
    r = e.screen_diff_hunk("src/a.py", "diff")
    assert r.should_review is True
    assert r.risk == 0.50


def test_tier1_sdk_error_fails_closed(_clean_env, _restore_sdk):
    class Boom:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, **kw):
            raise RuntimeError("sdk exploded")

    _install_stub(Boom)
    e = eng.TriageEngine(api_key="k1")
    assert e.is_available
    r = e.screen_diff_hunk("src/a.py", "diff")
    assert r.should_review is True
    assert "Triage error fallback" in r.reason


def test_tier1_async_timeout_fails_closed(_clean_env, _restore_sdk):
    import time as _time

    class Slow:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, **kw):
            _time.sleep(5)
            raise AssertionError("unreachable")

    _install_stub(Slow)
    r = asyncio.run(
        jev_client.jev_screen_diff_async("src/a.py", "diff", api_key="k1", timeout_s=0.5)
    )
    assert r.should_review is True
    assert "async fallback" in r.reason


def test_tier3_sdk_missing_fails_open(_clean_env):
    e = eng.TriageEngine(api_key=None)
    g = e.gate_candidate_comment("src/a.py", "diff", "maybe bug")
    assert g.passed is True


def test_tier3_sdk_error_fails_open(_clean_env, _restore_sdk):
    class Boom:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, **kw):
            raise RuntimeError("gate exploded")

    _install_stub(Boom)
    e = eng.TriageEngine(api_key="k1")
    g = e.gate_candidate_comment("src/a.py", "diff", "maybe bug")
    assert g.passed is True
    assert "Gate error fallback" in g.reason


def test_tier3_async_timeout_fails_open(_clean_env, _restore_sdk):
    import time as _time

    class Slow:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, **kw):
            _time.sleep(5)
            raise AssertionError("unreachable")

    _install_stub(Slow)
    g = asyncio.run(
        jev_client.jev_gate_comment_async(
            "src/a.py", "diff", "maybe bug", api_key="k1", timeout_s=0.5
        )
    )
    assert g.passed is True


def test_doc_bypass_and_i18n_guard(_clean_env):
    e = eng.TriageEngine(api_key=None)
    r = e.screen_diff_hunk("docs/README.md", "x")
    assert r.should_review is False
    # i18n guard: locale/message paths are NOT bypassed (fail-closed)
    r2 = e.screen_diff_hunk("locales/en/messages.md", "x")
    assert r2.should_review is True
    r3 = e.screen_diff_hunk("src/locale/en.md", "x")
    assert r3.should_review is True


def test_json_lock_suspect_entry_removed(_clean_env):
    assert ".json-lock" not in eng.DOC_OR_BOILERPLATE_EXTENSIONS
    assert ".lock" in eng.DOC_OR_BOILERPLATE_EXTENSIONS
    # disjoint authoritative policy
    assert not (set(eng.DOC_OR_BOILERPLATE_EXTENSIONS) & set(eng.CODE_EXTENSIONS))


def test_code_extension_override_low_risk(_clean_env, _restore_sdk):
    captured = {}

    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            captured["len"] = len(state["diff"])
            return _Resp(
                nouls={"needs_review": _Val(noul=0.01)},
                choices={"change_type": _Val(choice="documentation")},
                scores={"priority": _Val(score=0)},
            )

    _install_stub(Stub)
    e = eng.TriageEngine(api_key="k1")
    # code file forced review even at low risk + documentation category
    r = e.screen_diff_hunk("src/a.py", "diff")
    assert r.should_review is True
    # non-code, low risk, documentation -> no review
    r2 = e.screen_diff_hunk("static/site.css", "diff")
    assert r2.should_review is False


def test_threshold_constants_env_overridable(_clean_env, _restore_sdk):
    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            return _Resp(
                nouls={"needs_review": _Val(noul=0.50)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=1)},
            )

    _install_stub(Stub)
    e = eng.TriageEngine(api_key="k1")
    # default 0.20: risk 0.50 on non-code file -> review
    assert e.screen_diff_hunk("static/site.css", "d").should_review is True
    os.environ["CODERAI_JEV_TRIAGE_THRESHOLD"] = "0.99"
    assert e.screen_diff_hunk("static/site.css", "d").should_review is False
    # explicit arg wins over env
    assert e.screen_diff_hunk("static/site.css", "d", threshold=0.10).should_review is True


def test_gate_triple_condition_uses_constants(_clean_env, _restore_sdk):
    seen = {}

    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            return _Resp(
                nouls={
                    "is_actionable_bug": _Val(noul=0.80),
                    "is_speculative_or_nit": _Val(noul=0.10),
                    "will_developer_accept": _Val(noul=0.70),
                }
            )

    _install_stub(Stub)
    e = eng.TriageEngine(api_key="k1")
    assert e.gate_candidate_comment("a.py", "d", "c").passed is True
    os.environ["CODERAI_JEV_ACCEPT_THRESHOLD"] = "0.95"
    assert e.gate_candidate_comment("a.py", "d", "c").passed is False
    os.environ.pop("CODERAI_JEV_ACCEPT_THRESHOLD", None)
    os.environ["CODERAI_JEV_SPECULATIVE_THRESHOLD"] = "0.05"
    assert e.gate_candidate_comment("a.py", "d", "c").passed is False


def test_single_truncation_point(_clean_env, _restore_sdk):
    captured = {}

    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            captured["diff"] = state["diff"]
            return _Resp(
                nouls={"needs_review": _Val(noul=0.9)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=2)},
            )

    _install_stub(Stub)
    big = "x" * (eng.JEV_MAX_DIFF_CHARS + 5000)
    e = eng.TriageEngine(api_key="k1")
    e.screen_diff_hunk("src/a.py", big)
    assert len(captured["diff"]) == eng.jev_max_diff_chars()
    assert jev_client._MAX_DIFF_CHARS == eng.JEV_MAX_DIFF_CHARS
    # client passes through; engine is the single cut
    seen_engine_diff = {}

    class Spy(eng.TriageEngine):
        def screen_diff_hunk(self, fp, diff, threshold=None):
            seen_engine_diff["len"] = len(diff)
            return super().screen_diff_hunk(fp, diff, threshold=threshold)

    orig = jev_client.get_triage_engine
    try:
        jev_client.get_triage_engine = lambda api_key=None: Spy(api_key="k1")  # type: ignore
        asyncio.run(jev_client.jev_screen_diff_async("src/a.py", big, api_key="k1"))
        assert seen_engine_diff["len"] == len(big)
    finally:
        jev_client.get_triage_engine = orig


def test_singleton_rotation_and_thread_safety(_clean_env):
    os.environ["TYPESAFE_API_KEY"] = "env-A"
    eng.reset_triage_engine()
    e1 = eng.get_triage_engine(api_key="explicit-1")
    assert e1.api_key == "explicit-1"
    # implicit call re-resolves env instead of leaking explicit key
    e2 = eng.get_triage_engine()
    assert e2.api_key == "env-A"
    assert e2 is not e1
    # concurrent access returns a single consistent instance
    os.environ["TYPESAFE_API_KEY"] = "stable"
    eng.reset_triage_engine()
    results = []

    def _get():
        results.append(eng.get_triage_engine())

    threads = [threading.Thread(target=_get) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(r) for r in results}) == 1


def test_shared_client_reuse(_clean_env, _restore_sdk):
    created = []

    class Stub:
        def __init__(self, api_key=None, **kw):
            created.append(api_key)

        def system_one(self, **kw):
            raise RuntimeError("x")

    _install_stub(Stub)
    a = eng.TriageEngine(api_key="same")
    b = eng.TriageEngine(api_key="same")
    assert a._client is b._client
    assert created.count("same") == 1


def test_question_specs_hoisted(_clean_env, _restore_sdk):
    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            return _Resp(
                nouls={"needs_review": _Val(noul=0.9)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=1)},
            )

    _install_stub(Stub)
    e = eng.TriageEngine(api_key="k1")
    e.screen_diff_hunk("src/a.py", "d1")
    q1 = eng._get_triage_questions()
    e.screen_diff_hunk("src/a.py", "d2")
    q2 = eng._get_triage_questions()
    assert q1 is q2


def test_cache_threshold_and_backend_keying(_clean_env):
    e = eng.TriageEngine(api_key=None)
    r1 = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd"))
    r2 = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd"))
    assert r1 is r2  # cache hit
    # different threshold -> different key -> miss (new object)
    r3 = asyncio.run(
        jev_client.jev_screen_diff_async("src/a.py", "ddd", threshold=0.99)
    )
    assert r3 is not r1
    stats = jev_client.jev_cache_stats()
    assert stats["triage_hits"] >= 1
    assert stats["triage_misses"] >= 2


def test_cache_concurrent_access_safe(_clean_env):
    errs = []

    def _work(n):
        try:
            for i in range(50):
                asyncio.run(jev_client.jev_screen_diff_async(f"src/{n}.py", f"diff-{i % 5}"))
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=_work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs


def test_status_ttl_cache_no_hang(_clean_env):
    # no key -> immediate, no probe
    s1 = jev_client.jev_status()
    assert s1["configured"] is False
    assert s1["latency_ms"] is None
    s2 = jev_client.jev_status()
    assert s2 == s1


def test_batch_fanout_order(_clean_env):
    items = [(f"src/f{i}.py", f"diff-{i}") for i in range(10)]
    out = asyncio.run(jev_client.jev_screen_many_async(items, max_concurrency=3))
    assert [r.file_path for r in out] == [f for f, _ in items]
    assert all(r.should_review for r in out)


def test_sync_path_passes_sdk_timeout(_clean_env, _restore_sdk):
    captured = {}

    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            captured.update(kw)
            return _Resp(
                nouls={"needs_review": _Val(noul=0.9)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=1)},
            )

    _install_stub(Stub)
    os.environ["CODERAI_JEV_TIMEOUT_S"] = "2.5"
    e = eng.TriageEngine(api_key="k1")
    e.screen_diff_hunk("src/a.py", "d")
    assert captured.get("timeout") == 2.5


def test_gate_accept_threshold_change_invalidates_cache(_clean_env, _restore_sdk):
    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            return _Resp(
                nouls={
                    "is_actionable_bug": _Val(noul=0.80),
                    "is_speculative_or_nit": _Val(noul=0.10),
                    "will_developer_accept": _Val(noul=0.70),
                }
            )

    _install_stub(Stub)
    g1 = asyncio.run(
        jev_client.jev_gate_comment_async("a.py", "d", "c", api_key="k1")
    )
    assert g1.passed is True
    os.environ["CODERAI_JEV_ACCEPT_THRESHOLD"] = "0.95"
    g2 = asyncio.run(
        jev_client.jev_gate_comment_async("a.py", "d", "c", api_key="k1")
    )
    assert g2 is not g1
    assert g2.passed is False


def test_fallback_not_cached(_clean_env, _restore_sdk):
    class Boom:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, **kw):
            raise RuntimeError("sdk exploded")

    _install_stub(Boom)
    r1 = asyncio.run(
        jev_client.jev_screen_diff_async("src/a.py", "diff", api_key="k1", use_cache=True)
    )
    r2 = asyncio.run(
        jev_client.jev_screen_diff_async("src/a.py", "diff", api_key="k1", use_cache=True)
    )
    assert r1.should_review is True
    assert r2.should_review is True
    assert r1 is not r2
    stats = jev_client.jev_cache_stats()
    assert stats["triage_misses"] == 2
    assert stats["triage_hits"] == 0
    g1 = asyncio.run(
        jev_client.jev_gate_comment_async(
            "src/a.py", "diff", "c", api_key="k1", use_cache=True
        )
    )
    g2 = asyncio.run(
        jev_client.jev_gate_comment_async(
            "src/a.py", "diff", "c", api_key="k1", use_cache=True
        )
    )
    assert g1.passed is True
    assert g2.passed is True
    assert g1 is not g2
    stats = jev_client.jev_cache_stats()
    assert stats["gate_misses"] == 2
    assert stats["gate_hits"] == 0


def test_cross_key_isolation(_clean_env, _restore_sdk):
    class Stub:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            if questions is not None and "is_actionable_bug" in questions:
                return _Resp(
                    nouls={
                        "is_actionable_bug": _Val(noul=0.9),
                        "is_speculative_or_nit": _Val(noul=0.0),
                        "will_developer_accept": _Val(noul=0.9),
                    }
                )
            return _Resp(
                nouls={"needs_review": _Val(noul=0.9)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=1)},
            )

    _install_stub(Stub)
    r1 = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd", api_key="k1"))
    r2 = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd", api_key="k2"))
    assert r1 is not r2
    r1b = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd", api_key="k1"))
    assert r1b is r1
    r2b = asyncio.run(jev_client.jev_screen_diff_async("src/a.py", "ddd", api_key="k2"))
    assert r2b is r2
    g1 = asyncio.run(
        jev_client.jev_gate_comment_async("src/a.py", "ddd", "c", api_key="k1")
    )
    g2 = asyncio.run(
        jev_client.jev_gate_comment_async("src/a.py", "ddd", "c", api_key="k2")
    )
    assert g1 is not g2


def test_stale_gate_spec_after_sdk_swap(_clean_env, _restore_sdk):
    class StubA:
        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            if questions is not None and "is_actionable_bug" in questions:
                return _Resp(
                    nouls={
                        "is_actionable_bug": _Val(noul=0.10),
                        "is_speculative_or_nit": _Val(noul=0.90),
                        "will_developer_accept": _Val(noul=0.10),
                    }
                )
            return _Resp(
                nouls={"needs_review": _Val(noul=0.10)},
                choices={"change_type": _Val(choice="boilerplate")},
                scores={"priority": _Val(score=0)},
            )

    _install_stub(StubA)
    ea = eng.TriageEngine(api_key="k1")
    ea.screen_diff_hunk("src/a.py", "d")
    ga = ea.gate_candidate_comment("src/a.py", "d", "c")
    assert ga.passed is False

    class StubB:
        marker = "stub-B"

        def __init__(self, api_key=None, **kw):
            pass

        def system_one(self, state=None, questions=None, **kw):
            if questions is not None and "is_actionable_bug" in questions:
                qtypes = {k: type(v).__name__ for k, v in questions.items()}
                assert "Noul" in str(qtypes) or qtypes
                return _Resp(
                    nouls={
                        "is_actionable_bug": _Val(noul=0.99),
                        "is_speculative_or_nit": _Val(noul=0.0),
                        "will_developer_accept": _Val(noul=0.99),
                    }
                )
            return _Resp(
                nouls={"needs_review": _Val(noul=0.99)},
                choices={"change_type": _Val(choice="core_logic")},
                scores={"priority": _Val(score=2)},
            )

    stub_b = types.ModuleType("typesafe_sdk")
    stub_b.Noul = _Noul
    stub_b.Choice = _Choice
    stub_b.Score = _Score
    stub_b.TypeSafeClient = StubB
    # Swap WITHOUT reset: no reset_jev_question_cache / reset_jev_shared_clients /
    # reset_triage_engine here, so the engine must invalidate stale specs itself.
    sys.modules["typesafe_sdk"] = stub_b
    eb = eng.TriageEngine(api_key="k-swap-b")
    gb = eb.gate_candidate_comment("src/a.py", "d", "c")
    assert gb.passed is True
    assert gb.is_actionable_bug == 0.99
    rb = eb.screen_diff_hunk("src/a.py", "d")
    assert rb.risk == 0.99
