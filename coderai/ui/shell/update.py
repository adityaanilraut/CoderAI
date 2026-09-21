from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import sys
from enum import Enum, auto
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

from coderai._version import __version__ as current_version
from coderai.share import get_share_dir
from coderai.ui.shell.console import console
from coderai.utils.aiohttp import new_client_session
from coderai.utils.logging import logger

UPGRADE_COMMAND = "pip install -U coderai-agent"
PYPI_URL = "https://pypi.org/pypi/coderai-agent/json"

LATEST_VERSION_FILE = get_share_dir() / "latest_version.txt"
SKIPPED_VERSION_FILE = get_share_dir() / "skipped_version.txt"


class UpdateResult(Enum):
    UPDATE_AVAILABLE = auto()
    UPDATED = auto()
    UP_TO_DATE = auto()
    FAILED = auto()
    UNSUPPORTED = auto()


_UPDATE_LOCK = asyncio.Lock()


def semver_tuple(version: str) -> tuple[int, int, int]:
    """Parse semver string into a comparable 3-tuple (major, minor, patch)."""
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", v)
    if not match:
        return (0, 0, 0)
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _fetch_latest_version() -> str | None:
    """Check PyPI for the latest published coderai-agent release."""
    try:
        async with new_client_session() as session:
            async with session.get(PYPI_URL, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    version = data.get("info", {}).get("version")
                    if version:
                        with contextlib.suppress(OSError):
                            LATEST_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                            LATEST_VERSION_FILE.write_text(version, encoding="utf-8")
                        return str(version)
    except Exception as exc:
        logger.debug("Failed to query PyPI version: {exc}", exc=exc)
    return None


async def do_update(*, print: bool = True, check_only: bool = False) -> UpdateResult:
    """Check or execute package update."""
    async with _UPDATE_LOCK:

        def _print(msg: str) -> None:
            if print:
                console.print(msg)

        cur_v = current_version
        cached = _read_text_file(LATEST_VERSION_FILE)
        latest_version = await _fetch_latest_version() or cached or cur_v

        cur_t = semver_tuple(cur_v)
        lat_t = semver_tuple(latest_version)

        if cur_t >= lat_t:
            logger.debug("Already up to date: {cur_v}", cur_v=cur_v)
            _print(f"[green]CoderAI is up to date (v{cur_v}).[/green]")
            return UpdateResult.UP_TO_DATE

        if check_only:
            logger.info(
                "Update available: current={cur_v}, latest={latest_version}",
                cur_v=cur_v,
                latest_version=latest_version,
            )
            _print(f"[yellow]Update available: v{latest_version} (current: v{cur_v})[/yellow]")
            _print(f"Run [bold cyan]{UPGRADE_COMMAND}[/] to upgrade.")
            return UpdateResult.UPDATE_AVAILABLE

        _print(f"Upgrading CoderAI from v{cur_v} to v{latest_version}...")
        try:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "coderai-agent"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                _print(f"[bold green]✓ Upgraded successfully to v{latest_version}![/]")
                _print("[dim]Restart CoderAI to use the new version.[/]")
                return UpdateResult.UPDATED
            else:
                err_msg = stderr.decode().strip() or stdout.decode().strip()
                logger.error("Upgrade failed: {err_msg}", err_msg=err_msg)
                _print(f"[red]Upgrade failed: {err_msg}[/red]")
                return UpdateResult.FAILED
        except Exception as e:
            logger.exception("Failed to execute upgrade:")
            _print(f"[red]Upgrade execution error: {e}[/red]")
            return UpdateResult.FAILED


async def check_update_gate() -> None:
    """Non-blocking update check during interactive startup."""
    from coderai.utils.envvar import get_env_bool

    if get_env_bool("CODERAI_NO_AUTO_UPDATE"):
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return

    latest_version = _read_text_file(LATEST_VERSION_FILE)
    if (
        latest_version
        and semver_tuple(latest_version) > semver_tuple(current_version)
        and _read_text_file(SKIPPED_VERSION_FILE) != latest_version
    ):
        panel = Panel(
            Text.assemble(
                ("A new version of CoderAI is available: ", "default"),
                (f"v{latest_version}\n", "bold cyan"),
                ("Run ", "dim"),
                ("/upgrade", "bold green"),
                (" or ", "dim"),
                (UPGRADE_COMMAND, "bold"),
                (" to update.", "dim"),
            ),
            title="[bold yellow]Update Available[/]",
            border_style="yellow",
        )
        console.print(panel)
