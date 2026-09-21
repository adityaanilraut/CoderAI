from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


def open_external_editor(initial_text: str = "") -> str:
    """Open the system default or user configured $EDITOR to compose a prompt.

    Checks $VISUAL, $EDITOR, with fallbacks to nano, vim, vi, or notepad.
    """
    editor = os.getenv("VISUAL") or os.getenv("EDITOR")
    if not editor:
        if os.name == "nt":
            editor = "notepad"
        else:
            for fallback in ("nano", "vim", "vi", "emacs"):
                if shutil.which(fallback):
                    editor = fallback
                    break
            if not editor:
                editor = "vi"

    with tempfile.NamedTemporaryFile(
        suffix=".coderai.md", mode="w+", encoding="utf-8", delete=False
    ) as tf:
        if initial_text:
            tf.write(initial_text)
        temp_path = tf.name

    try:
        # Run editor in foreground
        ret = subprocess.run(f"{editor} {temp_path}", shell=True)
        if ret.returncode == 0 and os.path.exists(temp_path):
            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()
            return content.strip()
        return ""
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
