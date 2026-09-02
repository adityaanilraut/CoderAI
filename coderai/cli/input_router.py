"""Input router — Phase4 port of Kimi ui/shell/visualize/_input_router.py:31.

Single routing decision point for all user input (btw/queue/send).
ponytail: lean — no slashcmd registry needed, plain string parse.
"""

from __future__ import annotations


class InputAction:
    BTW = "btw"
    QUEUE = "queue"
    SEND = "send"
    IGNORED = "ignored"

    __slots__ = ("kind", "args")

    def __init__(self, kind: str, args: str = "") -> None:
        self.kind = kind
        self.args = args


def classify_input(text: str, *, is_streaming: bool) -> InputAction:
    stripped = text.strip()
    # /btw handling — support "/btw <q>" and "/btw: <q>" style
    if stripped.startswith("/btw"):
        # parse "/btw" + optional args
        rest = stripped[4:].lstrip()
        # allow "/btw:..." or "/btw ..." — strip leading colon
        if rest.startswith(":"):
            rest = rest[1:].lstrip()
        if rest:
            return InputAction(InputAction.BTW, rest)
        return InputAction(InputAction.IGNORED, "Usage: /btw <question>")
    if is_streaming:
        return InputAction(InputAction.QUEUE)
    return InputAction(InputAction.SEND)
