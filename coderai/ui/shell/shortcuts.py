"""Composer bindings and their user-facing reference share one definition."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    keys: tuple[tuple[str, ...], ...]
    label: str
    description: str


SHORTCUTS = {
    "complete": Shortcut((("tab",),), "Tab", "Complete command or file path"),
    "newline": Shortcut(
        (("c-j",), ("escape", "enter")),
        "Ctrl-J / Alt-Enter",
        "Insert newline; Enter submits or queues",
    ),
    "plan": Shortcut(
        (("s-tab",), ("escape", "tab"), ("escape", "[", "Z")),
        "Shift-Tab",
        "Toggle plan mode (next turn while running)",
    ),
    "shell": Shortcut((("c-x",),), "Ctrl-X", "Toggle agent / shell input"),
    "steer": Shortcut((("c-s",),), "Ctrl-S", "Steer running agent; send normally when idle"),
    "editor": Shortcut((("c-o",),), "Ctrl-O", "Edit the draft in $VISUAL / $EDITOR"),
    "interrupt": Shortcut(
        (("c-c",),),
        "Ctrl-C",
        "Stop active turn and pause queue; otherwise clear draft; cancel dialogs",
    ),
    "output": Shortcut((("c-e",),), "Ctrl-E", "Inspect tool output, preserving draft"),
    "attachments": Shortcut((("c-t",),), "Ctrl-T", "Review/remove attachments, preserving draft"),
    "cancel": Shortcut((("escape",),), "Esc", "Cancel focused interaction"),
    "exit": Shortcut(
        (("c-d",),), "Ctrl-D", "Exit with empty draft; otherwise delete character; cancel dialogs"
    ),
}


def bind_shortcut(bindings, name):
    def decorate(handler):
        for keys in SHORTCUTS[name].keys:
            bindings.add(*keys)(handler)
        return handler

    return decorate


def shortcut_help() -> str:
    return (
        "\n".join(f"{item.label}: {item.description}" for item in SHORTCUTS.values())
        + "\nCtrl-R: search input history\nCtrl-L: redraw screen\nBrowsers: Up/Down select; Tab details; Enter open; Esc return. Output: Ctrl-F search."
    )
