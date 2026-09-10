# Ported from coderai/cli/info_cmd.py - kimi structure (cli/info.py).
"""``coderai info`` subcommand (Kimi ``cli/info.py`` parity, stdlib only).

Single source of version truth: ``coderai/_version.py``. Emits either human
lines or ``--json`` for scripting.
"""

from __future__ import annotations

import json
import platform


def collect_info() -> dict[str, object]:
    """Collect version/protocol metadata (never raises)."""
    try:
        from coderai._version import __version__ as version
    except Exception:
        version = "0.0.0"
    try:
        from coderai.core.agentspec import SUPPORTED_AGENT_SPEC_VERSIONS

        agent_specs = [str(v) for v in SUPPORTED_AGENT_SPEC_VERSIONS]
    except Exception:
        agent_specs = []
    try:
        from coderai.core.wire.protocol import WIRE_PROTOCOL_VERSION

        wire = str(WIRE_PROTOCOL_VERSION)
    except Exception:
        wire = "unknown"
    return {
        "coderai_version": version,
        "agent_spec_versions": agent_specs,
        "wire_protocol_version": wire,
        "python_version": platform.python_version(),
    }


def run_info(argv: list[str]) -> int:
    """Run ``coderai info [--json]``. Returns a process exit code."""
    as_json = False
    for tok in argv:
        if tok == "--json":
            as_json = True
        elif tok in ("-h", "--help"):
            print("Usage: coderai info [--json]\n\nShow version and protocol information.")
            return 0
        else:
            print(f"Unknown option for 'info': {tok}")
            return 2
    info = collect_info()
    if as_json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    specs = ", ".join(str(v) for v in info["agent_spec_versions"]) or "(none)"
    print(f"coderai version: {info['coderai_version']}")
    print(f"agent spec versions: {specs}")
    print(f"wire protocol: {info['wire_protocol_version']}")
    print(f"python version: {info['python_version']}")
    return 0
