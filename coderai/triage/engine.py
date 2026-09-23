"""System 1 fast heuristic triage and precision gating engine using TypeSafe (Jev).

Provides sub-100ms diff screening and calibrated precision filtering to pair
with CoderAI's System 2 deep generative and reasoning loops.
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# API key for TypeSafe / Jev. Must come from env or explicit arg — never hardcoded.
# Supported vars: TYPESAFE_API_KEY, JEV_API_KEY (alias).
JEV_ENV_VARS = ("TYPESAFE_API_KEY", "JEV_API_KEY")

# Tunables (overridable via env): timeouts in seconds, thresholds 0..1.
JEV_DEFAULT_TIMEOUT_S = 3.0
JEV_DEFAULT_TRIAGE_THRESHOLD = 0.20
JEV_DEFAULT_GATE_THRESHOLD = 0.75
# Precision-gate sub-thresholds (previously literals 0.60 / 0.50 in gate logic).
JEV_DEFAULT_ACCEPT_THRESHOLD = 0.60
JEV_DEFAULT_SPECULATIVE_THRESHOLD = 0.50
# Minimum Choice confidence before a "documentation" label may suppress Tier-1 review.
JEV_DEFAULT_MIN_CONFIDENCE = 0.60
# Single authoritative payload budget (previously sliced in both client.py and here).
JEV_MAX_DIFF_CHARS = 10000

# Sync fallback scores (fail-closed Tier-1 risk / fail-open Tier-3 gate).
JEV_FALLBACK_RISK = 0.50
JEV_FALLBACK_GATE_SCORE = 0.80

# Bound on shared TypeSafeClient reuse (env-overridable via CODERAI_JEV_MAX_CLIENTS).
JEV_DEFAULT_MAX_CLIENTS = 16

JEV_MODEL_ID = "jev-system-one"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        return float(raw.strip())
    except (TypeError, ValueError, AttributeError):
        return default


def jev_triage_threshold(explicit: float | None = None) -> float:
    """Resolve Tier-1 risk threshold: explicit arg, else env, else default."""
    if explicit is not None:
        return float(explicit)
    return _env_float("CODERAI_JEV_TRIAGE_THRESHOLD", JEV_DEFAULT_TRIAGE_THRESHOLD)


def jev_gate_threshold(explicit: float | None = None) -> float:
    """Resolve Tier-3 is_bug threshold: explicit arg, else env, else default."""
    if explicit is not None:
        return float(explicit)
    return _env_float("CODERAI_JEV_GATE_THRESHOLD", JEV_DEFAULT_GATE_THRESHOLD)


def jev_accept_threshold(explicit: float | None = None) -> float:
    """Resolve Tier-3 acceptance threshold: explicit arg, else env, else default."""
    if explicit is not None:
        return float(explicit)
    return _env_float("CODERAI_JEV_ACCEPT_THRESHOLD", JEV_DEFAULT_ACCEPT_THRESHOLD)


def jev_speculative_threshold(explicit: float | None = None) -> float:
    """Resolve Tier-3 speculative ceiling: explicit arg, else env, else default."""
    if explicit is not None:
        return float(explicit)
    return _env_float("CODERAI_JEV_SPECULATIVE_THRESHOLD", JEV_DEFAULT_SPECULATIVE_THRESHOLD)


def jev_min_confidence(explicit: float | None = None) -> float:
    """Resolve Tier-1 Choice confidence floor: explicit arg, else env, else default."""
    if explicit is not None:
        return float(explicit)
    return _env_float("CODERAI_JEV_MIN_CONFIDENCE", JEV_DEFAULT_MIN_CONFIDENCE)


def jev_max_diff_chars() -> int:
    """Authoritative diff payload budget, env-overridable."""
    try:
        raw = os.environ.get("CODERAI_JEV_MAX_DIFF_CHARS")
        if raw is not None and raw.strip():
            return max(512, int(float(raw.strip())))
    except (TypeError, ValueError, AttributeError):
        pass
    return JEV_MAX_DIFF_CHARS


def jev_max_clients() -> int:
    """Bound on shared-client cache size, env-overridable."""
    try:
        raw = os.environ.get("CODERAI_JEV_MAX_CLIENTS")
        if raw is not None and raw.strip():
            return max(1, int(float(raw.strip())))
    except (TypeError, ValueError, AttributeError):
        pass
    return JEV_DEFAULT_MAX_CLIENTS


def jev_sync_timeout_s(explicit: float | None = None) -> float:
    """Sync SDK per-call timeout, env-overridable (bounds system_one).

    Mirrors client ``jev_timeout_s()``: explicit arg wins, else the
    ``CODERAI_JEV_TIMEOUT_S`` env var, else default; floored at 0.5s.
    """
    if explicit is not None:
        try:
            return max(0.5, float(explicit))
        except (TypeError, ValueError):
            return JEV_DEFAULT_TIMEOUT_S
    try:
        return max(0.5, float(os.getenv("CODERAI_JEV_TIMEOUT_S", str(JEV_DEFAULT_TIMEOUT_S))))
    except (TypeError, ValueError):
        return JEV_DEFAULT_TIMEOUT_S


def truncate_diff(diff_hunk: str) -> str:
    """Single authoritative truncation point for Jev payloads."""
    if not diff_hunk:
        return diff_hunk
    budget = jev_max_diff_chars()
    if len(diff_hunk) <= budget:
        return diff_hunk
    return diff_hunk[:budget]


def resolve_jev_api_key(explicit: str | None = None) -> str | None:
    """Resolve Jev/TypeSafe key from explicit arg then env. Returns None if unset."""
    if explicit and explicit.strip():
        return explicit.strip()
    for var in JEV_ENV_VARS:
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    return None


def is_jev_configured(api_key: str | None = None) -> bool:
    """True if a Jev key is available via arg or env."""
    return resolve_jev_api_key(api_key) is not None

# Known documentation or boilerplate file extensions
# NOTE: ".json-lock" was removed (dead entry: splitext("x.json-lock") yields
# ".json-lock", which matches no real file; ".lock" already covers lockfiles).
# This set is disjoint from CODE_EXTENSIONS by design — see below.
DOC_OR_BOILERPLATE_EXTENSIONS = frozenset({
    ".lock", ".svg", ".png", ".jpg", ".jpeg",
    ".md", ".rst", ".txt", ".adoc",
})

# Authoritative post-SDK code-file policy. Disjoint from DOC_OR_BOILERPLATE_EXTENSIONS:
# pre-SDK bypass skips docs/assets; post-SDK this set forces should_review=True
# even when risk is low (fail-closed for source/config). ".json/.yml/.xml/
# .properties" live here (not in DOC set), so e.g. package-lock.json (ext
# ".json") is never bypassed — it is always reviewed.
CODE_EXTENSIONS = frozenset({
    ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".kt", ".kts", ".swift", ".m",
    ".rb", ".php", ".scala", ".sql", ".sh", ".bash", ".zsh", ".ps1",
    ".properties", ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".tf", ".gradle",
})


@dataclass(frozen=True)
class TriageResult:
    """Result of Tier 1 fast diff screening."""
    file_path: str
    risk: float
    category: str
    priority: int
    should_review: bool
    reason: str = ""
    category_confidence: float | None = None
    category_probabilities: Mapping[str, float] | None = field(default=None, hash=False)
    priority_expected: float | None = None
    priority_confidence: float | None = None
    priority_probabilities: Mapping[int, float] | None = field(default=None, hash=False)
    truncated: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class GateResult:
    """Result of Tier 3 pre-flight precision gating."""
    is_actionable_bug: float
    will_developer_accept: float
    passed: bool
    reason: str = ""
    is_speculative: float | None = None
    truncated: bool = False


CHANGE_TYPES: dict[str, str] = {
    "core_logic": "Business logic, algorithms, DB queries, auth, payment logic, concurrency, or localization.",
    "boilerplate": "Imports, renames, configuration constants, logging additions.",
    "documentation": "Comments, docstrings, typing hints only.",
}
PRIORITY_LEVELS: tuple[str, ...] = ("Trivial", "Low", "Medium", "Critical")


def _probability(value: Any, name: str) -> float:
    p = float(value)
    if not math.isfinite(p) or not 0.0 <= p <= 1.0:
        raise ValueError(f"{name} out of range: {value!r}")
    return p


def _read_choice(answer: Any, name: str, labels: Mapping[str, Any]) -> tuple[str, float, dict[str, float] | None]:
    label = str(answer.choice)
    if label not in labels:
        raise ValueError(f"{name} returned unknown label {label!r}")
    raw_probs = getattr(answer, "probabilities", None)
    probs = {str(k): _probability(v, f"{name}[{k}]") for k, v in raw_probs.items()} if raw_probs else None
    conf = getattr(answer, "confidence", None)
    if conf is None and probs is not None:
        conf = probs.get(label)
    return label, _probability(conf, f"{name}.confidence") if conf is not None else 0.0, probs


def _read_score(answer: Any, name: str, levels: int) -> tuple[int, float, float | None, dict[int, float] | None]:
    expected = float(answer.score)
    if not math.isfinite(expected):
        raise ValueError(f"{name} is not finite: {answer.score!r}")
    raw_probs = getattr(answer, "probabilities", None)
    probs = {int(k): _probability(v, f"{name}[{k}]") for k, v in raw_probs.items()} if raw_probs else None
    level = max(probs, key=probs.__getitem__) if probs else round(expected)
    conf = getattr(answer, "confidence", None)
    return (
        min(max(int(level), 0), levels - 1),
        expected,
        _probability(conf, f"{name}.confidence") if conf is not None else None,
        probs,
    )


def _log_engine_error(tier: str, file_path: str, exc: BaseException) -> None:
    try:
        from coderai.utils.logging import logger

        logger.warning(f"Jev {tier} error: file={file_path} {type(exc).__name__}: {str(exc)[:300]}")
    except Exception:
        pass


# --- Lazy-once question specs (hoisted off the per-call hot path) ---
# Rebuilt once per typesafe_sdk module identity so stub injection in tests
# (sys.modules["typesafe_sdk"] = stub) invalidates the cache.
_questions_lock = threading.Lock()
_triage_questions: dict[str, Any] | None = None
_gate_questions: dict[str, Any] | None = None
_triage_sdk_id: int | None = None
_gate_sdk_id: int | None = None


def _current_sdk_id() -> int | None:
    import sys as _sys

    mod = _sys.modules.get("typesafe_sdk")
    return id(mod) if mod is not None else None


def _get_triage_questions() -> dict[str, Any]:
    """Return cached triage question spec, building once per SDK identity."""
    global _triage_questions, _triage_sdk_id
    from typesafe_sdk import Choice as _Choice
    from typesafe_sdk import Noul as _Noul
    from typesafe_sdk import Score as _Score

    sid = _current_sdk_id()
    with _questions_lock:
        if _triage_questions is None or _triage_sdk_id != sid:
            _triage_questions = {
                "needs_review": _Noul(
                    instructions="Does this code change introduce potential logic bugs, security risks, or concurrency issues?"
                ),
                "change_type": _Choice(
                    instructions="What is the functional nature of this diff?",
                    criteria=dict(CHANGE_TYPES),
                ),
                "priority": _Score(
                    instructions="Rate urgency of review",
                    criteria=list(PRIORITY_LEVELS),
                ),
            }
            # Do not clobber a separately cached gate spec built under the same SDK.
            _triage_sdk_id = sid
        return _triage_questions


def _get_gate_questions() -> dict[str, Any]:
    """Return cached gate question spec, building once per SDK identity."""
    global _gate_questions, _gate_sdk_id
    from typesafe_sdk import Noul as _Noul

    sid = _current_sdk_id()
    with _questions_lock:
        if _gate_questions is None or _gate_sdk_id != sid:
            _gate_questions = {
                "is_actionable_bug": _Noul(
                    instructions=(
                        "Does this comment identify a genuine defect, functional bug, security issue, "
                        "concurrency flaw, API contract violation, resource leak, documentation discrepancy, "
                        "locale error, or valid code convention issue in the PR? "
                        "Score < 0.4 ONLY if it is an ungrounded hallucination, impossible edge case, "
                        "or subjective architectural complaint."
                    )
                ),
                "is_speculative_or_nit": _Noul(
                    instructions=(
                        "Is this comment an ungrounded hallucination, an impossible edge case (such as "
                        "division by zero or NPE under impossible conditions), or a critique of deliberate "
                        "architectural design? Score 1.0 if speculative/hallucinated, 0.0 if a grounded defect."
                    )
                ),
                "will_developer_accept": _Noul(
                    instructions=(
                        "Would a senior software engineer accept this PR comment as an accurate, actionable, "
                        "and valuable review finding rather than reject it as noise or pedantic micromanagement?"
                    )
                ),
            }
            _gate_sdk_id = sid
        return _gate_questions


def reset_jev_question_cache() -> None:
    """Clear cached question specs (mainly for tests that stub typesafe_sdk)."""
    global _triage_questions, _gate_questions, _triage_sdk_id, _gate_sdk_id
    with _questions_lock:
        _triage_questions = None
        _gate_questions = None
        _triage_sdk_id = None
        _gate_sdk_id = None


# --- Shared TypeSafeClient reuse (one per api_key, invalidated on SDK swap) ---
_shared_client_lock = threading.Lock()
_shared_clients: dict[str | None, Any] = {}
_shared_clients_sdk_id: int | None = None


def _get_shared_client(api_key: str | None) -> Any | None:
    """Return a shared TypeSafeClient for api_key, or None if unavailable."""
    global _shared_clients_sdk_id
    if not api_key:
        return None
    try:
        from typesafe_sdk import TypeSafeClient as _Client
    except Exception:
        return None
    # SDK default is 2 retries + backoff (30s budget); the async wrapper abandons the call at
    # jev_timeout_s(), so retries would only pin pool workers after the caller has fallen back.
    try:
        from typesafe_sdk import RetryPolicy as _RetryPolicy

        client_kwargs: dict[str, Any] = {"retry": _RetryPolicy(max_retries=0)}
    except Exception:
        client_kwargs = {}
    sid = _current_sdk_id()
    with _shared_client_lock:
        if _shared_clients_sdk_id != sid:
            _shared_clients.clear()
            _shared_clients_sdk_id = sid
        client = _shared_clients.get(api_key)
        if client is not None:
            # LRU touch: reinsert to mark as most-recently used.
            _shared_clients[api_key] = _shared_clients.pop(api_key)
            return client
        try:
            client = _Client(api_key=api_key, **client_kwargs)
        except Exception:
            return None
        cap = jev_max_clients()
        while len(_shared_clients) >= cap:
            try:
                _shared_clients.pop(next(iter(_shared_clients)))
            except StopIteration:
                break
        _shared_clients[api_key] = client
        return client


def reset_jev_shared_clients() -> None:
    """Drop shared clients (mainly for tests)."""
    global _shared_clients_sdk_id
    with _shared_client_lock:
        _shared_clients.clear()
        _shared_clients_sdk_id = None


class TriageEngine:
    """Kahneman System 1 engine for code review triage and precision gating."""

    def __init__(self, api_key: str | None = None):
        self.api_key = resolve_jev_api_key(api_key)
        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        """Initialize TypeSafeClient if typesafe_sdk is installed (shared per key)."""
        self._client = _get_shared_client(self.api_key)

    @property
    def is_available(self) -> bool:
        """True if the TypeSafe client is initialized and ready."""
        return self._client is not None

    def screen_diff_hunk(
        self,
        file_path: str,
        diff_hunk: str,
        threshold: float | None = None,
        timeout_s: float | None = None,
    ) -> TriageResult:
        """Tier 1: Fast Diff Triage (System 1).

        Screens a diff hunk in ~80ms to determine whether it warrants a deep LLM review.
        Fail-closed: every fallback returns should_review=True.
        """
        # Fast local heuristic check for obvious docs / assets
        ext = os.path.splitext(file_path)[1].lower()
        if ext in DOC_OR_BOILERPLATE_EXTENSIONS and not any(k in file_path.lower() for k in ("message", "locale", "i18n")):
            return TriageResult(
                file_path=file_path,
                risk=0.05,
                category="documentation",
                priority=0,
                should_review=False,
                reason="Bypassed: documentation/asset file",
            )

        if not self._client:
            # Fallback heuristic when SDK is unavailable (fail-closed)
            return TriageResult(
                file_path=file_path,
                risk=JEV_FALLBACK_RISK,
                category="core_logic",
                priority=1,
                should_review=True,
                reason="Fallback heuristic: TypeSafe client unavailable",
            )

        try:
            triage_questions = _get_triage_questions()

            resp = self._client.system_one(
                state={"file": file_path, "diff": truncate_diff(diff_hunk)},
                questions=triage_questions,
                timeout=jev_sync_timeout_s(timeout_s),
            )

            risk = _probability(resp.nouls["needs_review"].noul, "needs_review")
            category, category_conf, category_probs = _read_choice(
                resp.choices["change_type"], "change_type", CHANGE_TYPES
            )
            priority, priority_expected, priority_conf, priority_probs = _read_score(
                resp.scores["priority"], "priority", len(PRIORITY_LEVELS)
            )

            # Core review gating logic: preserve all source code and properties files
            triage_cutoff = jev_triage_threshold(threshold)
            is_code_file = ext in CODE_EXTENSIONS
            truncated = len(diff_hunk) > jev_max_diff_chars()
            uncertain = category_conf < jev_min_confidence()
            doc_suppresses = category == "documentation" and not uncertain
            should_review = (
                is_code_file or truncated or (risk >= triage_cutoff and not doc_suppresses)
            )
            return TriageResult(
                file_path=file_path,
                risk=risk,
                category=category,
                priority=priority,
                should_review=should_review,
                reason=(
                    f"Jev screening: risk={risk:.2f}, category={category} ({category_conf:.2f}), "
                    f"priority={priority} (E={priority_expected:.2f})"
                    + (", truncated" if truncated else "")
                    + (", low-confidence category" if uncertain else "")
                ),
                category_confidence=category_conf,
                category_probabilities=category_probs,
                priority_expected=priority_expected,
                priority_confidence=priority_conf,
                priority_probabilities=priority_probs,
                truncated=truncated,
                uncertain=uncertain,
            )
        except Exception as e:
            _log_engine_error("triage", file_path, e)
            return TriageResult(
                file_path=file_path,
                risk=JEV_FALLBACK_RISK,
                category="core_logic",
                priority=1,
                should_review=True,
                reason=f"Triage error fallback: {e}",
            )

    def gate_candidate_comment(
        self,
        file_path: str,
        diff_hunk: str,
        comment: str,
        threshold: float | None = None,
        timeout_s: float | None = None,
    ) -> GateResult:
        """Tier 3: Pre-Flight Precision Gate (System 1).

        Validates whether a drafted comment is a genuine functional bug and has
        high predicted developer acceptance probability before reporting.
        Fail-open: every fallback returns passed=True, as does a truncated diff
        (the model cannot confirm a comment is grounded in lines it never saw).
        Low scores on a complete diff still reject: this gate trades recall for precision.
        """
        gate_cutoff = jev_gate_threshold(threshold)
        if not self._client:
            return GateResult(
                is_actionable_bug=JEV_FALLBACK_GATE_SCORE,
                will_developer_accept=JEV_FALLBACK_GATE_SCORE,
                passed=True,
                reason="Fallback: TypeSafe client unavailable, comment passed",
            )

        try:
            gate_questions = _get_gate_questions()

            resp = self._client.system_one(
                state={
                    "file": file_path,
                    "diff": truncate_diff(diff_hunk),
                    "suggested_review_comment": truncate_diff(comment),
                },
                questions=gate_questions,
                timeout=jev_sync_timeout_s(timeout_s),
            )

            is_bug = _probability(resp.nouls["is_actionable_bug"].noul, "is_actionable_bug")
            is_speculative = _probability(
                resp.nouls["is_speculative_or_nit"].noul, "is_speculative_or_nit"
            )
            accept_prob = _probability(
                resp.nouls["will_developer_accept"].noul, "will_developer_accept"
            )

            # Strict precision gating: must be high-confidence defect, high acceptance, and low speculative noise
            accept_cutoff = jev_accept_threshold()
            spec_ceiling = jev_speculative_threshold()
            truncated = len(diff_hunk) > jev_max_diff_chars()
            passed = truncated or (
                (is_bug >= gate_cutoff)
                and (accept_prob >= accept_cutoff)
                and (is_speculative <= spec_ceiling)
            )
            return GateResult(
                is_actionable_bug=is_bug,
                will_developer_accept=accept_prob,
                passed=passed,
                reason=(
                    f"Jev gate: is_bug={is_bug:.2f}, spec={is_speculative:.2f}, "
                    f"accept_prob={accept_prob:.2f} (threshold={gate_cutoff:.2f}, passed={passed})"
                    + (", truncated diff" if truncated else "")
                ),
                is_speculative=is_speculative,
                truncated=truncated,
            )
        except Exception as e:
            _log_engine_error("gate", file_path, e)
            return GateResult(
                is_actionable_bug=JEV_FALLBACK_GATE_SCORE,
                will_developer_accept=JEV_FALLBACK_GATE_SCORE,
                passed=True,
                reason=f"Gate error fallback: {e}",
            )


# Default singleton instance
_default_engine: TriageEngine | None = None
_engine_lock = threading.Lock()


def get_triage_engine(api_key: str | None = None) -> TriageEngine:
    """Get or instantiate the global TriageEngine (thread-safe, rotation-aware).

    The singleton is keyed by *resolved* key (explicit arg wins, else env), so
    key rotation is honored for both explicit and implicit (None) callers —
    an implicit call after an explicit-key call re-resolves env instead of
    leaking the old explicit key.
    """
    global _default_engine
    with _engine_lock:
        if _default_engine is None:
            _default_engine = TriageEngine(api_key=api_key)
            return _default_engine
        resolved = resolve_jev_api_key(api_key)
        if resolved != _default_engine.api_key:
            _default_engine = TriageEngine(api_key=api_key)
        return _default_engine


def reset_triage_engine() -> None:
    """Reset the global singleton (mainly for tests)."""
    global _default_engine
    with _engine_lock:
        _default_engine = None
