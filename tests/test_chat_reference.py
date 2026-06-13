"""Coverage for coderAI/bridge/chat_reference.py — /show topic text builders."""

import asyncio
from types import SimpleNamespace

import pytest

from coderAI import __version__
from coderAI.bridge import chat_reference as cr


# ── _truncate ───────────────────────────────────────────────────────────


def test_truncate_passthrough_and_cut():
    assert cr._truncate("short") == "short"
    long = "x" * 20_000
    out = cr._truncate(long)
    assert len(out) < len(long)
    assert "truncated" in out


# ── _mask_keys ──────────────────────────────────────────────────────────


def test_mask_keys_long_short_and_absent():
    data = {
        "openai_api_key": "sk-1234567890abcdef",  # long → head...tail
        "groq_api_key": "short",  # non-empty, <=12 → "(set)"
        "anthropic_api_key": None,  # falsy → untouched
        "other": "keep",
    }
    out = cr._mask_keys(data)
    assert out["openai_api_key"].startswith("sk-12345")
    assert out["openai_api_key"].endswith("cdef")
    assert "..." in out["openai_api_key"]
    assert out["groq_api_key"] == "(set)"
    assert out["anthropic_api_key"] is None
    assert out["other"] == "keep"


# ── build_*_text (config-backed) ────────────────────────────────────────


def test_build_models_text():
    text = cr.build_models_text()
    assert "Models & providers" in text
    assert "OpenAI" in text
    assert "Saved default model" in text


def test_build_cost_text_includes_pricing():
    text = cr.build_cost_text()
    assert "Reference pricing" in text
    # Either a priced or a free (local) model line is present.
    assert "in /" in text or "free (local)" in text


def test_build_cost_text_with_budget(monkeypatch):
    cfg = cr.config_manager.load()
    monkeypatch.setattr(cfg, "budget_limit", 5.0, raising=False)
    monkeypatch.setattr(cr.config_manager, "load", lambda: cfg)
    text = cr.build_cost_text()
    assert "Budget limit" in text


def test_build_system_text():
    text = cr.build_system_text()
    assert "System status" in text
    assert "API keys" in text
    assert "sessions on disk:" in text


def test_build_config_text_masks_keys():
    agent = SimpleNamespace(
        config=SimpleNamespace(
            model_dump=lambda exclude_none: {
                "openai_api_key": "sk-abcdefghijklmnop",
                "default_model": "gpt-5.4",
            }
        )
    )
    text = cr.build_config_text(agent)
    assert "Effective configuration" in text
    assert "default_model: gpt-5.4" in text
    assert "sk-abcde...mnop" in text  # masked head...tail, not full key
    assert "ghijkl" not in text


# ── _flatten_model_info ─────────────────────────────────────────────────


def test_flatten_model_info_nested():
    obj = {
        "name": "m",
        "caps": {"vision": True, "tools": ["a", "b"]},
        "list": [{"x": 1}, "scalar"],
    }
    lines = cr._flatten_model_info(obj)
    joined = "\n".join(lines)
    assert "name: m" in joined
    assert "vision: True" in joined
    assert "- a" in joined  # list scalar
    assert "[0]:" in joined  # list of dict index marker
    # Top-level scalar hits the non-dict/non-list branch.
    assert cr._flatten_model_info("justscalar") == ["justscalar"]


# ── build_info_text ─────────────────────────────────────────────────────


def _info_agent(model_info, tools, *, info_raises=False, tools_raise=False):
    def get_model_info():
        if info_raises:
            raise RuntimeError("no info")
        return model_info

    class _Tools:
        def get_all(self):
            if tools_raise:
                raise RuntimeError("no tools")
            return tools

    return SimpleNamespace(
        model="gpt-5.4",
        provider=SimpleNamespace(),
        get_model_info=get_model_info,
        tools=_Tools(),
    )


def test_build_info_text_happy():
    tools = [SimpleNamespace(name=f"t{i}", description="does a thing") for i in range(3)]
    agent = _info_agent({"family": "gpt", "context": 200000}, tools)
    text = cr.build_info_text(agent)
    assert f"CoderAI {__version__}" in text
    assert "model:    gpt-5.4" in text
    assert "t0 — does a thing" in text


def test_build_info_text_truncates_long_desc_and_many_tools():
    tools = [SimpleNamespace(name=f"t{i}", description="d" * 100) for i in range(60)]
    agent = _info_agent({"k": "v"}, tools)
    text = cr.build_info_text(agent)
    assert "…" in text  # long description ellipsis
    assert "and 12 more" in text  # 60 tools - 48 shown


def test_build_info_text_handles_errors():
    agent = _info_agent({}, [], info_raises=True, tools_raise=True)
    text = cr.build_info_text(agent)
    assert "could not load" in text
    assert "could not list" in text


# ── build_tasks_text (async) ────────────────────────────────────────────


def test_build_tasks_text_failure(monkeypatch):
    async def fake_execute(self, *a, **k):
        return {"success": False, "error": "db locked"}

    monkeypatch.setattr("coderAI.tools.tasks.ManageTasksTool.execute", fake_execute)
    text = asyncio.run(cr.build_tasks_text("/tmp/proj"))
    assert "could not load" in text
    assert "db locked" in text


def test_build_tasks_text_with_buckets(monkeypatch):
    async def fake_execute(self, *a, **k):
        return {
            "success": True,
            "summary": "3 tasks",
            "in_progress": [{"id": 1, "title": "Build", "description": "the thing"}],
            "pending": [{"id": 2, "title": "Plan"}],
            "completed": [],
        }

    monkeypatch.setattr("coderAI.tools.tasks.ManageTasksTool.execute", fake_execute)
    text = asyncio.run(cr.build_tasks_text("/tmp/proj"))
    assert "3 tasks" in text
    assert "In progress:" in text
    assert "[1] Build — the thing" in text
    assert "Pending:" in text
    assert "[2] Plan" in text


# ── resolve_reference_text dispatch ─────────────────────────────────────


def test_resolve_reference_text_dispatch():
    agent = SimpleNamespace(
        config=SimpleNamespace(model_dump=lambda exclude_none: {"default_model": "m"}),
        model="gpt-5.4",
        provider=SimpleNamespace(),
        get_model_info=lambda: {"k": "v"},
        tools=SimpleNamespace(get_all=lambda: []),
    )
    assert cr.resolve_reference_text("version", agent) == f"CoderAI {__version__}"
    assert "Models & providers" in cr.resolve_reference_text("providers", agent)
    assert "Reference pricing" in cr.resolve_reference_text("pricing", agent)
    assert "System status" in cr.resolve_reference_text("diag", agent)
    assert "Effective configuration" in cr.resolve_reference_text("config", agent)
    assert f"CoderAI {__version__}" in cr.resolve_reference_text("info", agent)


def test_resolve_reference_text_unknown_raises():
    with pytest.raises(ValueError, match="Unknown topic"):
        cr.resolve_reference_text("bogus", SimpleNamespace())
