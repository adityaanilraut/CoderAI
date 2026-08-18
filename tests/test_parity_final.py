"""Comprehensive parity test suite verifying all final hardening and parity enhancements."""

import pytest
from coderai.core.common.llm_error import (
    describe_llm_error,
    get_llm_error_details,
    mask_sensitive,
)
from coderai.core.settings import resolve_current_settings, _merge_statusline, _normalize_statusline
from coderai.cli.app import _build_parser, main


def test_mask_sensitive_credentials():
    # 1. sk- style api keys
    raw1 = "Error using key sk-12345678abcdefghij"
    assert mask_sensitive(raw1) == "Error using key ***MASKED***"

    # 2. Authorization Bearer header
    raw2 = "Failed request with Authorization: Bearer secret_jwt_token_12345 to API"
    assert mask_sensitive(raw2) == "Failed request with Authorization: Bearer ***MASKED*** to API"

    # 3. Query param token/key
    raw3 = "https://api.openai.com/v1/chat?api_key=my_super_secret_key&other=1"
    assert mask_sensitive(raw3) == "https://api.openai.com/v1/chat?api_key=***MASKED***&other=1"

    # 4. JSON secret field
    raw4 = '{"api_key": "top_secret_value", "data": 123}'
    assert mask_sensitive(raw4) == '{"api_key": "***MASKED***", "data": 123}'


def test_describe_llm_error_formatting():
    # Test HTTP error with status, code, type, request ID and trace ID
    error_dict = {
        "status": 500,
        "name": "InternalServerError",
        "message": "The server had an error processing your request.",
        "code": "internal_error",
        "type": "server_error",
        "param": "messages",
        "headers": {
            "x-request-id": "req_abc123",
            "x-ds-trace-id": "trace_xyz789",
        },
    }
    desc = describe_llm_error(error_dict)
    assert "HTTP 500: The server had an error processing your request." in desc
    assert "code: internal_error" in desc
    assert "type: server_error" in desc
    assert "param: messages" in desc
    assert "request ID: req_abc123" in desc
    assert "trace ID: trace_xyz789" in desc


def test_describe_llm_error_nested_cause_extraction():
    # Test nested connection error
    inner = Exception("getaddrinfo ENOTFOUND api.openai.com")
    mid = Exception("fetch failed")
    mid.__cause__ = inner
    outer = Exception("Connection error")
    outer.__cause__ = mid

    desc = describe_llm_error(outer)
    assert "Connection error: getaddrinfo ENOTFOUND api.openai.com" in desc


def test_describe_llm_error_masks_in_exceptions():
    err = Exception("Failed connecting with key sk-abcdef1234567890")
    desc = describe_llm_error(err)
    assert "sk-abcdef1234567890" not in desc
    assert "***MASKED***" in desc


def test_cli_parser_flags():
    parser = _build_parser()

    # --prompt / -p
    args1 = parser.parse_args(["-p", "hello world"])
    assert args1.prompt_flag == "hello world"

    # --exec / -x
    args2 = parser.parse_args(["-x", "-p", "run task"])
    assert args2.exec_prompt is True
    assert args2.prompt_flag == "run task"


    # --fork / -f
    args3 = parser.parse_args(["-f", "sess-123"])
    assert args3.fork == "sess-123"

    args3_bare = parser.parse_args(["-f"])
    assert args3_bare.fork is True

    # --last / -l
    args4 = parser.parse_args(["-l"])
    assert args4.last is True

    # --resume / -r
    args5 = parser.parse_args(["-r", "sess-456"])
    assert args5.resume == "sess-456"

    args5_bare = parser.parse_args(["-r"])
    assert args5_bare.resume is True


def test_cli_mutual_exclusion_validations(capsys):
    # 1. Positional + -p
    rc1 = main(["query", "-p", "query2"])
    assert rc1 == 1
    err1 = capsys.readouterr().err
    assert "Cannot use both a positional prompt and the --prompt (-p) flag together" in err1

    # 2. --last + --resume
    rc2 = main(["-l", "-r", "sess-1"])
    assert rc2 == 1
    err2 = capsys.readouterr().err
    assert "Cannot use --last together with --resume" in err2

    # 3. --fork + --resume
    rc3 = main(["-f", "sess-1", "-r", "sess-2"])
    assert rc3 == 1
    err3 = capsys.readouterr().err
    assert "Cannot use --fork together with --resume" in err3

    # 4. --last + --fork
    rc4 = main(["-l", "-f"])
    assert rc4 == 1
    err4 = capsys.readouterr().err
    assert "Cannot use --last together with --fork" in err4

    # 5. bare --resume + prompt
    rc5 = main(["-r", "-p", "test prompt"])
    assert rc5 == 1
    err5 = capsys.readouterr().err
    assert "Cannot use --resume without a session ID together with --prompt" in err5

    # 6. --exec without prompt
    rc6 = main(["-x"])
    assert rc6 == 1
    err6 = capsys.readouterr().err
    assert "--exec / -x requires a non-empty --prompt / -p value" in err6


def test_statusline_settings_resolution():
    user = {
        "statusline": {
            "refreshMs": 5000,
            "providers": [{"type": "command", "id": "git", "command": "git status"}],
        }
    }
    project = {
        "statusline": {
            "separator": " • ",
            "providers": [{"type": "command", "id": "git", "command": "git branch"}],
        }
    }
    merged = _merge_statusline(user, project)
    assert merged["enabled"] is True
    assert merged["refreshMs"] == 5000
    assert merged["separator"] == " • "
    assert len(merged["providers"]) == 1
    assert merged["providers"][0]["command"] == "git branch"

    # Verify resolve_current_settings includes statusline
    settings = resolve_current_settings()
    assert "statusline" in settings
    assert "enabled" in settings["statusline"]
    assert "refreshMs" in settings["statusline"]
    assert "separator" in settings["statusline"]
    assert "providers" in settings["statusline"]
