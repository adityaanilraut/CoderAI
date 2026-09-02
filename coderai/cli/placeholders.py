"""Placeholders — Phase5 port of Kimi ui/shell/placeholders.py:26-387.

Large paste collapse + image cache + SequenceMatcher refold.
ponytail: env thresholds, regex [Pasted text #] / [image:], cache ~/.coderai/prompt-cache/images sha256,
wrap_media_part stub, per-process paste-id seq. No share/ dir; no legacy roots; no UI deps.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any


# thresholds — env KIMI_CLI_PASTE_CHAR_THRESHOLD (Kimi compat) + fallback CODERAI variant
def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or not v.strip():
            return default
        return int(v.strip())
    except Exception:
        return default


KIMI_CLI_PASTE_CHAR_THRESHOLD = _env_int("KIMI_CLI_PASTE_CHAR_THRESHOLD", 1000)
KIMI_CLI_PASTE_LINE_THRESHOLD = _env_int("KIMI_CLI_PASTE_LINE_THRESHOLD", 15)

# also support CODERAI_ prefix
if os.getenv("CODERAI_PASTE_CHAR_THRESHOLD"):
    KIMI_CLI_PASTE_CHAR_THRESHOLD = _env_int(
        "CODERAI_PASTE_CHAR_THRESHOLD", KIMI_CLI_PASTE_CHAR_THRESHOLD
    )
if os.getenv("CODERAI_PASTE_LINE_THRESHOLD"):
    KIMI_CLI_PASTE_LINE_THRESHOLD = _env_int(
        "CODERAI_PASTE_LINE_THRESHOLD", KIMI_CLI_PASTE_LINE_THRESHOLD
    )

_IMAGE_PLACEHOLDER_RE = re.compile(
    r"\[(?P<type>[a-zA-Z0-9_\-]+):(?P<id>[a-zA-Z0-9_\-\.]+)(?:,(?P<width>\d+)x(?P<height>\d+))?\]"
)
_PASTED_TEXT_PLACEHOLDER_RE = re.compile(
    r"\[Pasted text #(?P<id>\d+)(?: \+(?P<lines>\d+) lines?)?\]"
)


# cache root — Kimi uses share/prompt-cache/images sha256; CoderAI uses ~/.coderai/prompt-cache/images
def _prompt_cache_root() -> Path:
    # honor env if set
    env = os.getenv("CODERAI_PROMPT_CACHE_DIR") or os.getenv("KIMI_PROMPT_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".coderai" / "prompt-cache" / "images"


def _image_cache_dir() -> Path:
    return _prompt_cache_root()


# ponytail: minimal wrap_media_part — Kimi wraps ImageURLPart for wire, we return dict-like
def wrap_media_part(
    part: Any, tag: str = "image", attrs: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    # part may be dict with url or ImageURLPart-like; normalize to list
    if isinstance(part, list):
        return part
    if isinstance(part, dict):
        return [part]
    # try to convert Pydantic-like ImageURLPart
    try:
        url = (
            getattr(getattr(part, "image_url", None), "url", None)
            or getattr(part, "url", None)
            or str(part)
        )
        return [{"type": "image_url", "image_url": {"url": url}, "tag": tag, "attrs": attrs or {}}]
    except Exception:
        return [{"type": "image_url", "image_url": {"url": str(part)}}]


def sanitize_surrogates(text: str) -> str:
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def normalize_pasted_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def count_text_lines(text: str) -> int:
    if not text:
        return 1
    return text.count("\n") + 1


def should_placeholderize_pasted_text(text: str) -> bool:
    normalized = normalize_pasted_text(text)
    return (
        len(normalized) >= KIMI_CLI_PASTE_CHAR_THRESHOLD
        or count_text_lines(normalized) >= KIMI_CLI_PASTE_LINE_THRESHOLD
    )


def build_pasted_text_placeholder(paste_id: int, text: str) -> str:
    lc = count_text_lines(text)
    if lc <= 1:
        return f"[Pasted text #{paste_id}]"
    return f"[Pasted text #{paste_id} +{lc} lines]"


@dataclass(slots=True)
class PastedTextEntry:
    paste_id: int
    text: str

    @property
    def token(self) -> str:
        return build_pasted_text_placeholder(self.paste_id, self.text)


@dataclass(slots=True)
class PlaceholderTokenMatch:
    start: int
    end: int
    raw: str
    handler: Any
    match: re.Match[str]


# — handlers —
class PastedTextPlaceholderHandler:
    def __init__(self) -> None:
        self._entries: dict[int, PastedTextEntry] = {}
        self._next_id = 1

    def create_placeholder(self, text: str) -> str:
        normalized = sanitize_surrogates(normalize_pasted_text(text))
        e = PastedTextEntry(paste_id=self._next_id, text=normalized)
        self._entries[e.paste_id] = e
        self._next_id += 1
        return e.token

    def maybe_placeholderize(self, text: str) -> str:
        normalized = normalize_pasted_text(text)
        if not should_placeholderize_pasted_text(normalized):
            return normalized
        return self.create_placeholder(normalized)

    def entry_for_id(self, paste_id: int) -> PastedTextEntry | None:
        return self._entries.get(paste_id)

    def iter_entries_for_command(
        self, command: str
    ) -> list[tuple[PlaceholderTokenMatch, PastedTextEntry]]:
        out: list[tuple[PlaceholderTokenMatch, PastedTextEntry]] = []
        cur = 0
        while True:
            m = self.find_next(command, cur)
            if m is None:
                break
            pid = int(m.match.group("id"))
            e = self.entry_for_id(pid)
            if e is not None:
                out.append((m, e))
            cur = m.end
        return out

    def find_next(self, text: str, start: int = 0) -> PlaceholderTokenMatch | None:
        m = _PASTED_TEXT_PLACEHOLDER_RE.search(text, start)
        if m is None:
            return None
        return PlaceholderTokenMatch(
            start=m.start(), end=m.end(), raw=m.group(0), handler=self, match=m
        )

    def resolve_content(self, match: PlaceholderTokenMatch) -> list[dict[str, Any]] | None:
        pid = int(match.match.group("id"))
        e = self.entry_for_id(pid)
        if e is None:
            return None
        return [{"type": "text", "text": e.text}]

    def expand_text(self, match: PlaceholderTokenMatch) -> str | None:
        pid = int(match.match.group("id"))
        e = self.entry_for_id(pid)
        return None if e is None else e.text

    def serialize_for_history(self, match: PlaceholderTokenMatch) -> str | None:
        return self.expand_text(match)

    def expand_for_editor(self, match: PlaceholderTokenMatch) -> str | None:
        return self.expand_text(match)

    def refold_after_editor(self, edited_text: str, original_command: str) -> str:
        expanded_original, intervals = self._expanded_text_and_intervals(original_command)
        if not intervals:
            return edited_text
        opcodes = SequenceMatcher(a=expanded_original, b=edited_text, autojunk=False).get_opcodes()
        reps: list[tuple[int, int, str]] = []
        for start, end, token, expected in intervals:
            mapped = self._map_interval(opcodes, start, end)
            if mapped is None:
                continue
            ms, me = mapped
            if edited_text[ms:me] != expected:
                continue
            reps.append((ms, me, token))
        res = edited_text
        for s, e, tok in reversed(reps):
            res = res[:s] + tok + res[e:]
        return res

    def _expanded_text_and_intervals(
        self, command: str
    ) -> tuple[str, list[tuple[int, int, str, str]]]:
        parts: list[str] = []
        intervals: list[tuple[int, int, str, str]] = []
        cur = 0
        exp_cur = 0
        for m, e in self.iter_entries_for_command(command):
            lit = command[cur : m.start]
            if lit:
                parts.append(lit)
                exp_cur += len(lit)
            s = exp_cur
            parts.append(e.text)
            exp_cur += len(e.text)
            intervals.append((s, exp_cur, m.raw, e.text))
            cur = m.end
        if cur < len(command):
            parts.append(command[cur:])
        return "".join(parts), intervals

    @staticmethod
    def _map_interval(opcodes: Any, start: int, end: int) -> tuple[int, int] | None:
        ms: int | None = None
        me: int | None = None
        cur = start
        for tag, i1, i2, j1, _j2 in opcodes:
            if i2 <= cur:
                continue
            if i1 >= end:
                break
            os_ = max(i1, cur, start)
            oe = min(i2, end)
            if os_ >= oe:
                continue
            if tag != "equal":
                return None
            seg_s = j1 + (os_ - i1)
            seg_e = j1 + (oe - i1)
            if ms is None:
                ms = seg_s
            elif me != seg_s:
                return None
            me = seg_e
            cur = oe
        if cur != end or ms is None or me is None:
            return None
        return ms, me


class ImagePlaceholderHandler:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or _image_cache_dir()

    def _ensure_dir(self) -> Path | None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            return self._cache_dir
        except Exception:
            return None

    def create_placeholder(self, image: Any) -> str | None:
        # image is PIL.Image or bytes path
        try:
            from PIL import Image as PILImage  # type: ignore

            if isinstance(image, (bytes, bytearray)):
                # bytes -> treat as png bytes sha
                payload = bytes(image)
                h = sha256(payload).hexdigest()
                d = self._ensure_dir()
                if d is None:
                    return None
                p = d / f"{h}.png"
                if not p.exists():
                    p.write_bytes(payload)
                return f"[image:{h}.png]"
            if isinstance(image, str) and Path(image).exists():
                # file path
                pth = Path(image)
                data = pth.read_bytes()
                h = sha256(data).hexdigest()
                # if image file not png, convert via Pillow if available
                try:
                    img = PILImage.open(BytesIO(data))
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    payload = buf.getvalue()
                    h = sha256(payload).hexdigest()
                except Exception:
                    payload = data
                d = self._ensure_dir()
                if d is None:
                    return None
                out = d / f"{h}.png"
                if not out.exists():
                    out.write_bytes(payload)
                # try to get dimensions
                try:
                    img = PILImage.open(BytesIO(payload))
                    w, h2 = img.size
                    return f"[image:{h}.png,{w}x{h2}]"
                except Exception:
                    return f"[image:{h}.png]"
            # PIL Image instance
            if hasattr(image, "save"):
                buf = BytesIO()
                image.save(buf, format="PNG")
                payload = buf.getvalue()
                h = sha256(payload).hexdigest()
                d = self._ensure_dir()
                if d is None:
                    return None
                out = d / f"{h}.png"
                if not out.exists():
                    out.write_bytes(payload)
                w, h2 = getattr(image, "size", (0, 0))
                if w and h2:
                    return f"[image:{h}.png,{w}x{h2}]"
                return f"[image:{h}.png]"
        except Exception:
            return None
        return None

    def find_next(self, text: str, start: int = 0) -> PlaceholderTokenMatch | None:
        m = _IMAGE_PLACEHOLDER_RE.search(text, start)
        if m is None:
            return None
        return PlaceholderTokenMatch(
            start=m.start(), end=m.end(), raw=m.group(0), handler=self, match=m
        )

    def resolve_content(self, match: PlaceholderTokenMatch) -> list[dict[str, Any]] | None:
        # image placeholder -> ImageURLPart via base64 data url if cached file exists
        try:
            aid = match.match.group("id")
            p = self._cache_dir / aid
            if not p.exists():
                return None
            data = p.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            mime, _ = mimetypes.guess_type(p.name)
            mime = mime or "image/png"
            url = f"data:{mime};base64,{b64}"
            part = {"type": "image_url", "image_url": {"url": url}}
            return wrap_media_part(part, tag="image", attrs={"path": str(p)})
        except Exception:
            return None

    def expand_text(self, match: PlaceholderTokenMatch) -> str | None:
        return match.raw

    def serialize_for_history(self, match: PlaceholderTokenMatch) -> str | None:
        return match.raw

    def expand_for_editor(self, match: PlaceholderTokenMatch) -> str | None:
        return match.raw


@dataclass(slots=True)
class ResolvedPromptCommand:
    display_command: str
    resolved_text: str
    content: list[dict[str, Any]]


class PromptPlaceholderManager:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or _image_cache_dir()
        self._text_handler = PastedTextPlaceholderHandler()
        self._image_handler = ImagePlaceholderHandler(self._cache_dir)
        self._handlers: tuple[Any, ...] = (self._text_handler, self._image_handler)

    @property
    def attachment_cache(self) -> Any:
        # compat: expose image handler cache dir
        return self._cache_dir

    @property
    def text_handler(self) -> PastedTextPlaceholderHandler:
        return self._text_handler

    @property
    def image_handler(self) -> ImagePlaceholderHandler:
        return self._image_handler

    def maybe_placeholderize_pasted_text(self, text: str) -> str:
        return self._text_handler.maybe_placeholderize(text)

    def create_image_placeholder(self, image: Any) -> str | None:
        return self._image_handler.create_placeholder(image)

    def resolve_command(self, command: str) -> ResolvedPromptCommand:
        content: list[dict[str, Any]] = []
        resolved: list[str] = []
        cur = 0
        while True:
            m = self._find_next_match(command, cur)
            if m is None:
                break
            if m.start > cur:
                lit = command[cur : m.start]
                content.append({"type": "text", "text": lit})
                resolved.append(lit)
            rc = m.handler.resolve_content(m)
            if rc is None:
                content.append({"type": "text", "text": m.raw})
                resolved.append(m.raw)
            else:
                content.extend(rc)
                exp = m.handler.expand_text(m)
                resolved.append(m.raw if exp is None else exp)
            cur = m.end
        if cur < len(command):
            lit = command[cur:]
            content.append({"type": "text", "text": lit})
            resolved.append(lit)
        if not content:
            content = [{"type": "text", "text": command}]
            resolved = [command]
        return ResolvedPromptCommand(
            display_command=command, resolved_text="".join(resolved), content=content
        )

    def serialize_for_history(self, command: str) -> str:
        return self._rewrite(command, lambda h, m: h.serialize_for_history(m))

    def expand_for_editor(self, command: str) -> str:
        return self._rewrite(command, lambda h, m: h.expand_for_editor(m))

    def refold_after_editor(self, edited_text: str, original_command: str) -> str:
        return self._text_handler.refold_after_editor(edited_text, original_command)

    def _find_next_match(self, text: str, start: int = 0) -> PlaceholderTokenMatch | None:
        earliest: PlaceholderTokenMatch | None = None
        for h in self._handlers:
            m = h.find_next(text, start)
            if m is None:
                continue
            if earliest is None or m.start < earliest.start:
                earliest = m
        return earliest

    def _rewrite(self, command: str, replacer: Any) -> str:
        parts: list[str] = []
        cur = 0
        while True:
            m = self._find_next_match(command, cur)
            if m is None:
                break
            if m.start > cur:
                parts.append(command[cur : m.start])
            rep = replacer(m.handler, m)
            parts.append(m.raw if rep is None else rep)
            cur = m.end
        if cur < len(command):
            parts.append(command[cur:])
        return "".join(parts)


# singleton for app convenience
_default_manager: PromptPlaceholderManager | None = None


def get_placeholder_manager() -> PromptPlaceholderManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = PromptPlaceholderManager()
    return _default_manager
