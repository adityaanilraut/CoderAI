"""Single-row model catalog and provider-construction invariants."""

from coderAI.llm.factory import create_provider
from coderAI.llm.registry import ALL_SPECS
from coderAI.system.config import Config


def _config() -> Config:
    return Config(
        openai_api_key="test-openai",
        anthropic_api_key="test-anthropic",
        groq_api_key="test-groq",
        deepseek_api_key="test-deepseek",
        gemini_api_key="test-gemini",
        meta_api_key="test-meta",
    )


def test_every_catalog_row_constructs_its_declared_provider_class() -> None:
    config = _config()

    for spec in ALL_SPECS:
        provider = create_provider(spec.id, config)
        assert type(provider) is spec.provider_cls
        assert provider.actual_model


def test_supported_models_and_context_windows_are_derived_from_catalog() -> None:
    for spec in ALL_SPECS:
        assert spec.provider_cls.SUPPORTED_MODELS[spec.id] == spec.id
        assert spec.provider_cls.MODEL_CONTEXT_WINDOWS[spec.id] == spec.context_window
