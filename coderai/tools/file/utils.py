"""Shared context, sandbox, and callback plumbing for file mutations."""

from __future__ import annotations

import difflib
import pathlib
from typing import Any

from coderai.utils.path import write_text_file
from coderai.sandbox import check_sandbox_path_access, validate_sandboxed_path


def context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def _coerce_dry_run(value: Any) -> bool:
    """Coerce boolean-like dry-run inputs ("true"/1/yes) instead of `is True` checks."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


def is_dry_run(context: Any, args: dict[str, Any] | None = None) -> bool:
    """Return True if the execution context or tool arguments specify dry-run mode."""
    if _coerce_dry_run(context_value(context, "dry_run", False)):
        return True
    if args and isinstance(args, dict):
        if _coerce_dry_run(args.get("dry_run")):
            return True
    return False


def generate_virtual_patch(
    file_path: str,
    old_content: str | None,
    new_content: str,
) -> dict[str, Any]:
    """Generate structured virtual patch and diff metadata without touching disk."""
    old_text = old_content if old_content is not None else ""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    lines_added = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    lines_removed = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )

    return {
        "file_path": file_path,
        "diff": diff_text,
        "is_creation": old_content is None,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "old_bytes": len(old_text.encode("utf-8")),
        "new_bytes": len(new_content.encode("utf-8")),
    }


def check_file_write_access(context: Any, file_path: str) -> str | None:
    """Validate whether write access is permitted under current sandbox and isolated_cwd."""
    raw = file_path if isinstance(file_path, (str, pathlib.Path)) else ""
    if not str(raw).strip():
        return "SANDBOX_VIOLATION: empty file path."
    isolated_cwd = context_value(context, "isolated_cwd")
    isolated_root: str | None = None
    if (
        isinstance(isolated_cwd, (str, pathlib.Path))
        and str(isolated_cwd).strip()
        and "MagicMock" not in str(type(isolated_cwd))
    ):
        isolated_root = str(isolated_cwd)

    sb_mode = context_value(context, "sandbox_mode")
    mode_str = sb_mode if isinstance(sb_mode, str) else None
    ws_root = context_value(context, "project_root")
    ws_str = ws_root if isinstance(ws_root, (str, pathlib.Path)) else "."

    # Clamp relative paths against the effective root before any check so
    # `../` escapes cannot slip past either gate as a raw string.
    candidate = str(raw)
    if not pathlib.Path(candidate).is_absolute():
        candidate = str(pathlib.Path(str(isolated_root or ws_str)) / candidate)

    if isolated_root:
        valid, resolved, err = validate_sandboxed_path(candidate, root=isolated_root)
        if not valid:
            return err
        candidate = str(resolved)

    allowed, error = check_sandbox_path_access(
        candidate,
        op="write",
        mode=mode_str,
        workspace_root=ws_str,
    )
    return None if allowed else error


def write_file_with_callbacks(
    context: Any,
    file_path: str,
    content: str,
    encoding: str = "utf8",
    line_endings: str = "LF",
) -> int:
    before = context_value(context, "on_before_file_mutation")
    after = context_value(context, "on_after_file_mutation")
    if callable(before):
        before(file_path)
    bytes_written = write_text_file(file_path, content, encoding, line_endings)
    if callable(after):
        after(file_path)
    return bytes_written


async def async_write_file_with_locks(
    context: Any,
    file_path: str,
    content: str,
    encoding: str = "utf8",
    line_endings: str = "LF",
) -> int:
    """Acquire fine-grained path write lock and write file with lifecycle callbacks."""
    from coderai.tools.legacy.path_lock import get_path_lock_manager

    ws_root = context_value(context, "project_root")
    path_lock = get_path_lock_manager()
    async with path_lock.acquire_write_lock(file_path, project_root=ws_root):
        return write_file_with_callbacks(
            context=context,
            file_path=file_path,
            content=content,
            encoding=encoding,
            line_endings=line_endings,
        )


# --- File Type & Media Sniffing ---

import mimetypes
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

MEDIA_SNIFF_BYTES = 512

_EXTRA_MIME_TYPES = {
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".mts": "text/typescript",
    ".cts": "text/typescript",
}

for suffix, mime_type in _EXTRA_MIME_TYPES.items():
    mimetypes.add_type(mime_type, suffix)

_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".svgz": "image/svg+xml",
}
_VIDEO_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".flv": "video/x-flv",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
}
_TEXT_MIME_BY_SUFFIX = {
    ".svg": "image/svg+xml",
}

_ASF_HEADER = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
_FTYP_IMAGE_BRANDS = {
    "avif": "image/avif",
    "avis": "image/avif",
    "heic": "image/heic",
    "heif": "image/heif",
    "heix": "image/heif",
    "hevc": "image/heic",
    "mif1": "image/heif",
    "msf1": "image/heif",
}
_FTYP_VIDEO_BRANDS = {
    "isom": "video/mp4",
    "iso2": "video/mp4",
    "iso5": "video/mp4",
    "mp41": "video/mp4",
    "mp42": "video/mp4",
    "avc1": "video/mp4",
    "mp4v": "video/mp4",
    "m4v": "video/x-m4v",
    "qt": "video/quicktime",
    "3gp4": "video/3gpp",
    "3gp5": "video/3gpp",
    "3gp6": "video/3gpp",
    "3gp7": "video/3gpp",
    "3g2": "video/3gpp2",
}

_NON_TEXT_SUFFIXES = {
    ".icns",
    ".psd",
    ".ai",
    ".eps",
    ".pdf",
    ".doc",
    ".docx",
    ".dot",
    ".dotx",
    ".rtf",
    ".odt",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlt",
    ".xltx",
    ".xltm",
    ".ods",
    ".ppt",
    ".pptx",
    ".pptm",
    ".pps",
    ".ppsx",
    ".odp",
    ".pages",
    ".numbers",
    ".key",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".lz",
    ".lz4",
    ".br",
    ".cab",
    ".ar",
    ".deb",
    ".rpm",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".aac",
    ".m4a",
    ".wma",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".apk",
    ".ipa",
    ".jar",
    ".class",
    ".pyc",
    ".pyo",
    ".wasm",
    ".dmg",
    ".iso",
    ".img",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db3",
}


@dataclass(frozen=True)
class FileType:
    kind: Literal["text", "image", "video", "unknown"]
    mime_type: str


def _sniff_ftyp_brand(header: bytes) -> str | None:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return None
    brand = header[8:12].decode("ascii", errors="ignore").lower()
    return brand.strip()


def sniff_media_from_magic(data: bytes) -> FileType | None:
    header = data[:MEDIA_SNIFF_BYTES]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType(kind="image", mime_type="image/png")
    if header.startswith(b"\xff\xd8\xff"):
        return FileType(kind="image", mime_type="image/jpeg")
    if header.startswith((b"GIF87a", b"GIF89a")):
        return FileType(kind="image", mime_type="image/gif")
    if header.startswith(b"BM"):
        return FileType(kind="image", mime_type="image/bmp")
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return FileType(kind="image", mime_type="image/tiff")
    if header.startswith(b"\x00\x00\x01\x00"):
        return FileType(kind="image", mime_type="image/x-icon")
    if header.startswith(b"RIFF") and len(header) >= 12:
        chunk = header[8:12]
        if chunk == b"WEBP":
            return FileType(kind="image", mime_type="image/webp")
        if chunk == b"AVI ":
            return FileType(kind="video", mime_type="video/x-msvideo")
    if header.startswith(b"FLV"):
        return FileType(kind="video", mime_type="video/x-flv")
    if header.startswith(_ASF_HEADER):
        return FileType(kind="video", mime_type="video/x-ms-wmv")
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        lowered = header.lower()
        if b"webm" in lowered:
            return FileType(kind="video", mime_type="video/webm")
        if b"matroska" in lowered:
            return FileType(kind="video", mime_type="video/x-matroska")
    brand = _sniff_ftyp_brand(header)
    if brand:
        if brand in _FTYP_IMAGE_BRANDS:
            return FileType(kind="image", mime_type=_FTYP_IMAGE_BRANDS[brand])
        if brand in _FTYP_VIDEO_BRANDS:
            return FileType(kind="video", mime_type=_FTYP_VIDEO_BRANDS[brand])
    return None


def detect_file_type(path: str | PurePath, header: bytes | None = None) -> FileType:
    suffix = PurePath(str(path)).suffix.lower()
    media_hint: FileType | None = None
    if suffix in _TEXT_MIME_BY_SUFFIX:
        media_hint = FileType(kind="text", mime_type=_TEXT_MIME_BY_SUFFIX[suffix])
    elif suffix in _IMAGE_MIME_BY_SUFFIX:
        media_hint = FileType(kind="image", mime_type=_IMAGE_MIME_BY_SUFFIX[suffix])
    elif suffix in _VIDEO_MIME_BY_SUFFIX:
        media_hint = FileType(kind="video", mime_type=_VIDEO_MIME_BY_SUFFIX[suffix])
    else:
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            if mime_type.startswith("image/"):
                media_hint = FileType(kind="image", mime_type=mime_type)
            elif mime_type.startswith("video/"):
                media_hint = FileType(kind="video", mime_type=mime_type)

    if media_hint and media_hint.kind in ("image", "video"):
        return media_hint

    if header is not None:
        sniffed = sniff_media_from_magic(header)
        if sniffed:
            if media_hint and sniffed.kind != media_hint.kind:
                return FileType(kind="unknown", mime_type="")
            return sniffed
        if b"\x00" in header:
            return FileType(kind="unknown", mime_type="")

    if media_hint:
        return media_hint
    if suffix in _NON_TEXT_SUFFIXES:
        return FileType(kind="unknown", mime_type="")
    return FileType(kind="text", mime_type="text/plain")
