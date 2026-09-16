"""PyInstaller bundle helpers.

Importing this module requires PyInstaller to be installed
(``pip install pyinstaller``); it is only imported by ``coderai.spec`` at
build time, never at runtime.
"""

from __future__ import annotations

import pathlib

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

#: Every first-party subpackage with dynamically imported modules (tool
#: registry, soul sessions, MCP transports, lazy CLI paths). Kept in sync
#: with the directory listing of ``coderai/`` so new modular packages are
#: bundled without a spec change.
_FIRST_PARTY_PACKAGES = (
    "coderai.acp",
    "coderai.agents",
    "coderai.approval_runtime",
    "coderai.auth",
    "coderai.background",
    "coderai.cli",
    "coderai.goals",
    "coderai.hooks",
    "coderai.mcp",
    "coderai.network",
    "coderai.notifications",
    "coderai.plugin",
    "coderai.prompt",
    "coderai.prompts",
    "coderai.skill",
    "coderai.skills",
    "coderai.soul",
    "coderai.subagents",
    "coderai.telemetry",
    "coderai.terminal",
    "coderai.teams",
    "coderai.tools",
    "coderai.ui",
    "coderai.utils",
    "coderai.wire",
)

#: Third-party packages are intentionally NOT collected wholesale:
#: ``collect_submodules("kosong")``-style collection forces optional
#: contrib backends (torch/transformers audio extras, etc.) into the bundle
#: and balloons a ~100MB binary past 400MB. PyInstaller's static analysis
#: already follows the third-party modules we really import; add individual
#: dynamic-only modules below if a build ever reports a missing import.
_DYNAMIC_THIRD_PARTY: tuple[str, ...] = ()

hiddenimports: list[str] = []
for _package in _FIRST_PARTY_PACKAGES + _DYNAMIC_THIRD_PARTY:
    try:
        hiddenimports.extend(collect_submodules(_package))
    except Exception:
        # Package not installed in the build env (e.g. optional dep);
        # Analysis already covers statically imported modules.
        continue
hiddenimports.extend(
    [
        "setproctitle",
        "coderai._build_info",
        "coderai._version",
    ]
)

datas = (
    collect_data_files(
        "coderai",
        includes=[
            "agents/**/*.yaml",
            "agents/**/*.md",
            "agents/**/*.py",
            "prompts/**/*.md",
            "skills/**",
            "tools/**/*.md",
            # NOTE: the changelog lives at the repo root, so there is no
            # in-package changelog to collect.
        ],
        excludes=[
            "tools/*.md",
        ],
    )
    + collect_data_files(
        "fastmcp",
        includes=["../fastmcp-*.dist-info/*"],
    )
)


#: Heavy packages PyInstaller's static analysis reaches through guarded /
#: optional imports (google-genai notebook helpers, hub extras) but the
#: runtime never imports (verified: zero references in ``coderai/`` and a
#: bare ``import google.genai`` loads none of these). Excluding them keeps a
#: ~440MB / 30s-startup bundle near ~100MB with second-scale startup.
excludes: list[str] = [
    # ML / data stack (optional extras of hub/tokenizer packages).
    "torch",
    "functorch",
    "torchvision",
    "torchaudio",
    "accelerate",
    "peft",
    "bitsandbytes",
    "kernels",
    "transformers",
    "datasets",
    "tokenizers",
    "sentencepiece",
    "huggingface_hub",
    "safetensors",
    "gguf",
    "pandas",
    "matplotlib",
    "matplotlib_inline",
    "scipy",
    "sklearn",
    "nltk",
    "spacy",
    "networkx",
    "pyarrow",
    "bokeh",
    "altair",
    "plotly",
    "sympy",
    # Notebook / game / media extras.
    "IPython",
    "ipykernel",
    "jupyter_client",
    "jupyter_core",
    "ipywidgets",
    "comm",
    "debugpy",
    "nbformat",
    "nbclient",
    "pygame",
    "pygame_ce",
    "pyglet",
    "OpenGL",
    # Dev tools (tests never ship in the binary).
    "pytest",
    "_pytest",
    "black",
]


def _vendor_binaries() -> list[tuple[str, str]]:
    """Locate vendored native binaries (``rg``) for the ``binaries`` slot.

    Binaries must not ride in ``datas``: PyInstaller only preserves the
    executable bit for entries in ``binaries``.
    """
    found: list[tuple[str, str]] = []
    vendor_dir = pathlib.Path(__file__).resolve().parents[1] / "vendor"
    for pattern in ("rg", "rg-*", "rg.exe"):
        for path in sorted(vendor_dir.glob(pattern)):
            if path.is_file():
                found.append((str(path), "."))
    return found


binaries: list[tuple[str, str]] = _vendor_binaries()
