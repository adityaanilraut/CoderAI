"""Validated search filters shared by the tool and provider adapters."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from coderai.network.security import is_domain_matching
from coderai.tools.web.common import bounded_int


@dataclass(frozen=True)
class SearchFilters:
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    start_date: str = ""
    end_date: str = ""
    language: str = ""
    region: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}

    def allows_url(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return (
            not self.include_domains
            or any(
                is_domain_matching(host, "*." + domain.removeprefix("*."))
                for domain in self.include_domains
            )
        ) and not any(
            is_domain_matching(host, "*." + domain.removeprefix("*."))
            for domain in self.exclude_domains
        )

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> SearchFilters:
        def domains(key: str) -> tuple[str, ...]:
            values = args.get(key) or []
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError(f"{key} must be an array of at most 20 domains.")
            cleaned: list[str] = []
            for value in values:
                if not isinstance(value, str) or not re.fullmatch(
                    r"(?:\*\.)?[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value
                ):
                    raise ValueError(f"{key} must contain hostnames, not URLs or paths.")
                cleaned.append(value.lower().removeprefix("*."))
            return tuple(dict.fromkeys(cleaned))

        def date(key: str) -> str:
            value = args.get(key) or ""
            if not value:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"{key} must be an ISO date or datetime.")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{key} must be an ISO date or datetime.") from exc
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).isoformat()

        start, end = date("start_date"), date("end_date")
        if args.get("recency_days") is not None:
            if start:
                raise ValueError("Use either start_date or recency_days, not both.")
            days = bounded_int(args["recency_days"], 7, 1, 3650, "recency_days")
            start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if start and end and datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ValueError("start_date must not follow end_date.")
        language, region = args.get("language") or "", args.get("region") or ""
        if not isinstance(language, str) or (
            language and not re.fullmatch(r"[a-zA-Z]{2}", language)
        ):
            raise ValueError("language must be a language code, e.g. en.")
        if not isinstance(region, str) or (region and not re.fullmatch(r"[a-zA-Z]{2}", region)):
            raise ValueError("region must be a two-letter country code, e.g. US.")
        return cls(
            domains("include_domains"),
            domains("exclude_domains"),
            start,
            end,
            language.lower(),
            region.upper(),
        )
