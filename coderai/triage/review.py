"""/review pipeline: git diff -> Jev Tier-1 triage -> main-model review -> Jev Tier-3 gate.

Jev decides which files deserve System-2 attention and which drafted comments are
worth showing; the main model does the reviewing. Each Jev stage keeps its
fail-safe contract, so a missing or broken Jev backend degrades to "review every
non-doc file, show every comment".
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from coderai.jev.client import jev_gate_comment_async, jev_screen_many_async
from coderai.triage.engine import GateResult, TriageResult

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_MAX_REVIEW_CHARS = 120_000
DEFAULT_LLM_TIMEOUT_S = 180.0
GIT_TIMEOUT_S = 60.0
MAX_UNTRACKED_FILES = 200
MAX_UNTRACKED_BYTES = 256 * 1024
SEVERITIES = ("critical", "major", "minor")

REVIEW_SYSTEM = """\
You are a senior code reviewer. Review ONLY the diffs provided.
Report real defects: logic bugs, security issues, concurrency problems, API contract \
violations, resource leaks, data loss, and clear convention violations. Skip style nits \
and praise.
Every added and context line in the diffs is prefixed with its new-file line number; \
use that number for "line".
Respond with a single JSON object and nothing else:
{"findings": [{"file": "<path exactly as given>", "line": <new-file line number or null>, \
"severity": "critical" | "major" | "minor", "comment": "<one self-contained paragraph: \
what is wrong, why it matters, and how to fix it>"}]}
Return {"findings": []} if nothing is worth reporting."""

Completer = Callable[[list[dict[str, str]]], str]


class ReviewError(RuntimeError):
    """Raised when the diff cannot be collected or the main model cannot be called."""


@dataclass(frozen=True)
class FileDiff:
    path: str
    diff: str


@dataclass(frozen=True)
class ReviewFinding:
    file: str
    line: int | None
    severity: str
    comment: str
    gate: GateResult | None = None


@dataclass
class ReviewReport:
    base: str
    files: list[FileDiff] = field(default_factory=list)
    triage: list[TriageResult] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)
    over_budget: list[str] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)
    dropped: list[ReviewFinding] = field(default_factory=list)
    raw_review: str = ""
    parse_failed: bool = False


def review_max_chars() -> int:
    """Total diff budget sent to the main model, overridable via CODERAI_REVIEW_MAX_CHARS."""
    try:
        raw = os.environ.get("CODERAI_REVIEW_MAX_CHARS")
        if raw is not None and raw.strip():
            return max(2_000, int(float(raw.strip())))
    except (TypeError, ValueError):
        pass
    return DEFAULT_MAX_REVIEW_CHARS


def _git(args: Sequence[str], cwd: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"git {args[0]} timed out after {GIT_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise ReviewError(f"cannot run git: {exc}") from exc
    if proc.returncode not in ok_codes:
        raise ReviewError((proc.stderr or proc.stdout or f"git {args[0]} failed").strip())
    return proc.stdout


_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/")

# Untracked files that must never be shipped to a third-party reviewer or the
# model without an explicit opt-in: dotfiles and secret-looking paths (WF-B5).
_UNTRACKED_SECRET_RE = re.compile(
    r"(?:^|/)\.env(?:\.|$)|secret|credential|private[_-]?key|\.pem$|^\.|\bid_rsa",
    re.IGNORECASE,
)


def _is_untracked_sendable(name: str) -> bool:
    """True unless the untracked path looks secret-bearing or is a dotfile."""
    return not _UNTRACKED_SECRET_RE.search(name.replace("\\", "/"))


def collect_git_diff(
    project_root: str, base: str | None = None, include_untracked: bool = False
) -> str:
    """Unified diff of the working tree against HEAD, or against merge-base(base, HEAD)."""
    try:
        top = _git(["rev-parse", "--show-toplevel"], project_root).strip()
    except ReviewError as exc:
        raise ReviewError(f"not a git repository: {project_root}") from exc

    if base:
        try:
            ref = _git(["merge-base", base, "HEAD"], top).strip()
        except ReviewError as exc:
            raise ReviewError(f"cannot diff against {base!r}: {exc}") from exc
    else:
        try:
            _git(["rev-parse", "--verify", "--quiet", "HEAD"], top)
            ref = "HEAD"
        except ReviewError:
            ref = EMPTY_TREE

    diff = _git(["diff", *_DIFF_FLAGS, "-M", ref, "--"], top)
    if not include_untracked:
        return diff

    names = [
        n for n in _git(["ls-files", "--others", "--exclude-standard", "-z"], top).split("\0") if n
    ]
    chunks = [diff]
    for name in names[:MAX_UNTRACKED_FILES]:
        if not _is_untracked_sendable(name):
            continue
        full = os.path.join(top, name)
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > MAX_UNTRACKED_BYTES:
                continue
        except OSError:
            continue
        chunks.append(
            _git(["diff", *_DIFF_FLAGS, "--no-index", "--", os.devnull, name], top, ok_codes=(0, 1))
        )
    return "".join(chunks)


_FILE_HEADER = re.compile(r"^diff --git ", re.M)


def _strip_prefix(raw: str) -> str:
    path = raw.split("\t", 1)[0].strip().strip('"')
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _diff_path(chunk: str) -> str | None:
    old = new = None
    for line in chunk.splitlines()[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("+++ "):
            new = _strip_prefix(line[4:])
        elif line.startswith("--- "):
            old = _strip_prefix(line[4:])
        elif line.startswith("rename to "):
            new = line[len("rename to ") :].strip()
    for candidate in (new, old):
        if candidate and candidate not in ("/dev/null", os.devnull):
            return candidate
    header = chunk.split("\n", 1)[0]
    if " b/" in header:
        return header.rsplit(" b/", 1)[1].strip().strip('"')
    return None


def split_diff_by_file(diff: str) -> list[FileDiff]:
    """Split a multi-file unified diff into one chunk per file, preserving order."""
    starts = [m.start() for m in _FILE_HEADER.finditer(diff)]
    files: list[FileDiff] = []
    for i, start in enumerate(starts):
        chunk = diff[start : starts[i + 1] if i + 1 < len(starts) else len(diff)]
        path = _diff_path(chunk)
        if path:
            files.append(FileDiff(path, chunk))
    return files


def select_for_review(
    files: Sequence[FileDiff],
    triage: Sequence[TriageResult],
    *,
    review_all: bool = False,
    budget: int | None = None,
) -> tuple[list[FileDiff], list[str]]:
    """Pick flagged files (highest priority/risk first) that fit the diff budget."""
    limit = review_max_chars() if budget is None else budget
    ranked = sorted(zip(files, triage), key=lambda ft: (-ft[1].priority, -ft[1].risk))
    selected: list[FileDiff] = []
    over_budget: list[str] = []
    used = 0
    for f, t in ranked:
        if not (review_all or t.should_review):
            continue
        if used + len(f.diff) <= limit:
            selected.append(f)
            used += len(f.diff)
        elif not selected:
            selected.append(FileDiff(f.path, f.diff[:limit]))
            used = limit
        else:
            over_budget.append(f.path)
    return selected, over_budget


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _split_hunks(diff: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Split one file's diff into (header, [(new_start, new_len, hunk_text), ...])."""
    matches = list(_HUNK_HEADER.finditer(diff))
    if not matches:
        return diff, []
    hunks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        new_len = int(m.group(2)) if m.group(2) is not None else 1
        hunks.append((int(m.group(1)), new_len, diff[m.start() : end]))
    return diff[: matches[0].start()], hunks


def hunk_context(diff: str, line: int | None, neighbors: int = 1) -> str:
    """The file header plus the hunk containing new-file ``line`` and its neighbors.

    Falls back to the whole diff when ``line`` is unknown or has no hunks. When
    ``line`` lies between hunks, the nearest hunk is used.
    """
    header, hunks = _split_hunks(diff)
    if line is None or not hunks:
        return diff

    def distance(h: tuple[int, int, str]) -> int:
        start, length, _ = h
        end = start + max(length, 1) - 1
        return 0 if start <= line <= end else min(abs(line - start), abs(line - end))

    idx = min(range(len(hunks)), key=lambda i: distance(hunks[i]))
    lo, hi = max(0, idx - neighbors), min(len(hunks), idx + neighbors + 1)
    return header + "".join(h[2] for h in hunks[lo:hi])


def number_diff_lines(diff: str) -> str:
    """Prefix added/context lines with their new-file line number (removed lines get a blank gutter)."""
    out: list[str] = []
    new_line: int | None = None
    for raw in diff.splitlines(keepends=True):
        m = _HUNK_HEADER.match(raw)
        if m:
            new_line = int(m.group(1))
            out.append(raw)
            continue
        if new_line is None or raw.startswith("\\"):
            out.append(raw)
        elif raw.startswith("-"):
            out.append(f"{'':>6} {raw}")
        elif raw.startswith(("+", " ")) or raw in ("\n", ""):
            out.append(f"{new_line:>6} {raw}")
            new_line += 1
        else:
            new_line = None
            out.append(raw)
    return "".join(out)


def build_review_messages(files: Sequence[FileDiff]) -> list[dict[str, str]]:
    body = "\n\n".join(
        f"### File: {f.path}\n```diff\n{number_diff_lines(f.diff)}\n```" for f in files
    )
    return [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": f"Review these changes:\n\n{body}"},
    ]


def parse_findings(text: str) -> list[ReviewFinding] | None:
    """Parse the model's JSON findings; None when the reply has no usable JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    findings: list[ReviewFinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "").strip()
        comment = str(item.get("comment") or "").strip()
        if not file or not comment:
            continue
        severity = str(item.get("severity") or "minor").strip().lower()
        line = item.get("line")
        findings.append(
            ReviewFinding(
                file=file,
                line=line
                if isinstance(line, int) and not isinstance(line, bool) and line > 0
                else None,
                severity=severity if severity in SEVERITIES else "minor",
                comment=comment,
            )
        )
    return findings


def _match_path(file: str, known: Sequence[str]) -> str | None:
    if file in known:
        return file
    cleaned = _strip_prefix(file).removeprefix("./")
    if cleaned in known:
        return cleaned
    matches = [p for p in known if p.endswith("/" + cleaned) or cleaned.endswith("/" + p)]
    return matches[0] if len(matches) == 1 else None


async def gate_findings(
    findings: Sequence[ReviewFinding],
    diffs: dict[str, str],
    *,
    api_key: str | None = None,
    max_concurrency: int = 8,
) -> tuple[list[ReviewFinding], list[ReviewFinding]]:
    """Run each finding through the Tier-3 gate; returns (kept, dropped).

    The gate sees only the hunk around the finding's line, so large files stay
    under Jev's payload budget. A finding on a file that is not in the diff is
    dropped ungated (it cannot be grounded). A critical finding the gate rejects
    is kept with its failing gate attached, since a missed critical bug costs
    more than a noisy comment.
    """
    sem = asyncio.Semaphore(max(1, max_concurrency))
    known = list(diffs)

    async def _one(f: ReviewFinding) -> tuple[ReviewFinding, bool]:
        path = _match_path(f.file, known)
        if path is None:
            return f, False
        comment = f"Line {f.line}: {f.comment}" if f.line else f.comment
        async with sem:
            gate = await jev_gate_comment_async(
                path, hunk_context(diffs[path], f.line), comment, api_key=api_key
            )
        return replace(f, file=path, gate=gate), gate.passed or f.severity == "critical"

    results = await asyncio.gather(*(_one(f) for f in findings))
    return [f for f, ok in results if ok], [f for f, ok in results if not ok]


async def run_review(
    project_root: str,
    complete: Completer,
    *,
    base: str | None = None,
    review_all: bool = False,
    include_untracked: bool = False,
    api_key: str | None = None,
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
    progress: Callable[[str], None] | None = None,
) -> ReviewReport:
    """Run the full pipeline. Raises ReviewError for git or main-model failures."""
    note = progress or (lambda _msg: None)
    diff = await asyncio.to_thread(collect_git_diff, project_root, base, include_untracked)
    report = ReviewReport(base=base or "HEAD", files=split_diff_by_file(diff))
    if not report.files:
        return report

    note(f"Triage: screening {len(report.files)} file(s) with Jev System-One...")
    report.triage = await jev_screen_many_async(
        [(f.path, f.diff) for f in report.files], api_key=api_key
    )
    selected, report.over_budget = select_for_review(
        report.files, report.triage, review_all=review_all
    )
    report.reviewed = [f.path for f in selected]
    if not selected:
        return report

    note(f"Review: sending {len(selected)} file(s) to the main model...")
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(complete, build_review_messages(selected)), timeout=llm_timeout_s
        )
    except asyncio.TimeoutError as exc:
        raise ReviewError(f"main model did not answer within {llm_timeout_s:.0f}s") from exc
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError(f"main model call failed: {type(exc).__name__}: {exc}") from exc

    findings = parse_findings(raw)
    if findings is None:
        report.parse_failed = True
        report.raw_review = raw
        return report
    if not findings:
        return report

    note(f"Gate: checking {len(findings)} comment(s) with Jev System-One...")
    report.findings, report.dropped = await gate_findings(
        findings, {f.path: f.diff for f in report.files}, api_key=api_key
    )
    return report
