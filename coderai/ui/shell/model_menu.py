"""Shared provider and model submenu navigation for both terminal pickers."""

from __future__ import annotations

from typing import Any

from coderai.ui.shell.interaction import BrowserRow


_AUTHOR_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "meta-llama": "Meta",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "liquid": "LiquidAI",
    "mistralai": "Mistral",
    "nvidia": "NVIDIA",
    "microsoft": "Microsoft",
    "moonshotai": "Moonshot AI",
    "minimax": "MiniMax",
    "z-ai": "Z.ai",
    "openrouter": "OpenRouter",
    "other": "Other / Custom",
}
_BACK = "__back__"


def _model_entry(model: Any) -> tuple[str, str]:
    if isinstance(model, tuple):
        return str(model[0]), str(model[1])
    if isinstance(model, dict):
        return str(model.get("id", model.get("name", ""))), str(model.get("description", ""))
    return str(model), ""


def _author(model: str) -> str:
    author, separator, _ = model.removeprefix("openrouter/").partition("/")
    return author if separator else "other"


class ModelMenu:
    def __init__(
        self,
        grouped: dict[str, list[Any]],
        labels: dict[str, tuple[str, str]],
        current_model: str,
        initial_provider: str | None = None,
    ) -> None:
        self.grouped = {
            key: [_model_entry(m) for m in models] for key, models in grouped.items() if models
        }
        self.labels = labels
        self.current_model = current_model
        self.provider = initial_provider if initial_provider in self.grouped else None
        self.author: str | None = None
        self.selected_model: str | None = None
        self.authors: dict[str, list[tuple[str, str]]] = {}
        for entry in self.grouped.get("openrouter", []):
            self.authors.setdefault(_author(entry[0]), []).append(entry)
        self.authors = dict(
            sorted(self.authors.items(), key=lambda item: self.author_label(item[0]).casefold())
        )

    def author_label(self, author: str) -> str:
        return _AUTHOR_LABELS.get(author, author.replace("-", " ").title())

    @property
    def is_model_menu(self) -> bool:
        return self.provider is not None and (
            self.provider != "openrouter" or self.author is not None
        )

    @property
    def title(self) -> str:
        if self.provider is None:
            return "Model providers"
        label = self.labels.get(self.provider, (self.provider, ""))[0]
        if self.provider == "openrouter":
            if self.author is None:
                return "OpenRouter model providers"
            label += f" / {self.author_label(self.author)}"
        return f"{label} models"

    def rows(self) -> list[BrowserRow]:
        if self.provider is None:
            return [
                BrowserRow(
                    key,
                    f"{self.labels.get(key, (key, ''))[0]} ({len(models)})",
                    self.labels.get(key, (key, ""))[1],
                )
                for key, models in self.grouped.items()
            ]
        if not self.is_model_menu:
            return [
                BrowserRow(
                    key, f"{self.author_label(key)} ({len(models)})", "Models via OpenRouter"
                )
                for key, models in self.authors.items()
            ] + [BrowserRow(_BACK, "← Back to providers")]
        models = self.authors[self.author] if self.author else self.grouped[self.provider]
        back_label = (
            "← Back to OpenRouter model providers" if self.author else "← Back to providers"
        )
        return [
            BrowserRow(name, name.removeprefix("openrouter/"), description)
            for name, description in models
        ] + [BrowserRow(_BACK, back_label)]

    @property
    def default_id(self) -> str | None:
        if self.is_model_menu:
            return self.current_model
        if self.provider == "openrouter":
            return _author(self.current_model)
        return next(
            (
                key
                for key, models in self.grouped.items()
                if any(name == self.current_model for name, _ in models)
            ),
            None,
        )

    def advance(self, value: str | None) -> bool:
        """Return True on selection/root cancellation; otherwise open the next submenu."""
        if value is None or value == _BACK:
            if self.author is not None:
                self.author = None
            elif self.provider is not None:
                self.provider = None
            else:
                return True
        elif value in {row.id for row in self.rows()}:
            if self.provider is None:
                self.provider = value
            elif not self.is_model_menu:
                self.author = value
            else:
                self.selected_model = value
                return True
        return False
