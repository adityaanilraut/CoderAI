"""Auto-downloader for the prebuilt Ink UI binary.

The TypeScript UI ships as a per-platform standalone binary built with
``bun build --compile``. Keeping it out of the Python wheel keeps the wheel
pure-Python; this module lazily fetches the correct binary from GitHub
Releases the first time the user runs ``coderAI chat``.

Resolution order in :func:`ensure_binary`:

1. ``$CODERAI_UI_BINARY`` — an explicit path set by the user or tests.
2. A dev-checkout build at ``<repo>/ui/dist/coderai-ui``.
3. A versioned cached download at ``~/.coderAI/bin/coderai-ui-{plat}-v{ver}``.
4. Download from GitHub Releases + SHA256 verification, then cache.

No new runtime dependencies are added — all network I/O uses
:mod:`urllib.request` from the stdlib. The download progress bar uses Rich,
which is already a dependency for utility CLI commands.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ``owner/repo`` on GitHub where binaries are published as Release assets.
# Can be overridden via $CODERAI_UI_REPO for forks or staging builds.
DEFAULT_REPO = "coderAI/coderAI"
RELEASES_URL = "https://github.com/{repo}/releases/download/v{version}/{asset}"

_CACHE_DIR_NAME = ".coderAI"
_BIN_SUBDIR = "bin"


# --- Platform detection ------------------------------------------------------


class UnsupportedPlatformError(RuntimeError):
    """Raised when we can't map the host to a known binary target."""


def detect_platform() -> str:
    """Return the platform slug used in the release asset filename.

    Examples: ``darwin-arm64``, ``darwin-x64``, ``linux-x64``, ``linux-arm64``,
    ``windows-x64``.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_slug = "darwin"
    elif system == "linux":
        os_slug = "linux"
    elif system in ("windows", "win32"):
        os_slug = "windows"
    else:
        raise UnsupportedPlatformError(
            f"Unsupported operating system: {platform.system()}"
        )

    if machine in ("arm64", "aarch64"):
        arch_slug = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch_slug = "x64"
    else:
        raise UnsupportedPlatformError(f"Unsupported architecture: {machine}")

    # We don't publish windows-arm64 yet; future-proof the error message.
    if os_slug == "windows" and arch_slug != "x64":
        raise UnsupportedPlatformError(
            f"No prebuilt binary for windows-{arch_slug}. "
            "Build from source with `make ui-compile`."
        )

    return f"{os_slug}-{arch_slug}"


def _asset_name(plat: str) -> str:
    """File name of the binary asset for *plat* on GitHub Releases."""
    suffix = ".exe" if plat.startswith("windows-") else ""
    return f"coderai-ui-{plat}{suffix}"


# --- Path resolution ---------------------------------------------------------


def cache_dir() -> Path:
    """``~/.coderAI/bin`` (created on demand by the caller)."""
    return Path.home() / _CACHE_DIR_NAME / _BIN_SUBDIR


def binary_path(version: str, plat: Optional[str] = None) -> Path:
    """Return the versioned cache path for the binary on this host."""
    plat = plat or detect_platform()
    suffix = ".exe" if plat.startswith("windows-") else ""
    return cache_dir() / f"coderai-ui-{plat}-v{version}{suffix}"


def local_dev_binary() -> Optional[Path]:
    """Return the checked-in dev build if present (``<repo>/ui/dist/coderai-ui``).

    Uses the installed package location to find the sibling ``ui/`` directory
    in editable installs / source checkouts. Returns ``None`` for wheel
    installs where ``ui/dist`` was not shipped.
    """
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir.parent / "ui" / "dist" / "coderai-ui",
        pkg_dir.parent / "ui" / "dist" / "coderai-ui.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


# --- Download + verify -------------------------------------------------------


def _read_url(url: str, timeout: float = 30.0) -> bytes:
    """GET *url* and return the body, raising a helpful error on failure."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "coderAI/binary_manager"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec: B310
        return resp.read()


def _download_with_progress(url: str, dest: Path, timeout: float = 120.0) -> None:
    """Stream *url* to *dest*, displaying a Rich progress bar."""
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "coderAI/binary_manager"},
    )
    dest.parent.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec: B310
        total = int(resp.headers.get("Content-Length", 0)) or None

        with Progress(
            TextColumn("[bold blue]Downloading UI"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("download", total=total)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))
            tmp.replace(dest)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_checksum_sidecar(body: bytes, asset_name: str) -> Optional[str]:
    """Extract a hex digest from a ``sha256sum``-style sidecar body.

    Accepts both ``<hex>  <file>\\n`` and ``<hex>\\n`` formats.
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 1 and len(parts[0]) == 64:
            return parts[0].lower()
        if len(parts) >= 2 and len(parts[0]) == 64:
            # "<hex>  <filename>" — if a filename is included, prefer the
            # matching one; otherwise fall back to the first entry.
            if parts[-1] in (asset_name, f"./{asset_name}", f"*{asset_name}"):
                return parts[0].lower()
    # No filename match: return the first 64-char hex token we saw.
    for line in text.splitlines():
        tok = line.strip().split()
        if tok and len(tok[0]) == 64:
            return tok[0].lower()
    return None


# --- Public API --------------------------------------------------------------


class BinaryUnavailableError(RuntimeError):
    """Could not locate or download the Ink UI binary."""


def _repo() -> str:
    return os.environ.get("CODERAI_UI_REPO", DEFAULT_REPO)


def _release_url(version: str, plat: str) -> str:
    return RELEASES_URL.format(
        repo=_repo(), version=version, asset=_asset_name(plat)
    )


def _checksum_url(version: str, plat: str) -> str:
    return RELEASES_URL.format(
        repo=_repo(), version=version, asset=_asset_name(plat) + ".sha256"
    )


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_binary(version: str, *, force: bool = False) -> Path:
    """Resolve the Ink UI binary, downloading it on demand.

    Args:
        version: The coderAI package version. Used to build the Release URL
            and the versioned cache path.
        force: If True, bypass the cache and re-download.

    Returns:
        Absolute path to an executable binary.

    Raises:
        BinaryUnavailableError: When no mechanism can supply the binary.
        UnsupportedPlatformError: When the host OS/arch has no published
            binary and no local build is available.
    """
    override = os.environ.get("CODERAI_UI_BINARY")
    if override:
        p = Path(override).expanduser().resolve()
        if not p.is_file():
            raise BinaryUnavailableError(
                f"$CODERAI_UI_BINARY points at {p} but no file exists there."
            )
        return p

    dev = local_dev_binary()
    if dev is not None:
        return dev

    plat = detect_platform()
    target = binary_path(version, plat)

    if target.exists() and not force:
        return target

    allow_unsigned = os.environ.get("CODERAI_ALLOW_UNSIGNED_BINARY") == "1"

    try:
        sha_url = _checksum_url(version, plat)
        asset_url = _release_url(version, plat)
        logger.info("Fetching Ink UI binary: %s", asset_url)

        sha_body: Optional[bytes] = None
        try:
            sha_body = _read_url(sha_url)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            if not allow_unsigned:
                raise BinaryUnavailableError(
                    f"No SHA256 sidecar found at {sha_url}. Refusing to "
                    "install an unverified binary. Re-run with "
                    "CODERAI_ALLOW_UNSIGNED_BINARY=1 to accept the risk, "
                    "or build from source with `make ui-compile`."
                ) from e
            logger.warning(
                "No SHA256 sidecar at %s (HTTP 404); continuing unverified "
                "because CODERAI_ALLOW_UNSIGNED_BINARY=1.",
                sha_url,
            )

        _download_with_progress(asset_url, target)

        if sha_body:
            expected = _parse_checksum_sidecar(sha_body, _asset_name(plat))
            if not expected:
                target.unlink(missing_ok=True)
                raise BinaryUnavailableError(
                    f"SHA256 sidecar at {sha_url} was present but unparseable. "
                    "Refusing to install an unverified binary."
                )
            actual = _sha256_of(target)
            if actual.lower() != expected:
                target.unlink(missing_ok=True)
                raise BinaryUnavailableError(
                    "Downloaded UI binary failed SHA256 verification "
                    f"(expected {expected}, got {actual}). The file has "
                    "been removed. Re-run or build from source with "
                    "`make ui-compile`."
                )
        elif not allow_unsigned:
            # Defense in depth: sha_body must be present here unless the
            # unsigned-escape branch above was taken.
            target.unlink(missing_ok=True)
            raise BinaryUnavailableError(
                "Internal error: reached binary install path without SHA256 "
                "verification. Binary removed."
            )

        _make_executable(target)
        return target

    except urllib.error.HTTPError as e:
        raise BinaryUnavailableError(
            f"Could not download UI binary (HTTP {e.code}): {e.reason}.\n"
            f"URL: {e.url}\n"
            "Build from source with `make ui-compile`, or point "
            "$CODERAI_UI_BINARY at a local build."
        ) from e
    except urllib.error.URLError as e:
        raise BinaryUnavailableError(
            f"Could not reach GitHub Releases: {e.reason}. "
            "Build from source with `make ui-compile`, or point "
            "$CODERAI_UI_BINARY at a local build."
        ) from e


def preflight_info(version: str) -> dict:
    """Return a dict of resolved paths / URLs for diagnostics (no downloads)."""
    info = {
        "override": os.environ.get("CODERAI_UI_BINARY"),
        "dev_binary": str(local_dev_binary()) if local_dev_binary() else None,
        "cache_dir": str(cache_dir()),
    }
    try:
        plat = detect_platform()
        info["platform"] = plat
        info["cached_path"] = str(binary_path(version, plat))
        info["release_url"] = _release_url(version, plat)
        info["checksum_url"] = _checksum_url(version, plat)
    except UnsupportedPlatformError as e:
        info["platform_error"] = str(e)
    return info


__all__ = [
    "BinaryUnavailableError",
    "UnsupportedPlatformError",
    "binary_path",
    "cache_dir",
    "detect_platform",
    "ensure_binary",
    "local_dev_binary",
    "preflight_info",
]
