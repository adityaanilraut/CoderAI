"""Per-project workspace trust.

A cloned repository must not get code execution or API keys merely because
``coderai`` starts inside it. Project-scope command vectors (project
``.env``, project ``mcpServers`` commands, hooks, statusline providers,
``config.toml`` commands, and a project ``baseURL`` paired with a
user-scope ``apiKey``) are ignored until the user trusts the project.

Trust decisions persist in the user directory
(``~/.coderai/trusted_projects.json`` or ``CODERAI_SHARE_DIR``), never in
the repository itself. Non-interactive runs (``--print``, ACP) default to
untrusted; opt in with ``--trust-project`` or ``CODERAI_TRUST_PROJECT=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

TRUST_ENV_VAR = "CODERAI_TRUST_PROJECT"
TRUST_STORE_NAME = "trusted_projects.json"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def get_trust_store_path() -> Path:
    """Location of the trust store (user scope, never the repo)."""
    from coderai.share import get_share_dir

    return get_share_dir() / TRUST_STORE_NAME


def normalize_project_root(project_root: str | None) -> str:
    """Canonical absolute path used as the trust-store key."""
    try:
        return str(Path(project_root or ".").expanduser().resolve())
    except OSError:
        return str(project_root or ".")


def _read_store() -> dict[str, Any]:
    try:
        path = get_trust_store_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_store(data: dict[str, Any]) -> None:
    from coderai.utils.io import atomic_json_write

    path = get_trust_store_path()
    # ponytail: trust store read per settings resolution; cache if measurable.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    except OSError:
        pass
    atomic_json_write(data, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_override() -> bool | None:
    raw = os.environ.get(TRUST_ENV_VAR)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def is_project_trusted(project_root: str | None, *, explicit: bool | None = None) -> bool:
    """Return True when project-scope commands may run for ``project_root``.

    Precedence: explicit flag (``--trust-project``) > ``CODERAI_TRUST_PROJECT``
    env var > persisted trust store. Defaults to untrusted.
    """
    if explicit is not None:
        return bool(explicit)
    override = _env_override()
    if override is not None:
        return override
    return bool(_read_store().get(normalize_project_root(project_root), False))


def trust_project(project_root: str | None) -> None:
    """Persist trust for ``project_root`` in the user directory."""
    data = _read_store()
    data[normalize_project_root(project_root)] = True
    _write_store(data)


def untrust_project(project_root: str | None) -> bool:
    """Remove persisted trust. Returns True when an entry was removed."""
    key = normalize_project_root(project_root)
    data = _read_store()
    if key not in data:
        return False
    del data[key]
    _write_store(data)
    return True


def ensure_project_trust(
    project_root: str | None,
    *,
    non_interactive: bool = False,
    explicit: bool | None = None,
    ask: Any = None,
) -> bool:
    """Return True when the project may use project-scope commands.

    Already-trusted projects (store, env, or flag) return True without
    prompting. Interactive callers are asked once via ``ask`` (defaults to
    an ``input()`` y/N prompt); acceptance is persisted. Non-interactive
    runs never prompt and default to untrusted.
    """
    if is_project_trusted(project_root, explicit=explicit):
        return True
    if non_interactive:
        return False
    prompt = ask
    if prompt is None:

        def _ask(question: str) -> str:
            try:
                return input(question)
            except (EOFError, KeyboardInterrupt):
                return ""

        prompt = _ask
    try:
        answer = prompt(
            f"Trust project '{normalize_project_root(project_root)}'? "
            "Untrusted projects cannot run project .env, MCP server commands, "
            "hooks, or statusline commands. [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        return False
    if isinstance(answer, str) and answer.strip().lower() in ("y", "yes"):
        trust_project(project_root)
        return True
    return False
