"""Unified model registry — single source of truth for all providers.

Option A strict catalog: 3 tiers per provider (frontier / mid / small).
Every picker, factory route, pricing and reasoning check imports from here
so no other file hard-codes a model list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coderAI.llm.anthropic import AnthropicProvider
from coderAI.llm.base import LLMProvider
from coderAI.llm.deepseek import DeepSeekProvider
from coderAI.llm.gemini import GeminiProvider
from coderAI.llm.groq import GroqProvider
from coderAI.llm.lmstudio import LMStudioProvider
from coderAI.llm.meta import MetaProvider
from coderAI.llm.ollama import OllamaProvider
from coderAI.llm.openai import OpenAIProvider

Tier = Literal["frontier", "mid", "small", "custom"]
ReasoningMode = Literal[
    "none", "openai-effort", "anthropic-adaptive", "deepseek-thinking", "meta-effort"
]


@dataclass(frozen=True)
class ModelSpec:
    provider_cls: type[LLMProvider]
    id: str  # canonical API ID (lower-case)
    label: str  # human short label (Sol, Terra…)
    tier: Tier
    supports_reasoning: bool
    reasoning_mode: ReasoningMode
    context_window: int
    input_price: float  # USD per 1M input
    output_price: float
    aliases: tuple[str, ...] = ()
    requirement: str = ""  # credential hint for UI

    @property
    def provider(self) -> str:
        """Stable provider identifier derived from the construction class."""
        return self.provider_cls.PROVIDER_ID


# ---------------------------------------------------------------------------
# Strict Option-A catalog — verified July 2026
# ---------------------------------------------------------------------------

ALL_SPECS: list[ModelSpec] = [
    # OpenAI — GPT-5.6 Sol/Terra/Luna — platform.openai.com/docs/pricing
    ModelSpec(
        provider_cls=OpenAIProvider,
        id="gpt-5.6-sol",
        label="Sol",
        tier="frontier",
        supports_reasoning=True,
        reasoning_mode="openai-effort",
        context_window=1_050_000,
        input_price=5.00,
        output_price=30.00,
        aliases=("gpt-5.6", "sol", "gpt-sol"),
        requirement="OpenAI API key",
    ),
    ModelSpec(
        provider_cls=OpenAIProvider,
        id="gpt-5.6-terra",
        label="Terra",
        tier="mid",
        supports_reasoning=True,
        reasoning_mode="openai-effort",
        context_window=1_050_000,
        input_price=2.00,
        output_price=12.00,
        aliases=("terra", "gpt-terra"),
        requirement="OpenAI API key",
    ),
    ModelSpec(
        provider_cls=OpenAIProvider,
        id="gpt-5.6-luna",
        label="Luna",
        tier="small",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=400_000,
        input_price=0.20,
        output_price=1.20,
        aliases=("luna", "gpt-luna"),
        requirement="OpenAI API key",
    ),
    # Anthropic — Fable 5 / Opus 5 / Sonnet 5
    ModelSpec(
        provider_cls=AnthropicProvider,
        id="claude-fable-5",
        label="Fable 5",
        tier="frontier",
        supports_reasoning=True,
        reasoning_mode="anthropic-adaptive",
        context_window=1_000_000,
        input_price=10.00,
        output_price=50.00,
        aliases=("claude-5-fable", "fable", "claude-fable"),
        requirement="Anthropic API key",
    ),
    ModelSpec(
        provider_cls=AnthropicProvider,
        id="claude-opus-5",
        label="Opus 5",
        tier="mid",
        supports_reasoning=True,
        reasoning_mode="anthropic-adaptive",
        context_window=1_000_000,
        input_price=5.00,
        output_price=25.00,
        aliases=("opus", "claude-opus", "claude-5-opus"),
        requirement="Anthropic API key",
    ),
    ModelSpec(
        provider_cls=AnthropicProvider,
        id="claude-sonnet-5",
        label="Sonnet 5",
        tier="small",
        supports_reasoning=True,
        reasoning_mode="anthropic-adaptive",
        context_window=1_000_000,
        input_price=3.00,
        output_price=15.00,
        aliases=("sonnet", "claude-sonnet", "claude-5-sonnet"),
        requirement="Anthropic API key",
    ),
    # Gemini — 3.5 Flash / 3.6 Flash / 3.1 Flash-Lite
    ModelSpec(
        provider_cls=GeminiProvider,
        id="gemini-3.5-flash",
        label="3.5 Flash",
        tier="frontier",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=1_048_576,
        input_price=1.50,
        output_price=9.00,
        requirement="Gemini API key",
    ),
    ModelSpec(
        provider_cls=GeminiProvider,
        id="gemini-3.6-flash",
        label="3.6 Flash",
        tier="mid",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=1_048_576,
        input_price=1.50,
        output_price=7.50,
        requirement="Gemini API key",
    ),
    ModelSpec(
        provider_cls=GeminiProvider,
        id="gemini-3.1-flash-lite",
        label="3.1 Flash-Lite",
        tier="small",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=1_048_576,
        input_price=0.25,
        output_price=1.50,
        requirement="Gemini API key",
    ),
    # DeepSeek — V4 Pro / V4 Flash / V3-chat (compat alias to Flash)
    ModelSpec(
        provider_cls=DeepSeekProvider,
        id="deepseek-v4-pro",
        label="V4 Pro",
        tier="frontier",
        supports_reasoning=True,
        reasoning_mode="deepseek-thinking",
        context_window=1_000_000,
        input_price=1.74,
        output_price=3.48,
        requirement="DeepSeek API key",
    ),
    ModelSpec(
        provider_cls=DeepSeekProvider,
        id="deepseek-v4-flash",
        label="V4 Flash",
        tier="mid",
        supports_reasoning=True,
        reasoning_mode="deepseek-thinking",
        context_window=1_000_000,
        input_price=0.14,
        output_price=0.28,
        aliases=("deepseek-flash",),
        requirement="DeepSeek API key",
    ),
    ModelSpec(
        provider_cls=DeepSeekProvider,
        id="deepseek-chat",
        label="Chat",
        tier="small",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=1_000_000,
        input_price=0.27,
        output_price=1.10,
        aliases=("deepseek-v3", "deepseek-v3.2"),
        requirement="DeepSeek API key",
    ),
    # Groq — GPT-OSS 120B / Llama 4 Scout / GPT-OSS 20B
    ModelSpec(
        provider_cls=GroqProvider,
        id="openai/gpt-oss-120b",
        label="GPT-OSS 120B",
        tier="frontier",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=131_072,
        input_price=0.15,
        output_price=0.60,
        aliases=("gpt-oss-120b",),
        requirement="Groq API key",
    ),
    ModelSpec(
        provider_cls=GroqProvider,
        id="meta-llama/llama-4-scout-17b-16e-instruct",
        label="Llama 4 Scout",
        tier="mid",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=131_072,
        input_price=0.59,
        output_price=0.79,
        aliases=("llama-4-scout", "llama-4-scout-17b"),
        requirement="Groq API key",
    ),
    ModelSpec(
        provider_cls=GroqProvider,
        id="openai/gpt-oss-20b",
        label="GPT-OSS 20B",
        tier="small",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=131_072,
        input_price=0.075,
        output_price=0.30,
        aliases=("gpt-oss-20b",),
        requirement="Groq API key",
    ),
    # Meta — Muse Spark 1.2 family
    ModelSpec(
        provider_cls=MetaProvider,
        id="muse-spark-1.2",
        label="Spark 1.2",
        tier="frontier",
        supports_reasoning=True,
        reasoning_mode="meta-effort",
        context_window=1_048_576,
        input_price=1.25,
        output_price=4.25,
        aliases=("muse-spark", "muse"),
        requirement="Meta Model API key",
    ),
    ModelSpec(
        provider_cls=MetaProvider,
        id="muse-spark-1.1",
        label="Spark 1.1",
        tier="mid",
        supports_reasoning=True,
        reasoning_mode="meta-effort",
        context_window=1_048_576,
        input_price=1.00,
        output_price=3.50,
        requirement="Meta Model API key",
    ),
    ModelSpec(
        provider_cls=MetaProvider,
        id="muse-spark-1.2-contributor",
        label="Spark 1.2 Contributor",
        tier="small",
        supports_reasoning=True,
        reasoning_mode="meta-effort",
        context_window=1_048_576,
        input_price=0.80,
        output_price=2.50,
        aliases=("muse-contributor",),
        requirement="Meta Model API key",
    ),
    # Local — not tiered, custom
    ModelSpec(
        provider_cls=LMStudioProvider,
        id="lmstudio",
        label="LM Studio",
        tier="custom",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=128_000,
        input_price=0.0,
        output_price=0.0,
        requirement="LM Studio running locally",
    ),
    ModelSpec(
        provider_cls=OllamaProvider,
        id="ollama",
        label="Ollama",
        tier="custom",
        supports_reasoning=False,
        reasoning_mode="none",
        context_window=128_000,
        input_price=0.0,
        output_price=0.0,
        requirement="Ollama running locally",
    ),
]

_ID_TO_SPEC = {s.id.lower(): s for s in ALL_SPECS}
_ALIAS_TO_ID: dict[str, str] = {}
for s in ALL_SPECS:
    for a in s.aliases:
        _ALIAS_TO_ID[a.lower()] = s.id.lower()
# also map id itself as alias for uniform lookup
for s in ALL_SPECS:
    _ALIAS_TO_ID[s.id.lower()] = s.id.lower()

# Legacy compat aliases that don't deserve a tier but must still resolve
_LEGACY_ALIASES: dict[str, str] = {
    # Anthropic legacy
    "claude-4-sonnet": "claude-sonnet-5",
    "claude-4-opus": "claude-opus-5",
    "claude-4-haiku": "claude-sonnet-5",
    "claude-4.6-sonnet": "claude-sonnet-5",
    "claude-4.7-opus": "claude-opus-5",
    "claude-4.8-opus": "claude-opus-5",
    "claude-4.5-haiku": "claude-sonnet-5",
    "claude-3.5-sonnet": "claude-sonnet-5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-5",
    "claude-3-5-sonnet-20241022": "claude-sonnet-5",
    "claude-3-5-haiku-20241022": "claude-sonnet-5",
    "claude-3-opus-20240229": "claude-opus-5",
    "claude-haiku-4-5-20251001": "claude-sonnet-5",
    "haiku": "claude-sonnet-5",
    # OpenAI legacy
    "gpt-5": "gpt-5.6-sol",
    "gpt-5.4": "gpt-5.6-terra",
    "gpt-5.4-mini": "gpt-5.6-luna",
    "gpt-5.4-nano": "gpt-5.6-luna",
    "o1": "gpt-5.6-sol",
    "o1-mini": "gpt-5.6-luna",
    "o1-pro": "gpt-5.6-sol",
    "o3-mini": "gpt-5.6-luna",
    # Gemini legacy
    "gemini-2.5-flash": "gemini-3.6-flash",
    "gemini-2.5-pro": "gemini-3.5-flash",
    "gemini-2.0-flash": "gemini-3.1-flash-lite",
    "gemini-2.0-pro": "gemini-3.1-pro",
    "gemini-1.5-flash": "gemini-3.1-flash-lite",
    "gemini-1.5-pro": "gemini-3.1-pro",
    # DeepSeek legacy
    "deepseek-reasoner": "deepseek-v4-pro",
    "deepseek-r1": "deepseek-v4-pro",
    "deepseek-v3": "deepseek-chat",
    # Groq legacy
    "llama3-70b-8192": "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama3-8b-8192": "openai/gpt-oss-20b",
    "mixtral-8x7b-32768": "openai/gpt-oss-120b",
    "gemma-7b-it": "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile": "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    # Meta legacy
    "muse": "muse-spark-1.2",
    "muse-spark": "muse-spark-1.2",
}

for k, v in _LEGACY_ALIASES.items():
    _ALIAS_TO_ID.setdefault(k.lower(), v.lower())


def resolve_alias(name: str) -> str:
    if not isinstance(name, str):
        return name
    key = name.strip().lower()
    return _ALIAS_TO_ID.get(key, name)


def canonical_id(name: str) -> str:
    """Lower-cased canonical id for *name* (alias-aware)."""
    return resolve_alias(name).lower()


def get_spec(model_id: str) -> ModelSpec | None:
    cid = canonical_id(model_id)
    return _ID_TO_SPEC.get(cid)


def provider_for_model(model: str) -> str | None:
    spec = get_spec(model)
    if spec:
        return spec.provider
    low = (model or "").strip().lower()
    if low.startswith("ollama/") or low == "ollama":
        return "ollama"
    if low.startswith("lmstudio/") or low == "lmstudio":
        return "lmstudio"
    if low.startswith("groq/"):
        return "groq"
    if low.startswith("deepseek/"):
        return "deepseek"
    if low.startswith("gemini/") or low.startswith("gemini-"):
        return "gemini"
    if low.startswith("meta/") or low.startswith("muse"):
        return "meta"
    if low.startswith(("gpt-", "openai/")):
        return "openai"
    if low.startswith(("claude", "anthropic/")) or low in ("fable", "sonnet", "opus", "haiku"):
        return "anthropic"
    return None


def all_canonical_ids() -> set[str]:
    return set(_ID_TO_SPEC.keys()) | {"lmstudio", "ollama"}


def specs_by_provider() -> dict[str, list[ModelSpec]]:
    out: dict[str, list[ModelSpec]] = {}
    for s in ALL_SPECS:
        out.setdefault(s.provider, []).append(s)
    return out


def get_models_by_provider() -> list[tuple[str, list[str], str]]:
    """Return (Provider Label, [canonical ids], requirement) for display."""
    label_map = {
        "openai": "OpenAI Provider",
        "anthropic": "Anthropic Provider",
        "gemini": "Gemini Provider",
        "deepseek": "DeepSeek Provider",
        "groq": "Groq Provider",
        "meta": "Meta Provider",
        "lmstudio": "LM Studio Provider",
        "ollama": "Ollama Provider",
    }
    out: list[tuple[str, list[str], str]] = []
    for prov, specs in specs_by_provider().items():
        label = label_map.get(prov, prov.title())
        ids = [s.id for s in specs]
        req = specs[0].requirement if specs else ""
        out.append((label, ids, req))
    # stable order: cloud providers first, locals last
    order = {
        "openai": 0,
        "anthropic": 1,
        "gemini": 2,
        "deepseek": 3,
        "groq": 4,
        "meta": 5,
        "lmstudio": 6,
        "ollama": 7,
    }
    out.sort(key=lambda x: order.get(x[0].split()[0].lower(), 99))
    return out


def model_supports_reasoning(model: str) -> bool:
    spec = get_spec(model)
    return bool(spec and spec.supports_reasoning)


def get_context_window(model: str) -> int | None:
    spec = get_spec(model)
    return spec.context_window if spec else None
