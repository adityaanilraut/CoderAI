"""Pluggable statusline engine

Supports:
- Default dynamic gauge (Model, Tokens, Context Window %, Plan Mode, Git Branch, Turns, MCP count)
- Configurable `command` provider (executes shell command with timeout, ANSI stripping, and TTL cache)
- Configurable `module` provider (loads python module/callable or function with TTL cache)
- ANSI escape stripping and refresh timers
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.text import Text

from coderai.core.settings import get_default_context_window

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_PATTERN.sub("", text)


_GIT_STATUS_CACHE: dict[str, tuple[float, str | None, bool, int, int]] = {}
_GIT_CACHE_TTL = 3.0  # seconds


def get_git_status(project_root: str) -> tuple[str | None, bool]:
    """Retrieve the current active git branch and dirty status with TTL caching."""
    now = time.monotonic()
    cached = _GIT_STATUS_CACHE.get(project_root)
    if cached and now - cached[0] < _GIT_CACHE_TTL:
        return cached[1], cached[2]

    branch: str | None = None
    is_dirty = False
    ahead = behind = 0
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "-b"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if res.returncode == 0:
            lines = res.stdout.splitlines()
            if lines:
                first = lines[0]
                if first.startswith("## "):
                    header = first[3:].strip()
                    # e.g. "main...origin/main [ahead 1, behind 2]" or "main" or "HEAD (no branch)"
                    if "..." in header:
                        branch_part = header.split("...", 1)[0].strip()
                    else:
                        branch_part = header.split(" ", 1)[0].strip()
                    if branch_part and branch_part != "HEAD (no branch)":
                        branch = branch_part

                    m = re.search(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]", first)
                    if m:
                        ahead = int(m.group(1) or 0)
                        behind = int(m.group(2) or 0)
                if len(lines) > 1 or (len(lines) == 1 and not lines[0].startswith("## ")):
                    is_dirty = True
        else:
            # Fallback simple branch query
            res_b = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res_b.returncode == 0 and res_b.stdout.strip():
                branch = res_b.stdout.strip()
    except Exception:
        pass

    _GIT_STATUS_CACHE[project_root] = (now, branch, is_dirty, ahead, behind)
    return branch, is_dirty


def get_git_branch(project_root: str) -> str | None:
    """Retrieve the current active git branch name formatted with dirty flag, or None."""
    branch, is_dirty = get_git_status(project_root)
    if branch:
        return f"{branch}*" if is_dirty else branch
    return None


def get_git_branch_cached(project_root: str) -> str:
    """Get the active git branch name with dirty status marker if uncommitted changes exist, or 'no-git'."""
    b = get_git_branch(project_root)
    return b if b else "no-git"


def get_git_detailed_status(project_root: str) -> tuple[str | None, bool, int, int]:
    """Retrieve branch, dirty, ahead, behind with caching."""
    get_git_status(project_root)
    cached = _GIT_STATUS_CACHE.get(project_root)
    if cached:
        return cached[1], cached[2], cached[3], cached[4]
    return None, False, 0, 0


def make_mini_bar(pct: float, width: int = 8) -> str:
    """Generate a compact unicode progress bar."""
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    return "■" * filled + "□" * (width - filled)


def compute_token_gauge(active_tokens: int, model: str) -> tuple[str, str, float]:
    """Compute token display string, color style, and percentage used of context window."""
    ctx_window = get_default_context_window(model)
    pct = (active_tokens / ctx_window * 100) if ctx_window > 0 else 0.0

    if ctx_window >= 1024 * 1024:
        window_str = f"{ctx_window // (1024 * 1024)}M"
    elif ctx_window >= 1024:
        window_str = f"{ctx_window // 1024}k"
    else:
        window_str = str(ctx_window)

    pct_formatted = f"{pct:.0f}%" if pct >= 1 or active_tokens == 0 else f"{pct:.1f}%"
    display = f"{active_tokens:,} ({pct_formatted} of {window_str})"

    if pct < 60:
        style = "green"
    elif pct < 80:
        style = "yellow"
    else:
        style = "bold red"

    return display, style, pct


@dataclass
class StatuslineCacheEntry:
    value: str
    timestamp: float


class StatuslineEngine:
    """Pluggable statusline engine supporting custom command and module providers."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self._cache: dict[str, StatuslineCacheEntry] = {}
        self._default_ttl: float = 3.0  # seconds

    def _get_provider_config(self) -> dict[str, Any] | None:
        cfg = self.settings.get("statusline")
        if isinstance(cfg, dict):
            return cfg
        if isinstance(cfg, str):
            return {"type": "command", "command": cfg}
        return None

    def execute_command_provider(
        self, command: str, project_root: str, ttl: float | None = None
    ) -> str:
        """Run a configured command provider with caching and ANSI stripping."""
        cache_key = f"cmd:{command}:{project_root}"
        now = time.time()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        cached = self._cache.get(cache_key)
        if cached and (now - cached.timestamp) < effective_ttl:
            return cached.value

        env = dict(os.environ)
        env["PAGER"] = "cat"
        env["NO_COLOR"] = "1"

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=2.0,
                env=env,
            )
            raw = res.stdout.strip() if res.returncode == 0 else ""
            clean = strip_ansi(raw).replace("\r\n", " ").replace("\n", " ").strip()
            self._cache[cache_key] = StatuslineCacheEntry(value=clean, timestamp=now)
            return clean
        except Exception:
            return ""

    def execute_module_provider(
        self, module_spec: str, context: dict[str, Any], ttl: float | None = None
    ) -> str:
        """Run a configured python module provider with caching and ANSI stripping."""
        cache_key = f"mod:{module_spec}"
        now = time.time()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        cached = self._cache.get(cache_key)
        if cached and (now - cached.timestamp) < effective_ttl:
            return cached.value

        try:
            if ":" in module_spec:
                mod_name, func_name = module_spec.split(":", 1)
            else:
                mod_name, func_name = module_spec, "render_statusline"

            mod = importlib.import_module(mod_name)
            func = getattr(mod, func_name, None)
            if callable(func):
                res = func(context)
                clean = strip_ansi(str(res or "")).replace("\r\n", " ").replace("\n", " ").strip()
                self._cache[cache_key] = StatuslineCacheEntry(value=clean, timestamp=now)
                return clean
        except Exception:
            pass
        return ""

    def format_default_status_bar(
        self,
        model: str,
        active_tokens: int,
        plan_mode: bool,
        branch: str,
        turns: int = 0,
        mcp_count: int = 0,
        term_width: int = 80,
    ) -> Text:
        """Format the default dynamic powerline gauge, adapting gracefully to terminal width."""
        plan_label = "ON" if plan_mode else "OFF"
        tokens_display, token_style, pct = compute_token_gauge(active_tokens, model)
        mini_bar = make_mini_bar(pct, width=5)

        is_compact = term_width < 80
        is_very_compact = term_width < 60

        bar = Text()
        # Model Segment
        bar.append(" ", style="default")
        bar.append(f"Model: {model}", style="bold cyan")

        # Divider
        bar.append(" │ ", style="dim")

        # Tokens Segment with Mini Bar
        base_color = token_style.replace("bold ", "")
        if is_very_compact:
            bar.append(f"{pct:.0f}% ctx", style=f"bold {base_color}")
        elif is_compact:
            bar.append(f"Tokens: {tokens_display}", style=f"bold {base_color}")
        else:
            bar.append(f"Tokens: {tokens_display}", style=f"bold {base_color}")
            bar.append(f" [{mini_bar}]", style=f"dim {base_color}")

        # Plan Mode Segment
        bar.append(" │ ", style="dim")
        bar.append("Plan: ", style="dim")
        bar.append(plan_label, style="bold yellow" if plan_mode else "dim")

        # Git Branch Segment
        if branch and branch != "no-git":
            bar.append(" │ ", style="dim")
            bar.append("Git: ", style="dim magenta")
            bar.append(branch, style="bold magenta")

        # Optional Turns Segment (only if ample width)
        if turns > 0 and not is_compact:
            bar.append(" │ ", style="dim")
            bar.append(f"Turns: {turns}", style="bold")

        # Optional MCP Segment (only if ample width)
        if mcp_count > 0 and not is_compact:
            bar.append(" │ ", style="dim")
            bar.append(f"MCP: {mcp_count}", style="bold green")

        return bar

    def render(
        self,
        console: Any | None,
        model: str,
        active_tokens: int,
        plan_mode: bool,
        project_root: str,
        turns: int = 0,
        mcp_count: int = 0,
    ) -> None:
        """Render the active statusline line above the prompt."""
        active_console = console or Console()
        provider_cfg = self._get_provider_config()

        if provider_cfg:
            ptype = provider_cfg.get("type", "command")
            ttl = float(provider_cfg.get("ttl", self._default_ttl))

            if ptype == "command" and provider_cfg.get("command"):
                custom_text = self.execute_command_provider(
                    provider_cfg["command"], project_root, ttl=ttl
                )
                if custom_text:
                    active_console.print()
                    active_console.print(Text(f" {custom_text}", style="dim cyan"))
                    return
            elif ptype == "module" and provider_cfg.get("module"):
                ctx = {
                    "model": model,
                    "active_tokens": active_tokens,
                    "plan_mode": plan_mode,
                    "project_root": project_root,
                    "turns": turns,
                    "mcp_count": mcp_count,
                }
                custom_text = self.execute_module_provider(provider_cfg["module"], ctx, ttl=ttl)
                if custom_text:
                    active_console.print()
                    active_console.print(Text(f" {custom_text}", style="dim cyan"))
                    return

        # Fallback to default statusline
        import shutil

        term_width = 80
        if (
            active_console is not None
            and isinstance(getattr(active_console, "width", None), int)
            and active_console.width > 0
        ):
            term_width = active_console.width
        else:
            term_width = shutil.get_terminal_size(fallback=(80, 24)).columns

        branch = get_git_branch_cached(project_root)
        bar = self.format_default_status_bar(
            model,
            active_tokens,
            plan_mode,
            branch,
            turns=turns,
            mcp_count=mcp_count,
            term_width=term_width,
        )
        active_console.print(bar)


_DEFAULT_ENGINE = StatuslineEngine()


def format_default_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
    turns: int = 0,
    mcp_count: int = 0,
    term_width: int = 80,
) -> Text:
    """Format the default dynamic gauge via module helper."""
    return _DEFAULT_ENGINE.format_default_status_bar(
        model,
        active_tokens,
        plan_mode,
        branch,
        turns=turns,
        mcp_count=mcp_count,
        term_width=term_width,
    )


def format_streamlined_status_bar(
    model: str,
    active_tokens: int,
    project_root: str,
    thinking_level: str = "high",
    plan_mode: bool = False,
    branch: str = "",
    term_width: int = 80,
) -> Text:
    """Format a clean, gold-standard 2-row or 1-row status bar matching screenshot aesthetics."""
    # Shorten CWD: replace user home with ~
    import os

    home = os.path.expanduser("~")
    display_cwd = project_root
    if project_root.startswith(home):
        display_cwd = "~" + project_root[len(home) :]

    ctx_window = get_default_context_window(model)
    pct = (active_tokens / ctx_window * 100) if ctx_window > 0 else 0.0

    if ctx_window >= 1024 * 1024:
        max_str = f"{ctx_window // (1024 * 1024)}M"
    elif ctx_window >= 1024:
        max_str = f"{ctx_window // 1024}k"
    else:
        max_str = str(ctx_window)

    tok_str = f"{active_tokens / 1000:.1f}k" if active_tokens >= 1000 else str(active_tokens)

    bar = Text()
    # Left segment: Model, thinking, cwd, git branch
    bar.append(f"{model} ", style="bold white")
    if thinking_level:
        bar.append(f"thinking: {thinking_level} ", style="dim")
    if plan_mode:
        bar.append("plan ", style="bold #38bdf8")
    bar.append(f"{display_cwd}", style="dim cyan")
    if branch and branch != "no-git":
        bar.append(f" ({branch})", style="dim magenta")

    # Right segment: Shortcut hints
    hints = "@: mention files | ! to run a shell command"
    left_len = len(bar.plain)
    hints_len = len(hints)
    gap = max(2, term_width - left_len - hints_len)
    bar.append(" " * gap)
    bar.append(hints, style="dim")

    # Second line: context gauge right-aligned
    ctx_info = f"context: {pct:.0f}% ({tok_str}/{max_str})"
    ctx_gap = max(0, term_width - len(ctx_info))
    bar.append("\n" + " " * ctx_gap)
    bar.append(ctx_info, style="dim")

    return bar


def render_statusline(
    console: Any | None,
    model: str,
    active_tokens: int,
    plan_mode: bool,
    project_root: str,
    turns: int = 0,
    mcp_count: int = 0,
    settings: dict[str, Any] | None = None,
) -> None:
    """Render statusline using configured or default engine."""
    engine = StatuslineEngine(settings) if settings else _DEFAULT_ENGINE
    engine.render(
        console,
        model,
        active_tokens,
        plan_mode,
        project_root,
        turns=turns,
        mcp_count=mcp_count,
    )
