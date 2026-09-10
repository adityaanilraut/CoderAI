# Ported from coderai/core/oauth.py - kimi structure (auth/platforms.py).
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, NamedTuple

import requests

KIMI_CODE_PLATFORM_ID = "kimi-code"

class Platform(NamedTuple):
    id: str
    name: str
    base_url: str
    search_url: str | None = None
    fetch_url: str | None = None
    allowed_prefixes: list[str] | None = None


def _kimi_code_base_url() -> str:
    return os.getenv("KIMI_CODE_BASE_URL") or "https://api.kimi.com/coding/v1"


PLATFORMS: list[Platform] = [
    Platform(
        id=KIMI_CODE_PLATFORM_ID,
        name="Kimi Code",
        base_url=_kimi_code_base_url(),
        search_url=f"{_kimi_code_base_url()}/search",
        fetch_url=f"{_kimi_code_base_url()}/fetch",
    ),
    Platform(
        id="moonshot-cn",
        name="Moonshot AI Open Platform (moonshot.cn)",
        base_url="https://api.moonshot.cn/v1",
        allowed_prefixes=["kimi-k"],
    ),
    Platform(
        id="moonshot-ai",
        name="Moonshot AI Open Platform (moonshot.ai)",
        base_url="https://api.moonshot.ai/v1",
        allowed_prefixes=["kimi-k"],
    ),
]

_PLATFORM_BY_ID = {platform.id: platform for platform in PLATFORMS}

MANAGED_PROVIDER_PREFIX = "managed:"


def get_platform_by_id(platform_id: str) -> Platform | None:
    return _PLATFORM_BY_ID.get(platform_id)


def managed_provider_key(platform_id: str) -> str:
    return f"{MANAGED_PROVIDER_PREFIX}{platform_id}"


def managed_model_key(platform_id: str, model_id: str) -> str:
    return f"{platform_id}/{model_id}"


@dataclass(slots=True)
class RemoteModelInfo:
    id: str
    context_length: int = 0
    supports_reasoning: bool = False
    supports_image_in: bool = False
    supports_video_in: bool = False
    display_name: str | None = None

    @property
    def capabilities(self) -> set[str]:
        caps: set[str] = set()
        if self.supports_reasoning:
            caps.add("thinking")
        if "thinking" in self.id.lower():
            caps.update(("thinking", "always_thinking"))
        if self.supports_image_in:
            caps.add("image_in")
        if self.supports_video_in:
            caps.add("video_in")
        if self.id.lower().startswith("kimi-k2"):
            caps.update(("thinking", "image_in", "video_in"))
        return caps


def list_remote_models(platform: Platform, api_key: str) -> list[RemoteModelInfo]:
    from coderai.auth.oauth import OAuthError, OAuthUnauthorized

    try:
        resp = requests.get(
            f"{platform.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        payload = resp.json()
    except Exception as exc:
        raise OAuthError(f"Failed to list models: {exc}") from exc
    if resp.status_code == 401:
        raise OAuthUnauthorized("Listing models unauthorized.")
    if resp.status_code != 200:
        raise OAuthError(f"Listing models failed (HTTP {resp.status_code}).")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise OAuthError(f"Unexpected models response for {platform.base_url}")
    models: list[RemoteModelInfo] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        models.append(
            RemoteModelInfo(
                id=str(item["id"]),
                context_length=int(item.get("context_length") or 0),
                supports_reasoning=bool(item.get("supports_reasoning")),
                supports_image_in=bool(item.get("supports_image_in")),
                supports_video_in=bool(item.get("supports_video_in")),
                display_name=str(item["display_name"]) if item.get("display_name") else None,
            )
        )
    if platform.allowed_prefixes is not None:
        prefixes = tuple(platform.allowed_prefixes)
        models = [m for m in models if m.id.startswith(prefixes)]
    return models
