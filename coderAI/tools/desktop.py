"""macOS Desktop Automation tools (AppleScript and Accessibility)."""

import asyncio
import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.system.locks import resource_manager

# Tool names used for platform gating in Agent._create_tool_registry.
DESKTOP_TOOL_NAMES: tuple[str, ...] = (
    "run_applescript",
    "get_accessibility_tree",
    "click_ui_element",
    "type_keystrokes",
)

# Validation pattern: app names must be alphanumeric, spaces, hyphens, dots.
_SAFE_APP_NAME_RE = re.compile(r"^[\w \-\.]+$")

# Element path segments: restrict to standard AppleScript specifier class names (alphabetic words)
# followed by an optional index or double-quoted name.
_SAFE_PATH_SEGMENT_RE = re.compile(r'^[a-zA-Z]+(?: [a-zA-Z]+)*(?: \d+| "[^"\\]*")?$')

# Reject embedded AppleScript ``of`` chains inside a single segment.
_PATH_EMBEDDED_OF_RE = re.compile(r"\s+of\s+", re.IGNORECASE)

MAX_A11Y_JSON_BYTES = 500_000
_MAX_A11Y_NODES = 500
_MAX_A11Y_CHILDREN_PER_NODE = 50


def is_macos() -> bool:
    return sys.platform == "darwin"


def _check_platform() -> Dict[str, Any] | None:
    """Return an error dict if not running on macOS, else None."""
    if not is_macos():
        return {"success": False, "error": "This tool is only available on macOS."}
    return None


def _validate_app_name(app_name: str) -> Dict[str, Any] | None:
    """Return an error dict if app_name contains unsafe characters, else None."""
    if not _SAFE_APP_NAME_RE.match(app_name):
        return {"success": False, "error": f"Invalid application name: {app_name!r}"}
    return None


def _validate_path_segment(segment: str) -> Dict[str, Any] | None:
    """Return an error dict if an element_path segment is unsafe."""
    if _PATH_EMBEDDED_OF_RE.search(segment):
        return {
            "success": False,
            "error": (f"Invalid element path segment (must not contain ' of '): {segment!r}"),
        }
    if not _SAFE_PATH_SEGMENT_RE.match(segment):
        return {"success": False, "error": f"Invalid element path segment: {segment!r}"}
    return None


def _validate_keystroke_text(text: str) -> Dict[str, Any] | None:
    """Reject control characters that can break AppleScript string literals."""
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return {
            "success": False,
            "error": "Text cannot contain control characters (newlines, tabs, etc.).",
        }
    return None


async def _run_osascript(
    cmd: List[str],
    script: str,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run osascript in a worker thread so async callers do not block the loop."""
    return await asyncio.to_thread(
        subprocess.run,
        cmd,
        input=script.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )


def _cap_accessibility_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate oversized accessibility JSON before returning to the model."""
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= MAX_A11Y_JSON_BYTES:
        return data
    return {
        "app": data.get("app"),
        "bundleId": data.get("bundleId"),
        "tree": {
            "truncated": True,
            "note": (
                f"UI tree exceeded {MAX_A11Y_JSON_BYTES} bytes; "
                "narrow with a lower max_depth or a specific app_name."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Run AppleScript
# ---------------------------------------------------------------------------


class RunAppleScriptParams(BaseModel):
    script: str = Field(..., description="The AppleScript code to execute.")
    is_jxa: bool = Field(
        False,
        description="Set to true if the script is JavaScript for Automation (JXA).",
    )


class RunAppleScriptTool(Tool):
    """Execute arbitrary AppleScript or JXA on the macOS host."""

    name = "run_applescript"
    description = (
        "Execute AppleScript or JavaScript for Automation (JXA) on the host macOS. "
        "Useful for opening applications, navigating browsers (e.g. Chrome/Safari) to search or open URLs, "
        "getting system state, or complex UI scripting."
    )
    category = "desktop"
    parameters_model = RunAppleScriptParams
    requires_confirmation = True
    timeout = 35.0

    async def execute(self, script: str, is_jxa: bool = False) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_platform():
            return err
        async with resource_manager.desktop_lock():
            try:
                cmd = ["osascript"]
                if is_jxa:
                    cmd.extend(["-l", "JavaScript"])

                process = await _run_osascript(cmd, script, timeout=30)

                stdout_str = process.stdout.decode("utf-8", errors="replace").strip()
                stderr_str = process.stderr.decode("utf-8", errors="replace").strip()

                return {
                    "success": process.returncode == 0,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "returncode": process.returncode,
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "AppleScript execution timed out after 30 seconds."}
            except Exception as e:
                return {"success": False, "error": f"Failed to execute AppleScript: {e}"}


# ---------------------------------------------------------------------------
# Get Accessibility Tree
# ---------------------------------------------------------------------------


class GetAccessibilityTreeParams(BaseModel):
    app_name: Optional[str] = Field(
        None,
        description=(
            "Name of the application (e.g. 'Calculator'). "
            "If omitted, targets the frontmost application."
        ),
    )
    max_depth: int = Field(
        5,
        ge=1,
        le=10,
        description="Maximum depth to traverse the UI hierarchy (default: 5, min: 1, max: 10).",
    )


class GetAccessibilityTreeTool(Tool):
    """Retrieve the UI element tree of a macOS application using Accessibility APIs via JXA."""

    name = "get_accessibility_tree"
    description = (
        "Retrieve a JSON representation of the Accessibility UI tree for a running macOS application. "
        "Useful for finding the names, roles, and descriptions of UI elements to interact with."
    )
    category = "desktop"
    parameters_model = GetAccessibilityTreeParams
    is_read_only = True
    timeout = 20.0

    async def execute(self, app_name: Optional[str] = None, max_depth: int = 5) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_platform():
            return err
        if app_name is not None:
            if err := _validate_app_name(app_name):
                return err

        if app_name:
            app_selector = f'se.processes["{app_name}"]'
        else:
            app_selector = (
                "(function() { "
                "var a = se.processes.whose({frontmost: true}); "
                "if (a.length > 0) return a[0]; "
                'throw new Error("No frontmost application found."); '
                "})()"
            )

        jxa_script = f"""
        var maxNodes = {_MAX_A11Y_NODES};
        var maxChildren = {_MAX_A11Y_CHILDREN_PER_NODE};
        var nodeCount = 0;

        function getUIElements(element, depth, maxDepth) {{
            if (depth > maxDepth) return {{"truncated": true, "reason": "max_depth"}};
            nodeCount++;
            if (nodeCount > maxNodes) return {{"truncated": true, "reason": "max_nodes"}};

            let info = {{}};
            try {{
                let props = element.properties();
                info.role = props.role;
                info.roleDescription = props.roleDescription;
                info.name = props.name;
                info.title = props.title;
                info.value = props.value;
                info.enabled = props.enabled;
                info.focused = props.focused;
                info.position = props.position;
                info.size = props.size;
                info.description = props.description;
            }} catch(e) {{
                const props = ['role', 'roleDescription', 'name', 'title', 'value', 'enabled', 'focused', 'position', 'size', 'description'];
                props.forEach(p => {{ try {{ info[p] = element[p](); }} catch(e) {{}} }});
            }}

            let cleanInfo = {{}};
            for (let k in info) {{
                if (info[k] !== null && info[k] !== undefined && info[k] !== "") {{
                    cleanInfo[k] = info[k];
                }}
            }}

            try {{
                let children = element.uiElements();
                if (children && children.length > 0) {{
                    cleanInfo.children = [];
                    let limit = Math.min(children.length, maxChildren);
                    if (children.length > maxChildren) {{
                        cleanInfo.childrenTruncated = children.length - maxChildren;
                    }}
                    for (let i = 0; i < limit; i++) {{
                        if (nodeCount >= maxNodes) {{
                            cleanInfo.children.push({{"truncated": true, "reason": "max_nodes"}});
                            break;
                        }}
                        cleanInfo.children.push(getUIElements(children[i], depth + 1, maxDepth));
                    }}
                }}
            }} catch(e) {{}}

            return cleanInfo;
        }}

        try {{
            var se = Application("System Events");
            var targetApp = {app_selector};

            var result = {{
                app: targetApp.name(),
                tree: getUIElements(targetApp, 0, {max_depth})
            }};
            try {{ result.bundleId = targetApp.bundleIdentifier(); }} catch(e) {{}}
            JSON.stringify(result, null, 2);
        }} catch (e) {{
            JSON.stringify({{error: e.message || e.toString()}});
        }}
        """
        async with resource_manager.desktop_lock():
            try:
                process = await _run_osascript(
                    ["osascript", "-l", "JavaScript"],
                    jxa_script,
                    timeout=15,
                )
                stdout_str = process.stdout.decode("utf-8", errors="replace").strip()

                if process.returncode != 0:
                    return {
                        "success": False,
                        "error": process.stderr.decode("utf-8", errors="replace").strip()
                        or "Unknown osascript error",
                    }

                try:
                    # Find the first '{' and last '}' to strip warning logs or headers
                    start_idx = stdout_str.find("{")
                    end_idx = stdout_str.rfind("}")
                    if start_idx != -1 and end_idx != -1:
                        json_str = stdout_str[start_idx : end_idx + 1]
                    else:
                        json_str = stdout_str

                    data = json.loads(json_str)
                    if "error" in data:
                        return {"success": False, "error": data["error"]}
                    data = _cap_accessibility_payload(data)
                    return {"success": True, "data": data}
                except json.JSONDecodeError:
                    return {
                        "success": False,
                        "error": f"Failed to parse JXA output as JSON. Raw output: {stdout_str}",
                    }

            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Accessibility tree traversal timed out."}
            except Exception as e:
                return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Click UI Element
# ---------------------------------------------------------------------------


class ClickUIElementParams(BaseModel):
    app_name: str = Field(..., description="Name of the application (e.g. 'Calculator').")
    element_path: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "AppleScript specifiers from outermost to target element, "
            "e.g. ['window 1', 'button \"OK\"']. Each segment must not contain ' of '."
        ),
    )


class ClickUIElementTool(Tool):
    """Click a specific UI element in a macOS application using AppleScript System Events."""

    name = "click_ui_element"
    description = (
        "Click a UI element in a macOS application using its AppleScript hierarchy path. "
        'Example element_path for \'button "OK" of window 1\': ["window 1", "button \\"OK\\""]'
    )
    category = "desktop"
    parameters_model = ClickUIElementParams
    requires_confirmation = True
    timeout = 15.0

    async def execute(self, app_name: str, element_path: List[str]) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_platform():
            return err
        if err := _validate_app_name(app_name):
            return err

        for segment in element_path:
            if err := _validate_path_segment(segment):
                return err

        path_str = " of ".join(reversed(element_path))

        script = f"""
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                click {path_str}
            end tell
        end tell
        """

        async with resource_manager.desktop_lock():
            try:
                process = await _run_osascript(["osascript"], script, timeout=10)
                if process.returncode == 0:
                    return {
                        "success": True,
                        "message": f"Successfully clicked {path_str} in {app_name}",
                    }
                return {
                    "success": False,
                    "error": process.stderr.decode("utf-8", errors="replace").strip(),
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Click operation timed out after 10 seconds."}
            except Exception as e:
                return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Type Keystrokes
# ---------------------------------------------------------------------------


class TypeKeystrokesParams(BaseModel):
    text: Optional[str] = Field(None, description="Text string to type.")
    key_code: Optional[int] = Field(
        None,
        ge=0,
        le=255,
        description="AppleScript key code (e.g., 36 for Return, 48 for Tab). Mutually exclusive with 'text'.",
    )
    modifiers: Optional[
        List[Literal["command down", "option down", "control down", "shift down"]]
    ] = Field(
        None,
        description="Modifiers: 'command down', 'option down', 'control down', 'shift down'.",
    )


class TypeKeystrokesTool(Tool):
    """Simulate keyboard input using macOS System Events."""

    name = "type_keystrokes"
    description = (
        "Simulate typing text or pressing a specific key code on the macOS host. "
        "Either 'text' or 'key_code' must be provided. Use modifiers like ['command down'] for shortcuts."
    )
    category = "desktop"
    parameters_model = TypeKeystrokesParams
    requires_confirmation = True
    timeout = 15.0

    async def execute(  # type: ignore[override]
        self,
        text: Optional[str] = None,
        key_code: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if err := _check_platform():
            return err
        if text is None and key_code is None:
            return {"success": False, "error": "Must provide either 'text' or 'key_code'."}
        if text is not None and key_code is not None:
            return {
                "success": False,
                "error": "Cannot provide both 'text' and 'key_code' simultaneously.",
            }

        if text is not None:
            if err := _validate_keystroke_text(text):
                return err
            escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
            action_str = f'set theText to "{escaped_text}"\nkeystroke theText'
        else:
            action_str = f"key code {key_code}"

        modifier_str = ""
        if modifiers:
            valid_mods = [
                m
                for m in modifiers
                if m in ("command down", "option down", "control down", "shift down")
            ]
            if valid_mods:
                mod_list = ", ".join(valid_mods)
                modifier_str = f" using {{{mod_list}}}"

        script = f"""
tell application "System Events"
    {action_str}{modifier_str}
end tell
"""
        async with resource_manager.desktop_lock():
            try:
                process = await _run_osascript(["osascript"], script, timeout=10)
                if process.returncode == 0:
                    return {"success": True, "message": "Successfully executed keystroke action."}
                return {
                    "success": False,
                    "error": process.stderr.decode("utf-8", errors="replace").strip(),
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Keystroke operation timed out after 10 seconds."}
            except Exception as e:
                return {"success": False, "error": str(e)}
