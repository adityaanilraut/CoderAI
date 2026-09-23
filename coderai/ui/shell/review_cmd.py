"""/review: Jev triage -> main-model review -> Jev comment gate, rendered in the terminal."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text

from coderai.triage.review import (
    DEFAULT_LLM_TIMEOUT_S,
    ReviewError,
    ReviewFinding,
    ReviewReport,
    run_review,
)

USAGE = "Usage: /review [<base-ref>] [--all] [--no-untracked]"
COLLAPSE_SKIPPED_ABOVE = 30

_SEVERITY_STYLE = {"critical": "bold red", "major": "bold yellow", "minor": "cyan"}
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}


def parse_review_args(args: str) -> tuple[str | None, bool, bool] | None:
    """Return (base, review_all, include_untracked), or None for invalid input.

    Untracked files are excluded by default (WF-B5: a stray `.env.local`
    must not leak to the reviewer); pass `--untracked` to opt in.
    """
    base: str | None = None
    review_all = False
    include_untracked = False
    for token in args.split():
        if token == "--all":
            review_all = True
        elif token == "--untracked":
            include_untracked = True
        elif token == "--no-untracked":
            include_untracked = False
        elif token.startswith("-") or base is not None:
            return None
        else:
            base = token
    return base, review_all, include_untracked


def _out(console: Any, markup: str) -> None:
    if console is not None:
        console.print(markup)
    else:
        print(Text.from_markup(markup).plain)


def make_completer(mgr: Any, timeout_s: float = DEFAULT_LLM_TIMEOUT_S):
    """Blocking main-model call used by the review stage (no tools, no session writes)."""

    def complete(messages: list[dict[str, str]]) -> str:
        from coderai.llm import create_openai_client

        info = (
            mgr.create_openai_client()
            if hasattr(mgr, "create_openai_client")
            else create_openai_client(getattr(mgr, "project_root", "."))
        )
        client = info.get("client")
        if client is None:
            if info.get("baseURL") == "jev://system-one":
                raise ReviewError(
                    "the active model is Jev System-One, which cannot write reviews; "
                    "switch to a chat model with /model"
                )
            raise ReviewError("no API key for the active model; configure one with /setup")
        resp = client.chat.completions.create(
            model=info.get("model"),
            messages=messages,  # type: ignore[arg-type]
            temperature=0.1,
            timeout=timeout_s,
        )
        try:
            return (resp.choices[0].message.content or "").strip()
        except (AttributeError, IndexError):
            return ""

    return complete


def _decision(report: ReviewReport, path: str, should_review: bool, reason: str) -> str:
    if path in report.over_budget:
        return "[yellow]not sent (over budget)[/]"
    if path in report.reviewed:
        return "[green]review[/]" if should_review else "[green]review (--all)[/]"
    if reason.startswith("Bypassed"):
        return "[dim]skip (docs/asset)[/]"
    return "[dim]skip (low risk)[/]"


def _why(t: Any) -> str:
    if t.reason.startswith("Jev screening"):
        conf = f" {t.category_confidence:.2f}" if t.category_confidence is not None else ""
        extra = ", truncated" if t.truncated else ""
        return f"risk {t.risk:.2f} · {t.category}{conf} · P{t.priority}{extra}"
    if t.reason.startswith("Bypassed"):
        return "doc/asset extension"
    if t.reason.startswith("Local:"):
        return "secret-looking path, not sent to Jev"
    return "Jev unavailable (fail-closed)"


def render_triage(console: Any, report: ReviewReport) -> None:
    rows = list(zip(report.files, report.triage))
    skipped = 0
    if len(rows) > COLLAPSE_SKIPPED_ABOVE:
        shown = [
            (f, t) for f, t in rows if f.path in report.reviewed or f.path in report.over_budget
        ]
        skipped = len(rows) - len(shown)
        rows = shown
    if console is not None:
        table = Table(title=f"Jev triage vs {escape(report.base)}", show_lines=False)
        table.add_column("File", style="cyan", overflow="fold")
        table.add_column("Decision")
        table.add_column("Why", style="dim")
        for f, t in rows:
            table.add_row(
                escape(f.path),
                _decision(report, f.path, t.should_review, t.reason),
                escape(_why(t)),
            )
        console.print(table)
    else:
        print(f"Jev triage vs {report.base}:")
        for f, t in rows:
            decision = Text.from_markup(_decision(report, f.path, t.should_review, t.reason)).plain
            print(f"  {f.path}: {decision} ({_why(t)})")
    if skipped:
        _out(
            console, f"[dim]{skipped} more file(s) skipped by triage (docs/assets or low risk).[/]"
        )
    if report.over_budget:
        _out(
            console,
            f"[yellow]{len(report.over_budget)} flagged file(s) did not fit the review budget.[/] "
            "[dim]Narrow the diff (e.g. /review <base-ref>) or raise CODERAI_REVIEW_MAX_CHARS.[/]",
        )


def _location(f: ReviewFinding) -> str:
    return f"{f.file}:{f.line}" if f.line else f.file


def _gate_scores(f: ReviewFinding) -> str:
    g = f.gate
    if g is None:
        return "file not in diff, cannot be grounded"
    if g.reason.startswith("Local:"):
        return "secret-looking path, not sent to Jev"
    if not g.reason.startswith("Jev gate"):
        return "Jev unavailable (fail-open)"
    spec = f" · speculative {g.is_speculative:.2f}" if g.is_speculative is not None else ""
    trunc = " · diff truncated" if g.truncated else ""
    kept = " · below gate, kept (critical)" if not g.passed else ""
    return (
        f"bug {g.is_actionable_bug:.2f} · accept {g.will_developer_accept:.2f}{spec}{trunc}{kept}"
    )


def render_findings(console: Any, report: ReviewReport) -> None:
    if report.parse_failed:
        _out(
            console,
            "[yellow]The model's reply had no structured findings, so the Jev gate was skipped. "
            "Showing its review as-is:[/]",
        )
        _out(console, escape(report.raw_review or "(empty reply)"))
        return

    if not report.findings:
        _out(console, "[bold green]No findings survived review.[/]")
    else:
        _out(console, f"\n[bold]Findings ({len(report.findings)})[/]")
        for f in sorted(report.findings, key=lambda x: _SEVERITY_RANK.get(x.severity, 3)):
            style = _SEVERITY_STYLE.get(f.severity, "white")
            _out(console, f"[{style}]\\[{f.severity}][/] [cyan]{escape(_location(f))}[/]")
            _out(console, f"  {escape(f.comment)}")
            _out(console, f"  [dim]jev: {escape(_gate_scores(f))}[/]")

    if report.dropped:
        _out(console, f"\n[dim]Dropped by Jev gate ({len(report.dropped)}):[/]")
        for f in report.dropped:
            snippet = " ".join(f.comment.split())[:100]
            _out(
                console,
                f"[dim]  {escape(_location(f))} — {escape(_gate_scores(f))}: {escape(snippet)}[/]",
            )


def _jev_unavailable_reason() -> str | None:
    from coderai.jev.client import jev_is_available
    from coderai.triage.engine import is_jev_configured, jev_sdk_installed

    if jev_is_available():
        return None
    if not jev_sdk_installed():
        return "typesafe-sdk is not installed (pip install 'coderai-agent[jev]')"
    if not is_jev_configured():
        return "TYPESAFE_API_KEY is not set"
    return "the TypeSafe client could not be created"


async def run_review_command(mgr: Any, console: Any, args: str) -> None:
    parsed = parse_review_args(args)
    if parsed is None:
        _out(console, USAGE)
        return
    base, review_all, include_untracked = parsed

    reason = _jev_unavailable_reason()
    if reason:
        _out(
            console,
            f"[dim]Jev System-One is off ({escape(reason)}), so every non-doc file "
            "is reviewed and every comment is shown.[/]",
        )

    try:
        report = await run_review(
            getattr(mgr, "project_root", "."),
            make_completer(mgr),
            base=base,
            review_all=review_all,
            include_untracked=include_untracked,
            progress=lambda msg: _out(console, f"[dim]{escape(msg)}[/]"),
        )
    except ReviewError as exc:
        _out(console, f"[red]Review failed: {escape(str(exc))}[/]")
        return

    if not report.files:
        _out(console, f"[dim]No changes against {escape(report.base)}.[/]")
        return
    render_triage(console, report)
    if not report.reviewed:
        _out(
            console,
            "[bold green]Jev found nothing worth a full review.[/] Use /review --all to force one.",
        )
        return
    render_findings(console, report)
