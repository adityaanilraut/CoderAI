"""Tests for macOS Desktop Automation tools."""

import json
import subprocess
from unittest.mock import patch, MagicMock
import pytest
from pydantic import ValidationError

from coderAI.tools.desktop import (
    RunAppleScriptTool,
    GetAccessibilityTreeTool,
    ClickUIElementTool,
    TypeKeystrokesTool,
    ClickUIElementParams,
    GetAccessibilityTreeParams,
    TypeKeystrokesParams,
    _validate_app_name,
    _validate_path_segment,
)


@pytest.fixture(autouse=True)
def darwin_platform(request):
    """Simulate macOS so tests pass on Linux CI runners."""
    if request.node.name == "test_platform_guard_linux":
        yield
        return
    with patch("coderAI.tools.desktop.sys") as mock_sys:
        mock_sys.platform = "darwin"
        yield


@pytest.fixture
def run_applescript_tool():
    return RunAppleScriptTool()


@pytest.fixture
def get_accessibility_tree_tool():
    return GetAccessibilityTreeTool()


@pytest.fixture
def click_ui_element_tool():
    return ClickUIElementTool()


@pytest.fixture
def type_keystrokes_tool():
    return TypeKeystrokesTool()


# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------


@patch("coderAI.tools.desktop.sys")
@pytest.mark.asyncio
async def test_platform_guard_linux(mock_sys, run_applescript_tool):
    mock_sys.platform = "linux"
    result = await run_applescript_tool.execute(script="return 1")
    assert result["success"] is False
    assert "only available on macOS" in result["error"]


@patch("coderAI.tools.desktop.sys")
@pytest.mark.asyncio
async def test_platform_guard_darwin(mock_sys, run_applescript_tool):
    """On darwin _check_platform returns None so we proceed to subprocess."""
    mock_sys.platform = "darwin"
    with patch("coderAI.tools.desktop.subprocess.run") as mock_run:
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = b"ok"
        mock_process.stderr = b""
        mock_run.return_value = mock_process
        result = await run_applescript_tool.execute(script="return 1")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_validate_app_name_valid():
    assert _validate_app_name("Calculator") is None
    assert _validate_app_name("Google Chrome") is None
    assert _validate_app_name("my-app.v2") is None


def test_validate_app_name_injection():
    result = _validate_app_name('Calculator"]; ObjC.import("Cocoa"); //')
    assert result is not None
    assert result["success"] is False
    assert "Invalid application name" in result["error"]


def test_validate_app_name_semicolon():
    result = _validate_app_name("Calc; rm -rf /")
    assert result is not None
    assert result["success"] is False


def test_validate_app_name_newline():
    result = _validate_app_name("Calculator\nend tell")
    assert result is not None
    assert result["success"] is False
    assert "Invalid application name" in result["error"]


def test_validate_app_name_tab():
    result = _validate_app_name("Calculator\tTab")
    assert result is not None
    assert result["success"] is False


def test_validate_path_segment():
    # Valid segments
    assert _validate_path_segment("window 1") is None
    assert _validate_path_segment("button 9") is None
    assert _validate_path_segment('button "OK"') is None
    assert _validate_path_segment('menu bar item "File"') is None
    assert _validate_path_segment("pop up button 1") is None

    # Invalid segments / complex command injection attempts
    result = _validate_path_segment('window 1 tell application "Finder"')
    assert result is not None
    assert result["success"] is False

    result2 = _validate_path_segment("window 1; do shell script")
    assert result2 is not None
    assert result2["success"] is False

    result3 = _validate_path_segment("window 1 of button 1")
    assert result3 is not None
    assert result3["success"] is False


def test_params_validation_max_depth():
    with pytest.raises(ValidationError):
        GetAccessibilityTreeParams(max_depth=0)
    with pytest.raises(ValidationError):
        GetAccessibilityTreeParams(max_depth=11)
    params = GetAccessibilityTreeParams(max_depth=5)
    assert params.max_depth == 5


def test_params_validation_keystrokes():
    with pytest.raises(ValidationError):
        TypeKeystrokesParams(key_code=-1)
    with pytest.raises(ValidationError):
        TypeKeystrokesParams(key_code=256)
    with pytest.raises(ValidationError):
        TypeKeystrokesParams(modifiers=["invalid modifier"])
    params = TypeKeystrokesParams(key_code=36, modifiers=["command down"])
    assert params.key_code == 36
    assert params.modifiers == ["command down"]


# ---------------------------------------------------------------------------
# RunAppleScriptTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_run_applescript_success(mock_run, run_applescript_tool):
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b"success output"
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await run_applescript_tool.execute(script="return 1", is_jxa=False)

    assert result["success"] is True
    assert result["stdout"] == "success output"
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["osascript"]
    assert kwargs["input"] == b"return 1"


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_run_applescript_jxa(mock_run, run_applescript_tool):
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b"jxa output"
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await run_applescript_tool.execute(script="return 1;", is_jxa=True)

    assert result["success"] is True
    assert result["stdout"] == "jxa output"
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["osascript", "-l", "JavaScript"]


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_run_applescript_failure(mock_run, run_applescript_tool):
    """Non-zero return code should report success=False with stderr."""
    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.stdout = b""
    mock_process.stderr = b"execution error: some problem"
    mock_run.return_value = mock_process

    result = await run_applescript_tool.execute(script="invalid script")

    assert result["success"] is False
    assert result["returncode"] == 1
    assert "some problem" in result["stderr"]


@pytest.mark.asyncio
@patch(
    "coderAI.tools.desktop.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=30),
)
async def test_run_applescript_timeout(mock_run, run_applescript_tool):
    result = await run_applescript_tool.execute(script="delay 999")
    assert result["success"] is False
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# GetAccessibilityTreeTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_get_accessibility_tree_success(mock_run, get_accessibility_tree_tool):
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = json.dumps({"app": "Calculator", "tree": {"role": "AXWindow"}}).encode()
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await get_accessibility_tree_tool.execute(app_name="Calculator")

    assert result["success"] is True
    assert result["data"]["app"] == "Calculator"
    mock_run.assert_called_once()


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_get_accessibility_tree_frontmost(mock_run, get_accessibility_tree_tool):
    """When app_name is None, should target frontmost app."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = json.dumps({"app": "Finder", "tree": {}}).encode()
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await get_accessibility_tree_tool.execute(app_name=None)

    assert result["success"] is True
    # Verify the JXA script uses the frontmost-app selector (not a named process)
    args, kwargs = mock_run.call_args
    jxa_input = kwargs["input"].decode("utf-8")
    assert "frontmost" in jxa_input


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_get_accessibility_tree_error_in_json(mock_run, get_accessibility_tree_tool):
    """JXA returns a JSON object with an 'error' key."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = json.dumps({"error": "Can't get process."}).encode()
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await get_accessibility_tree_tool.execute(app_name="NonExistentApp")

    assert result["success"] is False
    assert "Can't get process" in result["error"]


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_get_accessibility_tree_non_json_output(mock_run, get_accessibility_tree_tool):
    """osascript returns something that isn't valid JSON."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b"not json at all"
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await get_accessibility_tree_tool.execute(app_name="Calculator")

    assert result["success"] is False
    assert "Failed to parse" in result["error"]


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_get_accessibility_tree_resilient_json(mock_run, get_accessibility_tree_tool):
    """JXA output contains warning headers before the actual JSON object."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b'Warning: Cocoa event loop delay\n{"app": "Calculator", "tree": {}}'
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await get_accessibility_tree_tool.execute(app_name="Calculator")

    assert result["success"] is True
    assert result["data"]["app"] == "Calculator"


@pytest.mark.asyncio
@patch(
    "coderAI.tools.desktop.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=15),
)
async def test_get_accessibility_tree_timeout(mock_run, get_accessibility_tree_tool):
    result = await get_accessibility_tree_tool.execute(app_name="Calculator")
    assert result["success"] is False
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_get_accessibility_tree_injection_blocked(get_accessibility_tree_tool):
    """Injecting JXA via app_name should be blocked by validation."""
    result = await get_accessibility_tree_tool.execute(
        app_name='Calculator"]; ObjC.import("Cocoa"); //'
    )
    assert result["success"] is False
    assert "Invalid application name" in result["error"]


# ---------------------------------------------------------------------------
# ClickUIElementTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_click_ui_element_success(mock_run, click_ui_element_tool):
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b""
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await click_ui_element_tool.execute(
        app_name="Calculator", element_path=["window 1", 'button "9"']
    )

    assert result["success"] is True
    assert "Successfully clicked" in result["message"]
    mock_run.assert_called_once()
    # Verify stdin is used (not -e)
    args, kwargs = mock_run.call_args
    assert args[0] == ["osascript"]
    assert "input" in kwargs


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_click_ui_element_failure(mock_run, click_ui_element_tool):
    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.stdout = b""
    mock_process.stderr = b"Can't get window 1"
    mock_run.return_value = mock_process

    result = await click_ui_element_tool.execute(
        app_name="Calculator", element_path=["window 1", 'button "X"']
    )

    assert result["success"] is False
    assert "Can't get window" in result["error"]


@pytest.mark.asyncio
async def test_click_ui_element_injection_app_name(click_ui_element_tool):
    result = await click_ui_element_tool.execute(
        app_name='Calc"; do shell script "rm -rf /"',
        element_path=["window 1"],
    )
    assert result["success"] is False
    assert "Invalid application name" in result["error"]


@pytest.mark.asyncio
async def test_click_ui_element_injection_element_path(click_ui_element_tool):
    result = await click_ui_element_tool.execute(
        app_name="Calculator",
        element_path=["window 1; do shell script"],
    )
    assert result["success"] is False
    assert "Invalid element path segment" in result["error"]


@pytest.mark.asyncio
async def test_click_ui_element_injection_newline_path(click_ui_element_tool):
    result = await click_ui_element_tool.execute(
        app_name="Calculator",
        element_path=["window 1\ndo shell script"],
    )
    assert result["success"] is False
    assert "Invalid element path segment" in result["error"]


@pytest.mark.asyncio
async def test_click_ui_element_embedded_of_rejected(click_ui_element_tool):
    result = await click_ui_element_tool.execute(
        app_name="Calculator",
        element_path=["window 1 of button 1"],
    )
    assert result["success"] is False
    assert "must not contain ' of '" in result["error"]


def test_click_ui_element_empty_path_rejected():
    with pytest.raises(ValidationError):
        ClickUIElementParams(app_name="Calculator", element_path=[])


@pytest.mark.asyncio
@patch(
    "coderAI.tools.desktop.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=10),
)
async def test_click_ui_element_timeout(mock_run, click_ui_element_tool):
    result = await click_ui_element_tool.execute(app_name="Calculator", element_path=["window 1"])
    assert result["success"] is False
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# TypeKeystrokesTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_type_keystrokes_text(mock_run, type_keystrokes_tool):
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b""
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await type_keystrokes_tool.execute(text="hello", modifiers=["command down"])

    assert result["success"] is True
    assert "Successfully executed" in result["message"]
    mock_run.assert_called_once()
    # Verify stdin is used
    args, kwargs = mock_run.call_args
    assert args[0] == ["osascript"]
    assert "input" in kwargs


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_type_keystrokes_key_code(mock_run, type_keystrokes_tool):
    """key_code=36 is Return."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b""
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await type_keystrokes_tool.execute(key_code=36)

    assert result["success"] is True
    # Verify the script contains 'key code 36'
    args, kwargs = mock_run.call_args
    script_text = kwargs["input"].decode("utf-8")
    assert "key code 36" in script_text


@pytest.mark.asyncio
async def test_type_keystrokes_validation_error(type_keystrokes_tool):
    result = await type_keystrokes_tool.execute()
    assert result["success"] is False
    assert "either 'text' or 'key_code'" in result["error"]

    result2 = await type_keystrokes_tool.execute(text="hello", key_code=36)
    assert result2["success"] is False
    assert "Cannot provide both" in result2["error"]


@pytest.mark.asyncio
async def test_type_keystrokes_control_char_rejected(type_keystrokes_tool):
    result = await type_keystrokes_tool.execute(text='hello"\nend tell')
    assert result["success"] is False
    assert "control characters" in result["error"]


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_type_keystrokes_invalid_modifiers_filtered(mock_run, type_keystrokes_tool):
    """Invalid modifiers should be silently filtered out."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = b""
    mock_process.stderr = b""
    mock_run.return_value = mock_process

    result = await type_keystrokes_tool.execute(
        text="a", modifiers=["command down", "invalid modifier"]
    )

    assert result["success"] is True
    # Only valid modifier should appear in the script
    args, kwargs = mock_run.call_args
    script_text = kwargs["input"].decode("utf-8")
    assert "command down" in script_text
    assert "invalid modifier" not in script_text


@pytest.mark.asyncio
@patch("coderAI.tools.desktop.subprocess.run")
async def test_type_keystrokes_failure(mock_run, type_keystrokes_tool):
    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.stdout = b""
    mock_process.stderr = b"System Events got an error"
    mock_run.return_value = mock_process

    result = await type_keystrokes_tool.execute(text="hello")
    assert result["success"] is False
    assert "System Events" in result["error"]


@pytest.mark.asyncio
@patch(
    "coderAI.tools.desktop.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=10),
)
async def test_type_keystrokes_timeout(mock_run, type_keystrokes_tool):
    result = await type_keystrokes_tool.execute(text="hello")
    assert result["success"] is False
    assert "timed out" in result["error"]
