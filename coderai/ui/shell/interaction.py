"""Async focused terminal interactions. The composer is suspended before entry.

All text is literal. Search examines full records while rendering a window;
selection uses stable identifiers rather than a stale filtered row index.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import os
from typing import Any

from coderai.ui.shell.submission import SelectorOutcome


@dataclass
class BrowserRow:
    id: str
    label: str
    detail: str = ""
    disabled: bool = False


@dataclass
class BrowserPosition:
    query: str = ""
    selected_id: str | None = None


def filter_rows(rows: list[BrowserRow], query: str) -> list[BrowserRow]:
    needle = query.casefold().strip()
    return [row for row in rows if needle in f"{row.id} {row.label} {row.detail}".casefold()]


def fit_label(text: str, width: int) -> str:
    """Keep one visible row per record, including wide Unicode labels."""
    from prompt_toolkit.utils import get_cwidth

    text = " ".join(text.splitlines())
    if sum(get_cwidth(char) for char in text) <= width:
        return text
    result, cells = "", 0
    for char in text:
        size = get_cwidth(char)
        if cells + size > max(0, width - 3):
            break
        result += char
        cells += size
    return result + "." * min(3, max(0, width))


async def choose(
    prompt,
    title: str,
    rows: list[BrowserRow] | Callable[[], list[BrowserRow]],
    *,
    accessible: bool = False,
    position: BrowserPosition | None = None,
) -> SelectorOutcome:
    source = rows if callable(rows) else lambda: rows
    position = position or BrowserPosition()
    if accessible:
        query, offset = position.query, 0
        while True:
            available = filter_rows(source(), query)
            print(f"\n{title} ({len(available)} matches)")
            window = available[offset : offset + 20]
            printed_details: set[str] = set()
            for i, row in enumerate(window):
                print(f"{i + 1}. {row.label}{' (unavailable)' if row.disabled else ''}")
                if row.detail and row.detail not in printed_details:
                    print(row.detail)
                    printed_details.add(row.detail)
            try:
                value = (
                    await prompt.prompt_async(
                        "Number, /details N, /search text, /next, /prev, or cancel: "
                    )
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return SelectorOutcome()
            if value.lower() in ("", "q", "cancel", "esc"):
                return SelectorOutcome()
            if value.startswith("/search "):
                query = value[8:]
                position.query = query
                offset = 0
            elif value == "/next":
                offset = offset + 20 if offset + 20 < len(available) else 0
            elif value == "/prev":
                offset = max(0, offset - 20)
            elif value.startswith("/details "):
                index = value[9:].strip()
                if index.isdigit() and 1 <= int(index) <= len(window):
                    print(window[int(index) - 1].detail or "No additional details.")
            elif value.isdigit() and 1 <= int(value) <= len(window):
                if window[int(value) - 1].disabled:
                    print("This action is unavailable. " + window[int(value) - 1].detail)
                    continue
                position.selected_id = window[int(value) - 1].id
                return SelectorOutcome(position.selected_id)
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition, has_focus
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, ConditionalContainer
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import DynamicStyle, Style
    from prompt_toolkit.widgets import TextArea
    from coderai.ui.theme import get_prompt_session_styles

    selected = 0
    detail_mode = False
    search = TextArea(text=position.query, height=1, prompt="Search: ", multiline=False)
    details = TextArea(read_only=True, scrollbar=True, wrap_lines=True)
    app: Any = None

    def matches():
        return filter_rows(source(), search.text)

    def content():
        nonlocal selected
        found = matches()
        if position.selected_id:
            selected = next(
                (i for i, row in enumerate(found) if row.id == position.selected_id), selected
            )
        selected = min(selected, max(0, len(found) - 1))
        height = max(2, (app.output.get_size().rows if app else 24) - 7)
        start = max(0, selected - height // 2)
        current = found[selected] if found else None
        if current and details.text != current.detail:
            details.text = current.detail
        if not current:
            details.text = "No results. Change the search or press Esc."
        lines = [("", f"{title} | {len(found)} matches\n")]
        if not found:
            lines.append(("", "No results.\n"))
        for i in range(start, min(len(found), start + height)):
            columns = app.output.get_size().columns if app else 80
            pane_width = (columns - 1) // 2 if columns >= 80 else columns
            lines.append(
                (
                    "class:completion-menu.completion.current" if i == selected else "",
                    f"{'>' if i == selected else ' '} {fit_label(found[i].label + (' (unavailable)' if found[i].disabled else ''), max(1, pane_width - 2))}\n",
                )
            )
        return lines

    wide = Condition(lambda: app.output.get_size().columns >= 80)
    list_pane = Window(FormattedTextControl(content), wrap_lines=True)
    body = HSplit(
        [
            ConditionalContainer(VSplit([list_pane, Window(width=1, char="|"), details]), wide),
            ConditionalContainer(list_pane, ~wide & Condition(lambda: not detail_mode)),
            ConditionalContainer(details, ~wide & Condition(lambda: detail_mode)),
        ]
    )
    kb = KeyBindings()

    @kb.add("up", filter=has_focus(search))
    @kb.add("c-p", filter=has_focus(search))
    def up(event):
        nonlocal selected
        selected = max(0, selected - 1)
        found = matches()
        position.selected_id = found[selected].id if found else None

    @kb.add("down", filter=has_focus(search))
    @kb.add("c-n", filter=has_focus(search))
    def down(event):
        nonlocal selected
        selected = min(max(0, len(matches()) - 1), selected + 1)
        found = matches()
        position.selected_id = found[selected].id if found else None

    @kb.add("enter")
    def submit(event):
        found = matches()
        if found:
            if found[min(selected, len(found) - 1)].disabled:
                return
            position.query = search.text
            position.selected_id = found[min(selected, len(found) - 1)].id
            event.app.exit(result=SelectorOutcome(found[min(selected, len(found) - 1)].id))

    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def cancel(event):
        position.query = search.text
        event.app.exit(result=SelectorOutcome())

    @kb.add("tab")
    @kb.add("c-e")
    def toggle(event):
        nonlocal detail_mode
        detail_mode = not detail_mode
        event.app.layout.focus(details if detail_mode else search)

    def search_changed(_):
        nonlocal selected
        selected = 0
        position.selected_id = None
        if app:
            app.invalidate()

    search.buffer.on_text_changed += search_changed
    app = Application(
        layout=Layout(
            HSplit(
                [
                    search,
                    body,
                    Window(
                        FormattedTextControl(
                            "Up/Down select | Enter open | Tab details | Esc cancel"
                        ),
                        height=1,
                    ),
                ]
            ),
            focused_element=search,
        ),
        key_bindings=kb,
        full_screen=True,
        enable_page_navigation_bindings=True,
        refresh_interval=1,
        input=prompt.session.app.input,
        output=prompt.session.app.output,
        style=DynamicStyle(
            lambda: Style.from_dict({} if "NO_COLOR" in os.environ else get_prompt_session_styles())
        ),
        color_depth=prompt.session.app.color_depth,
    )
    return await app.run_async()


async def inspect_output(prompt, title: str, text: str, *, accessible: bool = False) -> None:
    if accessible:
        print(f"\n{title}\n{text}")
        return
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.widgets import TextArea, Label, SearchToolbar
    from prompt_toolkit.search import start_search
    from prompt_toolkit.styles import DynamicStyle, Style
    from coderai.ui.theme import get_prompt_session_styles

    search = SearchToolbar()
    area = TextArea(text=text, read_only=True, scrollbar=True, wrap_lines=True, search_field=search)
    kb = KeyBindings()

    @kb.add("c-f")
    def find(event):
        start_search(area.control)

    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def close(event):
        event.app.exit()

    app: Application[None] = Application(
        layout=Layout(
            HSplit([Label(f"{title} | Ctrl-F search | Esc return"), area, search]),
            focused_element=area,
        ),
        key_bindings=kb,
        full_screen=True,
        enable_page_navigation_bindings=True,
        input=prompt.session.app.input,
        output=prompt.session.app.output,
        style=DynamicStyle(
            lambda: Style.from_dict({} if "NO_COLOR" in os.environ else get_prompt_session_styles())
        ),
    )
    await app.run_async()
