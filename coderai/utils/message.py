from __future__ import annotations

from kosong.message import Message, TextPart as KosongTextPart

from coderai.wire.types import AudioURLPart, ImageURLPart, TextPart as WireTextPart, VideoURLPart


def message_stringify(message: Message) -> str:
    """Get a string representation of a message."""
    parts: list[str] = []
    for part in message.content:
        if isinstance(part, (KosongTextPart, WireTextPart)):
            parts.append(part.text)
        elif hasattr(part, "text") and isinstance(part.text, str):
            parts.append(part.text)
        elif isinstance(part, ImageURLPart):
            parts.append("[image]")
        elif isinstance(part, AudioURLPart):
            suffix = f":{part.audio_url.id}" if part.audio_url.id else ""
            parts.append(f"[audio{suffix}]")
        elif isinstance(part, VideoURLPart):
            parts.append("[video]")
        else:
            parts.append(f"[{getattr(part, 'type', 'part')}]")
    return "".join(parts)

