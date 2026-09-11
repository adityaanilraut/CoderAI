from __future__ import annotations

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    collect_submodules("coderai.tools")
    + collect_submodules("coderai.soul")
    + collect_submodules("coderai.wire")
    + collect_submodules("coderai.acp")
    + collect_submodules("coderai.subagents")
    + collect_submodules("coderai.hooks")
    + collect_submodules("coderai.skill")
    + collect_submodules("coderai.plugin")
    + collect_submodules("coderai.auth")
    + collect_submodules("coderai.notifications")
    + collect_submodules("coderai.telemetry")
    + collect_submodules("coderai.approval_runtime")
    + collect_submodules("coderai.background")
    + collect_submodules("coderai.ui")
    + [
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
            "CHANGELOG.md",
        ],
        excludes=[
            "tools/*.py",
        ],
    )
    + collect_data_files(
        "dateparser",
        includes=["**/*.pkl"],
    )
    + collect_data_files(
        "fastmcp",
        includes=["../fastmcp-*.dist-info/*"],
    )
)
