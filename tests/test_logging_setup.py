"""Tests for central logging setup and redaction."""

import logging
import sys

import pytest

from coderAI.system import logging_setup
from coderAI.system.logging_setup import _MANAGED_ATTR, setup_logging
from coderAI.system.redaction import RedactingFilter, redact_text, sanitize_dict


@pytest.fixture
def isolated_root():
    """Snapshot and restore root logger handlers/level around each test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield root
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
            h.close()
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)


@pytest.fixture
def log_paths(tmp_path, monkeypatch):
    """Redirect the log dir to tmp so tests never touch ~/.coderAI."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(logging_setup, "LOG_FILE", log_dir / "coderai.log")
    return log_dir


class TestSetupLogging:
    def test_cli_mode_installs_stderr_handler(self, isolated_root):
        setup_logging(logging.INFO)
        managed = [h for h in isolated_root.handlers if getattr(h, _MANAGED_ATTR, False)]
        assert len(managed) == 1
        assert isinstance(managed[0], logging.StreamHandler)
        assert isolated_root.level == logging.INFO

    def test_tui_mode_uses_file_handler_only(self, isolated_root, log_paths, monkeypatch):
        monkeypatch.delenv("CODERAI_LOG_STDERR", raising=False)
        setup_logging(logging.WARNING, tui_mode=True)
        managed = [h for h in isolated_root.handlers if getattr(h, _MANAGED_ATTR, False)]
        assert len(managed) == 1
        assert isinstance(managed[0], logging.handlers.RotatingFileHandler)
        assert (log_paths / "coderai.log").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_tui_mode_file_permissions(self, isolated_root, log_paths):
        setup_logging(logging.WARNING, tui_mode=True)
        assert (log_paths.stat().st_mode & 0o777) == 0o700
        assert ((log_paths / "coderai.log").stat().st_mode & 0o777) == 0o600

    def test_tui_mode_stderr_escape_hatch(self, isolated_root, log_paths, monkeypatch):
        monkeypatch.setenv("CODERAI_LOG_STDERR", "1")
        setup_logging(logging.WARNING, tui_mode=True)
        managed = [h for h in isolated_root.handlers if getattr(h, _MANAGED_ATTR, False)]
        assert len(managed) == 2

    def test_reconfigure_replaces_only_managed_handlers(self, isolated_root, log_paths):
        foreign = logging.NullHandler()
        isolated_root.addHandler(foreign)
        setup_logging(logging.INFO)
        setup_logging(logging.DEBUG, tui_mode=True)
        assert foreign in isolated_root.handlers
        managed = [h for h in isolated_root.handlers if getattr(h, _MANAGED_ATTR, False)]
        assert len(managed) == 1


class TestRedaction:
    def test_redact_text_scrubs_api_keys(self):
        msg = "request failed with key sk-ant-abc123def456ghi789 retrying"
        out = redact_text(msg)
        assert "sk-ant" not in out
        assert "[REDACTED]" in out
        assert "request failed" in out  # rest of message preserved

    def test_redact_text_scrubs_bearer_tokens(self):
        out = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
        assert "eyJhbGci" not in out

    def test_redact_text_preserves_normal_messages(self):
        msg = "Tool read_file completed in 0.32s for /some/path/file.py"
        assert redact_text(msg) == msg

    def test_sanitize_dict_redacts_sensitive_keys(self):
        out = sanitize_dict({"api_key": "secret", "nested": {"token": "x", "ok": "fine"}})
        assert out["api_key"] == "[REDACTED]"
        assert out["nested"]["token"] == "[REDACTED]"
        assert out["nested"]["ok"] == "fine"

    def test_filter_redacts_log_records(self):
        record = logging.LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            "leaked sk-ant-abc123def456ghi789",
            (),
            None,
        )
        assert RedactingFilter().filter(record) is True
        assert "sk-ant" not in record.getMessage()

    def test_error_policy_backcompat_aliases(self):
        from coderAI.system.error_policy import _sanitize_dict

        assert _sanitize_dict({"api_key": "x"})["api_key"] == "[REDACTED]"
