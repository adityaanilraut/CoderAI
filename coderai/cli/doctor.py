"""Comprehensive system diagnostics and health inspector for CoderAI CLI."""

from __future__ import annotations

import os
import pathlib
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from coderai._version import __version__
from coderai.cli.welcome import get_git_status
from coderai.core.openai_client import resolve_model_provider_routing
from coderai.core.skill import list_skills

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


@dataclass
class DiagnosticItem:
    category: str
    name: str
    status: str  # "ok" | "warn" | "error"
    message: str
    remediation: str | None = None


@dataclass
class DoctorReport:
    items: list[DiagnosticItem] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(item.status == "error" for item in self.items)

    @property
    def has_warnings(self) -> bool:
        return any(item.status == "warn" for item in self.items)


def mask_secret(secret: str | None) -> str:
    """Mask secret API key showing only first 4 and last 3 characters."""
    if not secret:
        return "Not Set"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-3:]}"


def run_doctor_diagnostics(project_root: str, mgr: Any) -> DoctorReport:
    """Run full suite of environment, configuration, and connectivity diagnostics."""
    report = DoctorReport()

    # 1. Python Environment Check
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )
    if sys.version_info >= (3, 10):
        report.items.append(
            DiagnosticItem(
                category="Runtime",
                name="Python Version",
                status="ok",
                message=f"Python {py_ver} ({platform.system()} {platform.machine()})",
            )
        )
    else:
        report.items.append(
            DiagnosticItem(
                category="Runtime",
                name="Python Version",
                status="warn",
                message=f"Python {py_ver} (Python 3.10+ recommended)",
                remediation="Upgrade to Python 3.10 or newer.",
            )
        )

    report.items.append(
        DiagnosticItem(
            category="Runtime",
            name="Virtual Environment",
            status="ok" if in_venv else "warn",
            message=f"Active venv: {sys.prefix}" if in_venv else "Running outside a virtualenv",
            remediation="Consider activating a project virtual environment." if not in_venv else None,
        )
    )

    # 2. Git Workspace
    branch, is_dirty = get_git_status(project_root)
    git_bin = shutil.which("git")
    if git_bin and branch is not None:
        dirty_tag = " (uncommitted changes detected)" if is_dirty else " (clean working tree)"
        report.items.append(
            DiagnosticItem(
                category="Workspace",
                name="Git Repository",
                status="ok",
                message=f"Branch '{branch}'{dirty_tag}",
            )
        )
    elif git_bin:
        report.items.append(
            DiagnosticItem(
                category="Workspace",
                name="Git Repository",
                status="warn",
                message="Workspace is not a Git repository",
                remediation="Run 'git init' to enable rollback checkpoints and diff tracking.",
            )
        )
    else:
        report.items.append(
            DiagnosticItem(
                category="Workspace",
                name="Git Executable",
                status="error",
                message="git binary not found in PATH",
                remediation="Install git to enable checkpoints and diff features.",
            )
        )

    # 3. Model & LLM Provider Routing
    active_model = mgr.get_active_model() if hasattr(mgr, "get_active_model") else "gpt-5.6-luna"
    resolved_settings = mgr.get_resolved_settings() if hasattr(mgr, "get_resolved_settings") else {}
    base_url, api_key = resolve_model_provider_routing(
        active_model,
        explicit_base_url=resolved_settings.get("baseUrl") or resolved_settings.get("baseURL"),
        explicit_api_key=resolved_settings.get("apiKey") or resolved_settings.get("api_key"),
    )

    if api_key:
        report.items.append(
            DiagnosticItem(
                category="LLM / Provider",
                name=f"Active Model ({active_model})",
                status="ok",
                message=f"Endpoint: {base_url} • Key: {mask_secret(api_key)}",
            )
        )
    else:
        report.items.append(
            DiagnosticItem(
                category="LLM / Provider",
                name=f"Active Model ({active_model})",
                status="error",
                message=f"No API key resolved for model '{active_model}' (endpoint: {base_url})",
                remediation="Set DEEPSEEK_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY in your environment or ~/.coderai/settings.json.",
            )
        )

    # 4. MCP Servers
    mcp_mgr = getattr(mgr, "mcp_manager", None)
    if mcp_mgr:
        statuses = getattr(mcp_mgr, "server_statuses", [])
        active_clients = getattr(mcp_mgr, "clients", {}) or {}
        tools_count = len(getattr(mgr, "mcp_tool_definitions", []) or [])
        if not statuses and not active_clients:
            report.items.append(
                DiagnosticItem(
                    category="MCP Extensibility",
                    name="MCP Servers",
                    status="ok",
                    message="0 servers configured in .coderai/mcp.json",
                )
            )
        else:
            failed = [s for s in statuses if s.status == "error"]
            if failed:
                err_names = ", ".join(s.name for s in failed)
                report.items.append(
                    DiagnosticItem(
                        category="MCP Extensibility",
                        name="MCP Servers",
                        status="warn",
                        message=f"{len(active_clients)} connected, {len(failed)} failed ({err_names}) • {tools_count} external tools",
                        remediation="Use '/mcp reconnect <server>' or check mcp server logs in .coderai/mcp.json.",
                    )
                )
            else:
                report.items.append(
                    DiagnosticItem(
                        category="MCP Extensibility",
                        name="MCP Servers",
                        status="ok",
                        message=f"{len(active_clients)} server(s) connected • {tools_count} external tool(s) registered",
                    )
                )

    # 5. Skills Inventory
    try:
        discovered_skills = list_skills(project_root)
        report.items.append(
            DiagnosticItem(
                category="Skills & Guidance",
                name="Discovered Skills",
                status="ok",
                message=f"{len(discovered_skills)} skill(s) discovered across project & global paths",
            )
        )
    except Exception as err:
        report.items.append(
            DiagnosticItem(
                category="Skills & Guidance",
                name="Discovered Skills",
                status="warn",
                message=f"Error inspecting skills: {err}",
            )
        )

    # 6. Storage & Permissions
    dot_coderai = pathlib.Path(project_root) / ".coderai"
    home_coderai = pathlib.Path.home() / ".coderai"
    can_write_project = False
    try:
        dot_coderai.mkdir(parents=True, exist_ok=True)
        test_file = dot_coderai / ".doctor_probe"
        test_file.write_text("ok")
        test_file.unlink()
        can_write_project = True
    except Exception:
        can_write_project = False

    can_write_home = False
    try:
        home_coderai.mkdir(parents=True, exist_ok=True)
        test_home_file = home_coderai / ".doctor_probe"
        test_home_file.write_text("ok")
        test_home_file.unlink()
        can_write_home = True
    except Exception:
        can_write_home = False

    if can_write_project and can_write_home:
        report.items.append(
            DiagnosticItem(
                category="Persistence",
                name="Storage Permissions",
                status="ok",
                message="Workspace (.coderai) and user home (~/.coderai) are writable",
            )
        )
    else:
        report.items.append(
            DiagnosticItem(
                category="Persistence",
                name="Storage Permissions",
                status="error",
                message=f"Write permission check failed (project={can_write_project}, home={can_write_home})",
                remediation="Ensure write permissions for workspace directory and ~/.coderai.",
            )
        )

    # 7. Background Jobs & Timers
    job_store = getattr(mgr, "job_store", None)
    sched_mgr = getattr(mgr, "schedule_manager", None)
    active_jobs_cnt = 0
    if job_store:
        active_jobs_cnt = len(
            [j for j in getattr(job_store, "_jobs", {}).values() if j.status == "running"]
        )
    active_sched_cnt = 0
    if sched_mgr:
        active_sched_cnt = len(
            [s for s in getattr(sched_mgr, "_schedules", {}).values() if s.state == "scheduled"]
        )
    report.items.append(
        DiagnosticItem(
            category="Background Engine",
            name="Jobs & Schedules",
            status="ok",
            message=f"{active_jobs_cnt} active background job(s) • {active_sched_cnt} scheduled reminder(s)",
        )
    )

    return report


def render_doctor(console: Any | None, report: DoctorReport) -> None:
    """Render the Doctor diagnostics report nicely in the console."""
    if console is not None and _RICH and Table is not None and Panel is not None:
        table = Table(title="[bold cyan]CoderAI System Doctor Diagnostics[/]", border_style="bright_blue", expand=True)
        table.add_column("Category", style="dim", width=18)
        table.add_column("Check / Resource", style="bold white", width=24)
        table.add_column("Status", width=10, justify="center")
        table.add_column("Details & Notes", style="white")

        for item in report.items:
            if item.status == "ok":
                status_badge = "[bold green]✓ PASS[/]"
            elif item.status == "warn":
                status_badge = "[bold yellow]▲ WARN[/]"
            else:
                status_badge = "[bold red]✗ FAIL[/]"

            details = item.message
            if item.remediation:
                details += f"\n[dim yellow]→ Fix: {item.remediation}[/]"

            table.add_row(item.category, item.name, status_badge, details)

        console.print()
        console.print(table)
        console.print()
    else:
        print("\n=== CoderAI System Doctor Diagnostics ===")
        for item in report.items:
            tag = "PASS" if item.status == "ok" else ("WARN" if item.status == "warn" else "FAIL")
            print(f"[{tag:4}] {item.category} / {item.name}: {item.message}")
            if item.remediation:
                print(f"       Fix: {item.remediation}")
        print()
