"""Coverage for coderAI/tui/clipboard.py."""

from pathlib import Path

from coderAI.tui import clipboard


def test_copy_osc52_small_text_notifies(capsys):
    messages = []
    clipboard.copy_to_clipboard_osc52("hello", lambda m: messages.append(m))
    out = capsys.readouterr().out
    assert "\033]52;c;" in out  # OSC-52 sequence emitted
    assert messages == ["Copied 5 chars via OSC-52"]


def test_copy_osc52_large_text_truncates(capsys):
    # > 102400 base64 chars requires > ~76800 raw bytes; use 200k.
    big = "a" * 200_000
    messages = []
    clipboard.copy_to_clipboard_osc52(big, lambda m: messages.append(m))
    capsys.readouterr()
    assert messages and "truncated" in messages[0]


def test_copy_osc52_without_notify(capsys):
    clipboard.copy_to_clipboard_osc52("x")  # no notify_fn branch
    assert "\033]52;c;" in capsys.readouterr().out


def test_copy_fallback_file_writes_and_notifies():
    notes = []
    clipboard.copy_fallback_file("payload", lambda level, msg: notes.append((level, msg)))
    import tempfile

    path = Path(tempfile.gettempdir()) / "coderAI-copy.txt"
    assert path.read_text(encoding="utf-8") == "payload"
    assert notes and notes[0][0] == "info"


def test_copy_fallback_file_without_notify():
    clipboard.copy_fallback_file("payload2")  # no notify_fn branch, no raise


def test_copy_fallback_file_oserror_swallowed(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    # Should not raise despite the write failing.
    clipboard.copy_fallback_file("x", lambda *a: None)
