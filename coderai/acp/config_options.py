"""ACP mode taxonomy and capability-aware configuration selectors."""

from __future__ import annotations

from typing import Any

import acp

from coderai.llm import derive_model_capabilities

ACP_MODES = (
    ("default", "Default", "Manual approvals; tools execute normally."),
    ("plan", "Plan", "Read-only planning."),
    ("auto", "Auto", "Run unattended; auto-approve operations and dismiss questions."),
    ("yolo", "YOLO", "Auto-approve tool operations."),
)
ACP_MODE_IDS = frozenset(mode[0] for mode in ACP_MODES)


def mode_id(state: Any) -> str:
    if getattr(state, "plan_mode", False):
        return "plan"
    approval = getattr(state, "approval", None)
    if getattr(approval, "afk", False):
        return "auto"
    if getattr(approval, "yolo", False):
        return "yolo"
    return "default"


def mode_state(state: Any) -> acp.schema.SessionModeState:
    return acp.schema.SessionModeState(
        available_modes=[
            acp.schema.SessionMode(id=id, name=name, description=description)
            for id, name, description in ACP_MODES
        ],
        current_mode_id=mode_id(state),
    )


def thinking_values(model: Any) -> list[str]:
    if model is None:
        return []
    capabilities = derive_model_capabilities(model)
    if "thinking" not in capabilities:
        return []
    return ["on"] if "always_thinking" in capabilities else ["off", "on"]


def config_options(
    config: Any, model_key: str, thinking: bool, state: Any
) -> list[acp.schema.SessionConfigOption]:
    models = getattr(config, "models", None) or {}
    payloads: list[dict[str, Any]] = [
        {
            "type": "select",
            "id": "model",
            "name": "Model",
            "category": "model",
            "currentValue": model_key,
            "options": [
                {"value": key, "name": model.display_name or model.model}
                for key, model in models.items()
            ],
        }
    ]
    values = thinking_values(models.get(model_key))
    if values:
        level = "on" if thinking or "off" not in values else "off"
        payloads.append(
            {
                "type": "select",
                "id": "thinking",
                "name": "Thinking",
                "category": "thought_level",
                "currentValue": level,
                "options": [
                    {"value": value, "name": f"Thinking {value.title()}"} for value in values
                ],
            }
        )
    payloads.append(
        {
            "type": "select",
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "currentValue": mode_id(state),
            "options": [
                {"value": id, "name": name, "description": description}
                for id, name, description in ACP_MODES
            ],
        }
    )
    return [acp.schema.SessionConfigOption.model_validate(payload) for payload in payloads]
