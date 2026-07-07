"""Coverage for coderAI/cli/utils.py Display helpers.

A non-terminal Console (force_terminal=False, record=True) lets each method run
its full rendering path without touching a real TTY.
"""

from rich.console import Console

from coderAI.cli.utils import Display, display


def _quiet_display() -> Display:
    d = Display()
    d.console = Console(record=True, force_terminal=False, width=80)
    return d


def test_message_helpers_emit_text():
    d = _quiet_display()
    d.print("plain")
    d.print_error("nope")
    d.print_success("yes")
    d.print_warning("careful")
    d.print_info("fyi")
    d.print_header("Section")
    out = d.console.export_text()
    assert "plain" in out
    assert "nope" in out
    assert "yes" in out
    assert "careful" in out
    assert "fyi" in out
    assert "Section" in out


def test_print_table_with_rows():
    d = _quiet_display()
    d.print_table(
        [{"name": "a", "count": 1}, {"name": "b", "count": 2, "extra": "x"}],
        title="Items",
    )
    out = d.console.export_text()
    assert "Items" in out
    assert "Name" in out  # header key title-cased
    assert "Extra" in out  # column union across rows


def test_print_table_empty_is_noop():
    d = _quiet_display()
    d.print_table([])
    assert d.console.export_text() == ""


def test_print_tree_nested():
    d = _quiet_display()
    d.print_tree(
        {
            "root": {"child": "leaf", "list": ["a", {"deep": "value"}]},
            "scalar": 42,
        },
        title="MyTree",
    )
    out = d.console.export_text()
    assert "MyTree" in out
    assert "root" in out
    assert "leaf" in out
    assert "deep" in out


def test_print_tree_list_root():
    d = _quiet_display()
    d.print_tree(["x", ["nested"]], title="L")
    out = d.console.export_text()
    assert "L" in out
    assert "x" in out


def test_module_global_display_exists():
    assert isinstance(display, Display)
