"""Pluggable statusline engine — port of deepcode statusline subsystem.

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

from coderai.core.settings import get_default_context_window

try:
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Text = None  # type: ignore[assignment,misc]
    _RICH = False

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def get_git_branch_cached(project_root: str) -> str:
    """Get the active git branch name with dirty status marker if uncommitted changes exist, or 'no-git'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=1,
        )
        if res.returncode == 0 and res.stdout.strip():
            branch = res.stdout.strip()
            # Check dirty status
            res_dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=1,
            )
            if res_dirty.returncode == 0 and res_dirty.stdout.strip():
                return f"{branch}*"
            return branch
    except Exception:
        pass
    return "no-git"


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
    ) -> Text | str:
        """Format the default dynamic powerline gauge."""
        plan_label = "ON" if plan_mode else "OFF"
        tokens_display, token_style, pct = compute_token_gauge(active_tokens, model)
        mini_bar = make_mini_bar(pct, width=6)

        if not _RICH or Text is None:
            extra = ""
            if turns > 0:
                extra += f" [Turns: {turns}]"
            if mcp_count > 0:
                extra += f" [MCP: {mcp_count}]"
            return f"[Model: {model}] [Tokens: {tokens_display}] [Plan: {plan_label}] [Git: {branch}]{extra}"

        bar = Text()
        # Model Segment
        bar.append(" ", style="default")
        bar.append(f"Model: {model}", style="bold cyan")

        # Divider
        bar.append(" │ ", style="dim")

        # Tokens Segment with Mini Bar
        base_color = token_style.replace("bold ", "")
        bar.append(f"Tokens: {tokens_display}", style=f"bold {base_color}")
        bar.append(f" [{mini_bar}]", style=f"dim {base_color}")

        # Divider
        bar.append(" │ ", style="dim")

        # Plan Mode Segment
        bar.append("Plan: ", style="dim")
        bar.append(plan_label, style="bold yellow" if plan_mode else "dim white")

        # Divider
        bar.append(" │ ", style="dim")

        # Git Branch Segment
        bar.append("Git: ", style="dim magenta")
        bar.append(branch, style="bold magenta")

        # Optional Turns Segment
        if turns > 0:
            bar.append(" │ ", style="dim")
            bar.append(f"Turns: {turns}", style="bold white")

        # Optional MCP Segment
        if mcp_count > 0:
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
        provider_cfg = self._get_provider_config()

        if provider_cfg:
            ptype = provider_cfg.get("type", "command")
            ttl = float(provider_cfg.get("ttl", self._default_ttl))

            if ptype == "command" and provider_cfg.get("command"):
                custom_text = self.execute_command_provider(
                    provider_cfg["command"], project_root, ttl=ttl
                )
                if custom_text:
                    if console is not None and _RICH:
                        console.print()
                        console.print(Text(f" {custom_text}", style="dim cyan"))
                    else:
                        print(f"\n {custom_text}")
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
                    if console is not None and _RICH:
                        console.print()
                        console.print(Text(f" {custom_text}", style="dim cyan"))
                    else:
                        print(f"\n {custom_text}")
                    return

        # Fallback to default statusline
        branch = get_git_branch_cached(project_root)
        bar = self.format_default_status_bar(
            model, active_tokens, plan_mode, branch, turns=turns, mcp_count=mcp_count
        )
        if console is not None and _RICH:
            console.print()
            console.print(bar)
        else:
            print(f"\n{bar}")


_DEFAULT_ENGINE = StatuslineEngine()


def format_default_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
    turns: int = 0,
    mcp_count: int = 0,
) -> Text | str:
    """Format the default dynamic gauge via module helper."""
    return _DEFAULT_ENGINE.format_default_status_bar(
        model, active_tokens, plan_mode, branch, turns=turns, mcp_count=mcp_count
    )


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
