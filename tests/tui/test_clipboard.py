"""Coverage for coderAI/tui/clipboard.py."""

import os

from coderAI.tui import clipboard


def test_copy_text_prefers_native(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: True)
    writes = []
    result = clipboard.copy_text("hello", write_osc52=writes.append, fallback_file=False)
    assert result.ok
    assert result.method == "native"
    assert result.chars == 5
    assert writes == []  # OSC-52 skipped when native works


def test_copy_text_osc52_via_writer(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: False)
    messages = []
    writes = []
    result = clipboard.copy_text(
        "hello",
        write_osc52=writes.append,
        notify_fn=lambda *a: messages.append(a),
        fallback_file=False,
    )
    assert result.method == "osc52"
    assert writes and "\033]52;c;" in writes[0]
    assert messages and "OSC-52" in messages[0][-1]


def test_copy_text_osc52_truncates(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: False)
    big = "a" * 200_000
    messages = []
    result = clipboard.copy_text(
        big,
        write_osc52=lambda _s: None,
        notify_fn=lambda *a: messages.append(a[-1] if a else ""),
        fallback_file=False,
    )
    assert result.truncated
    assert messages and "truncated" in messages[0]


def test_copy_text_does_not_persist_after_native_copy(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: True)
    monkeypatch.setattr(
        clipboard,
        "_write_fallback_file",
        lambda text: (_ for _ in ()).throw(AssertionError("must not persist successful copies")),
    )

    result = clipboard.copy_text("payload", fallback_file=True)

    assert result.method == "native"
    assert result.path is None


def test_copy_text_fallback_file(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: False)
    notes = []

    def unavailable(_sequence):
        raise OSError("terminal unavailable")

    result = clipboard.copy_text(
        "payload",
        write_osc52=unavailable,
        notify_fn=lambda level, msg: notes.append((level, msg)),
        fallback_file=True,
    )
    assert result.method == "file"
    assert result.path is not None
    try:
        assert result.path.read_text(encoding="utf-8") == "payload"
        if os.name != "nt":
            assert result.path.stat().st_mode & 0o777 == 0o600
    finally:
        result.path.unlink(missing_ok=True)


def test_copy_osc52_compat_helper(monkeypatch, capsys):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: False)
    messages = []
    clipboard.copy_to_clipboard_osc52("hello", lambda m: messages.append(m))
    out = capsys.readouterr().out
    assert "\033]52;c;" in out
    assert messages and "OSC-52" in messages[0]


def test_copy_osc52_without_notify(monkeypatch, capsys):
    monkeypatch.setattr(clipboard, "_copy_native", lambda text: False)
    clipboard.copy_to_clipboard_osc52("x")
    assert "\033]52;c;" in capsys.readouterr().out


def test_copy_fallback_file_writes_and_notifies():
    notes = []
    path = clipboard.copy_fallback_file("payload", lambda level, msg: notes.append((level, msg)))
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == "payload"
        assert notes and notes[0][0] == "info"
    finally:
        path.unlink(missing_ok=True)


def test_copy_fallback_file_without_notify():
    path = clipboard.copy_fallback_file("payload2")
    assert path is not None
    path.unlink(missing_ok=True)


def test_copy_fallback_file_oserror_swallowed(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(clipboard.tempfile, "mkstemp", boom)
    assert clipboard.copy_fallback_file("x", lambda *a: None) is None


def test_copy_native_darwin(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return None

    monkeypatch.setattr(clipboard.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        clipboard.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None
    )
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert clipboard._copy_native("hi") is True
    assert calls == [["pbcopy"]]


def test_copy_native_missing_tools(monkeypatch):
    monkeypatch.setattr(clipboard.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard._copy_native("hi") is False


def test_emit_notify_level_and_message_styles():
    dual = []
    single = []
    clipboard._emit_notify(lambda level, msg: dual.append((level, msg)), "info", "ok")
    clipboard._emit_notify(lambda msg: single.append(msg), "info", "ok")
    assert dual == [("info", "ok")]
    assert single == ["ok"]
