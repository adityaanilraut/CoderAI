"""Regression tests for multi-agent review fixes (security hardening)."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from coderai.cli.plugin import PluginError, _extract_zip_to_plugin
from coderai.hooks.engine import _decode_hook_output
from coderai.mcp.transport import create_mcp_spawn_spec


def test_notify_refuses_shell_command_line(tmp_path):
    """launch_notify_script must not Popen arbitrary shell strings."""
    from coderai.utils.common import notify

    with patch.object(subprocess, "Popen") as mock_popen:
        notify.launch_notify_script("evil; rm -rf ~", 1000, str(tmp_path))
        mock_popen.assert_not_called()


def test_notify_executes_file_directly(tmp_path):
    """A real executable file is still launched without a shell."""
    from coderai.utils.common import notify

    script = tmp_path / "note.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    script.chmod(0o755)
    with patch.object(subprocess, "Popen") as mock_popen:
        notify.launch_notify_script(str(script), 1000, str(tmp_path))
        assert mock_popen.called
        _, kwargs = mock_popen.call_args
        assert kwargs.get("shell", False) is not True


def test_config_ignores_project_notify():
    """Untrusted project checkout must not supply the notify executable."""
    from coderai.config import resolve_current_settings

    merged = resolve_current_settings(".")
    _ = merged  # smoke: resolver still works without project notify
    import inspect

    src = inspect.getsource(resolve_current_settings)
    assert 'project.get("notify")' not in src


def test_read_blocks_nested_traversal(tmp_path):
    """subdir/../../ escapes must be rejected, not just leading ../."""
    from coderai.tools.file.read import handle_read_tool

    ctx = {"session_id": "s", "project_root": str(tmp_path), "workdir": str(tmp_path)}
    res = handle_read_tool({"file_path": "subdir/../../etc/passwd"}, ctx)
    assert res.ok is False
    assert "project root" in (res.error or "").lower() or "absolute" in (res.error or "").lower()


def test_extract_zip_rejects_symlink(tmp_path):
    """Zip-slip via stored symlink must be refused."""
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    out = tmp_path / "out"
    out.mkdir()
    try:
        _extract_zip_to_plugin(zp, out)
    except PluginError as exc:
        assert "symlink" in str(exc).lower() or "unsafe" in str(exc).lower()
    else:
        raise AssertionError("symlink zip was not rejected")


def test_extract_zip_rejects_traversal(tmp_path):
    """../ members must be refused."""
    zp = tmp_path / "trav.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("../evil.txt", "x")
    out = tmp_path / "out2"
    out.mkdir()
    try:
        _extract_zip_to_plugin(zp, out)
    except PluginError as exc:
        assert "unsafe" in str(exc).lower()
    else:
        raise AssertionError("traversal zip was not rejected")


def test_editor_avoids_shell():
    """$EDITOR with metacharacters must not run through a shell."""
    from coderai.utils.editor import open_external_editor

    with (
        patch("os.getenv", return_value="myeditor; evil"),
        patch("shutil.which", return_value=None),
        patch("tempfile.NamedTemporaryFile") as mock_tf,
        patch("subprocess.run") as mock_run,
    ):
        mock_tf.return_value.__enter__.return_value.name = "/tmp/x.md"
        mock_run.return_value = MagicMock(returncode=1)
        open_external_editor("hi")
        assert mock_run.called
        argv = mock_run.call_args[0][0]
        assert isinstance(argv, list)
        assert mock_run.call_args[1].get("shell") is False


def test_editor_test_contract_shell_false():
    """Existing UI-A10 editor test shape still holds with argv list."""
    from coderai.utils import editor

    with (
        patch("os.getenv", return_value="dummy_editor"),
        patch("subprocess.run") as mock_run,
        patch("tempfile.NamedTemporaryFile") as mock_tf,
    ):
        mock_tf.return_value.__enter__.return_value.name = "/tmp/x.md"
        mock_run.return_value = MagicMock(returncode=0)
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = "hi"
            with patch("os.path.exists", return_value=True), patch("os.remove"):
                editor.open_external_editor("sample prompt")
        argv = mock_run.call_args[0][0]
        assert "dummy_editor" in argv


def test_mcp_spawn_never_uses_shell():
    """Windows and posix specs must both be argv lists with shell False."""
    spec = create_mcp_spawn_spec("myserver", ["--a", "b c"], platform="win32")
    assert spec["shell"] is False
    assert spec["args"] == ["--a", "b c"]
    spec2 = create_mcp_spawn_spec("myserver", ["x"], platform="linux")
    assert spec2["shell"] is False


def test_hooks_fail_closed_on_error_exit():
    """Non-zero hook exits deny instead of failing open."""
    out = _decode_hook_output("", "boom", 1, 1.0, None, None)
    assert out.decision == "deny"


def test_statusline_avoids_shell():
    """Statusline command provider must not use shell=True."""
    from coderai.ui.shell.prompt import StatuslineEngine

    eng = StatuslineEngine(settings={"statusline": {"type": "command", "command": "x"}})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        eng.execute_command_provider("mycmd --flag", "/tmp")
        assert mock_run.called
        argv = mock_run.call_args[0][0]
        assert isinstance(argv, list)
        assert mock_run.call_args[1].get("shell") is False


def test_terminal_scrubs_secrets():
    """Terminal spawn must not copy ambient secrets verbatim."""
    import inspect

    from coderai.terminal import manager

    src = inspect.getsource(manager.TerminalSession.__init__)
    assert "os.environ.copy()" not in src
    assert "scrub_subprocess_env" in src


def test_state_public_surface_only():
    """coderai.state must not advertise private names in __all__."""
    import coderai.state as st

    assert all(not name.startswith("_") for name in st.__all__)
