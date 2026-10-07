"""Shared, typed prompt preparation for the terminal shell."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SelectorOutcome:
    value: int | str | None = None

    @property
    def cancelled(self) -> bool:
        return self.value is None


@dataclass
class QuestionOutcome:
    status: Literal["submitted", "cancelled", "incomplete"]
    answers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str | None:
        return (
            "\n".join(f"{q}: {a}" for q, a in self.answers) if self.status == "submitted" else None
        )


@dataclass
class PromptSubmission:
    display_text: str
    resolved_text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    action: Literal["send", "queue", "steer", "shell", "command"] = "send"
    files: list[str] = field(default_factory=list)


def split_path_argument(text: str) -> tuple[str, str]:
    """Read one quoted path without interpreting Windows backslashes as escapes."""
    text = text.strip()
    if not text:
        return "", ""
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        if end == -1:
            raise ValueError("Close the quote around the file path.")
        return text[1:end], text[end + 1 :].strip()
    path, _, rest = text.partition(" ")
    return path, rest.strip()


def prepare_submission(
    text: str,
    project_root: str,
    *,
    skills: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> PromptSubmission:
    from coderai.ui.shell.placeholders import get_placeholder_manager
    from coderai.ui.shell.prompt import expand_file_mentions
    from coderai.ui.shell.submission_history import SubmissionHistory

    text, recalled = SubmissionHistory(project_root).resolve(text)

    manager = get_placeholder_manager()
    display = manager.maybe_placeholderize_pasted_text(text)
    resolved = manager.resolve_command(display)
    from dataclasses import asdict, is_dataclass

    images = list(attachments or []) + recalled
    for part in resolved.content:
        if getattr(part, "TYPE", "") == "image_url" or getattr(part, "type", "") == "image_url":
            data = asdict(part) if is_dataclass(part) else part.model_dump()
            data["type"] = data.pop("TYPE", "image_url")
            images.append(data)
    import re

    image_tokens = re.findall(r"\[image:[^\]]+\]", resolved.resolved_text)
    if image_tokens and not images:
        raise ValueError("This history image is unavailable. Attach it again with /image.")
    if re.search(r"\[Pasted text #\d+", resolved.resolved_text):
        raise ValueError("This paste is unavailable. Paste the original text again.")
    # Cached paste contents can contain language decorators. Only the visible
    # draft declares file mentions; the resolved paste stays literal content.
    expanded_display, files = expand_file_mentions(display, project_root)
    expanded = resolved.resolved_text + expanded_display[len(display) :]
    for token in image_tokens:
        expanded = expanded.replace(token, "[attached image]")
    return PromptSubmission(display, expanded, images, list(skills or []), files=files)
