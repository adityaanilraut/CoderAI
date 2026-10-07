"""Model comparison uses configured routing and cached metadata, never probes on render."""

from __future__ import annotations

from hashlib import sha256
import json
import time

from coderai.ui.shell.storage import ShellStorage


def connection_identity(model: str, settings: dict) -> tuple[str, bool]:
    from coderai.llm import resolve_model_provider_routing

    endpoint, key = resolve_model_provider_routing(
        model,
        explicit_base_url=settings.get("baseURL"),
        explicit_api_key=settings.get("apiKey"),
        env=settings.get("env"),
        use_oauth=False,
    )
    if not key and "kimi" in model.lower():
        from coderai.auth.oauth import KIMI_CODE_OAUTH_KEY, load_token

        token = load_token(KIMI_CODE_OAUTH_KEY)
        key = token.access_token if token else None
    identity = sha256(json.dumps([model, endpoint, str(key or "")]).encode()).hexdigest()
    return identity, bool(key)


def record_connection(root: str, model: str, settings: dict, success: bool) -> None:
    identity, _ = connection_identity(model, settings)
    ShellStorage(root).update_library(
        "connections", identity, {"success": success, "time": time.time()}
    )


def connection_label(root: str, model: str, settings: dict, *, records: dict | None = None) -> str:
    identity, configured = connection_identity(model, settings)
    records = ShellStorage(root).library().get("connections", {}) if records is None else records
    record = records.get(identity)
    if record:
        age = max(0, int((time.time() - record["time"]) / 60))
        return f"{'Verified' if record['success'] else 'Last test failed'} {age} min ago; /model verify retests"
    return (
        "Configured; not verified"
        if configured
        else "No API key; /setup (keyless local endpoints may work)"
    )


def describe_models(grouped: dict, root: str, settings: dict, current: str) -> dict:
    from coderai.openrouter import fetch_openrouter_models
    from coderai.prompt import calculate_context_budget
    from coderai.ui.shell.session_picker import estimate_model_cost
    from coderai.utils.common.model_capabilities import (
        supports_multimodal,
        MULTIMODAL_MODELS,
        NON_MULTIMODAL_MODELS,
    )

    catalog = {
        "openrouter/" + str(m.get("id")): m for m in fetch_openrouter_models(allow_network=False)
    }
    from coderai.ui.shell.model_menu import _model_entry

    records = ShellStorage(root).library().get("connections", {})
    result = {}
    for provider, models in grouped.items():
        rows = []
        for item in models:
            model, description = _model_entry(item)
            effective = settings if model == current else {}
            data = catalog.get(model, {})
            modalities = (data.get("architecture") or {}).get("input_modalities")
            if modalities is not None:
                images = "yes" if "image" in modalities else "no"
            elif (
                model in MULTIMODAL_MODELS
                or model in NON_MULTIMODAL_MODELS
                or effective.get("multimodal") in ("on", "off")
            ):
                images = (
                    "yes"
                    if supports_multimodal(model, effective.get("multimodal", "default"))
                    else "no"
                )
            else:
                images = "unknown"
            parameters = data.get("supported_parameters")
            tools = (
                ("yes" if "tools" in parameters else "no")
                if isinstance(parameters, list)
                else "unknown (provider dependent)"
            )
            context = data.get("context_length")
            if context is None:
                budget = calculate_context_budget(
                    model, context_limit=effective.get("contextWindow")
                )
                context_label = f"{budget['context_limit']:,} (configured / local estimate)"
            else:
                context_label = f"{int(context):,} (cached catalog)"
            pricing = data.get("pricing") or {}
            input_price: float | None
            output_price: float | None
            try:
                if "prompt" in pricing and "completion" in pricing:
                    input_price, output_price = (
                        float(pricing["prompt"]) * 1e6,
                        float(pricing["completion"]) * 1e6,
                    )
                else:
                    input_price, output_price = (
                        estimate_model_cost(model, 1000000, 0),
                        estimate_model_cost(model, 0, 1000000),
                    )
                price = (
                    f"${input_price:g} input / ${output_price:g} output per 1M tokens (estimate)"
                    if input_price is not None and output_price is not None
                    else "Unavailable"
                )
            except (ValueError, TypeError):
                price = "Unavailable"
            try:
                status = connection_label(root, model, effective, records=records)
                if model != current:
                    status = f"Default provider routing: {status}\nSelect this model to inspect and verify its configured route."
            except (OSError, ValueError):
                status = "Verification unavailable"
            rows.append(
                (
                    model,
                    f"{description}\n{status}\nImages: {images} | Tools: {tools}\nContext: {context_label}\nPricing: {price}\n/model favorite {model} saves a favorite.",
                )
            )
        result[provider] = rows
    return result
