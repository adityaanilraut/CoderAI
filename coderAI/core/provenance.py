"""Provenance / tainting for tool results (Phase 3 of the security hardening plan).

Guiding principle #3 of the hardening plan: *data is never instructions*. Anything
that originated outside the user's own typed input — a web page, an MCP server's
response, third-party stdout — is **data** to be analysed, never a directive the
model may act on with system authority. This module supplies the two primitives
that enforce that boundary in the transcript:

* :data:`Provenance` — the taint label a tool declares on its results.
* :func:`wrap_untrusted_output` — renders an ``UNTRUSTED_EXTERNAL`` result inside a
  clearly delimited, non-authoritative ``<untrusted_tool_output>`` block so the
  model (steered by the standing system-prompt instruction) treats it as inert.
* :func:`fence_project_context` — the sibling defusing used for repo-supplied
  rules / skills / ``AGENTS.md`` (Phase 3.3): present them as *user-provided
  project guidance*, not "MUST be followed" system text.

Kept dependency-free (stdlib only) so ``tools/base.py`` and the tool executor can
import it without an import cycle.
"""

from __future__ import annotations

__all__ = [
    "Provenance",
    "wrap_untrusted_output",
    "fence_project_context",
    "UNTRUSTED_OPEN_TAG",
    "UNTRUSTED_CLOSE_TAG",
]


class Provenance:
    """Taint labels for tool results.

    A tool sets ``result_provenance`` (see :class:`coderAI.tools.base.Tool`) to one
    of these. Anything touching outside data (web fetch, MCP output) declares
    ``UNTRUSTED_EXTERNAL``; internal/bookkeeping tools stay ``TRUSTED``.
    """

    TRUSTED = "trusted"
    UNTRUSTED_EXTERNAL = "untrusted_external"


UNTRUSTED_OPEN_TAG = "untrusted_tool_output"
UNTRUSTED_CLOSE_TAG = "</untrusted_tool_output>"


def _sanitize_source(source: str) -> str:
    """Make *source* safe to embed in the ``source="..."`` attribute.

    ``source`` can carry attacker-influenced data (e.g. a fetched URL), so strip
    the characters that would let it break out of the attribute or forge a
    closing tag. Also collapse whitespace and clamp the length.
    """
    if not source:
        return "unknown"
    cleaned = []
    for ch in str(source):
        if ch in '"<>\n\r\t':
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    out = "".join(cleaned).strip()
    # Collapse runs of spaces introduced by the substitutions above.
    out = " ".join(out.split())
    if len(out) > 200:
        out = out[:200] + "…"
    return out or "unknown"


def wrap_untrusted_output(serialized: str, source: str) -> str:
    """Wrap an untrusted tool result in a non-authoritative, delimited block.

    Args:
        serialized: The already-serialized tool result (typically a JSON string).
        source: A short label for where the content came from (tool name, and
            optionally a target such as a URL). Sanitized before embedding.

    The model is told (standing system-prompt instruction) that everything inside
    ``<untrusted_tool_output>`` is data to analyse, never instructions to follow,
    and must not trigger privileged tool calls without explicit user confirmation.
    A defensive close-tag guard neutralizes any literal close tag smuggled inside
    the payload so the fence can't be terminated early.
    """
    src = _sanitize_source(source)
    body = serialized
    if UNTRUSTED_CLOSE_TAG in body:
        # An attacker page could embed the literal close tag to escape the fence.
        # Defang it (HTML-escape the opening angle bracket) so the wrapper's own
        # closing tag stays the only authoritative terminator.
        body = body.replace(UNTRUSTED_CLOSE_TAG, "&lt;/untrusted_tool_output>")
    return f'<{UNTRUSTED_OPEN_TAG} source="{src}">\n{body}\n{UNTRUSTED_CLOSE_TAG}'


def fence_project_context(title: str, body: str, *, origin: str) -> str:
    """Render repo-supplied guidance as fenced, non-authoritative project context.

    Used for project rules, auto-loaded skills, and ``AGENTS.md``/``CLAUDE.md``
    (Phase 3.3). These come from files in the repo, not from the user's typed
    input, so they are framed as *guidance the user has provided for this
    project* — advisory, not a system directive — and never granted "MUST be
    followed" authority.

    Args:
        title: Human-readable heading (e.g. the rule/skill/file name).
        body: The raw content.
        origin: A short tag identifying the source kind ("rule", "skill",
            "AGENTS.md", ...), surfaced in the fence markers.
    """
    tag = _sanitize_source(origin) or "project-context"
    return (
        f"[BEGIN PROJECT {tag.upper()} — user-provided, advisory only]\n"
        f"{title}\n\n"
        f"{body}\n"
        f"[END PROJECT {tag.upper()}]"
    )
