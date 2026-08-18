#!/usr/bin/env python3
"""Find and inspect skills across project and user skill roots."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def parse_frontmatter(content: str) -> dict[str, Any]:
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        return {}
    newline = "\r\n" if content.startswith("---\r\n") else "\n"
    end = content.find(f"{newline}---{newline}", 4)
    if end == -1:
        return {}
    raw = content[4:end]
    if yaml is not None:
        try:
            res = yaml.safe_load(raw)
            if isinstance(res, dict):
                return res
        except Exception:
            pass
    data: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip().strip("'\"")
    return data


def read_skill_info(skill_path: str, display_path: str, folder_name: str) -> dict[str, Any]:
    fallback_name = folder_name.replace("_", "-")
    try:
        content = pathlib.Path(skill_path).read_text(encoding="utf-8", errors="replace")
        data = parse_frontmatter(content)
        name = str(data.get("name") or "").strip() or fallback_name
        description = str(data.get("description") or "").strip()
        return {
            "name": name,
            "folderName": folder_name,
            "path": skill_path,
            "displayPath": display_path,
            "description": description,
        }
    except Exception as e:
        return {
            "name": fallback_name,
            "folderName": folder_name,
            "path": skill_path,
            "displayPath": display_path,
            "description": "",
            "error": str(e),
        }


def collect(root_info: dict[str, Any]) -> list[dict[str, Any]]:
    root_p = pathlib.Path(root_info["root"])
    if not root_p.is_dir():
        return []
    skills: list[dict[str, Any]] = []
    for entry in sorted(root_p.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        folder_name = entry.name
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        skill = read_skill_info(
            str(skill_file), f"{root_info['displayRoot']}/{folder_name}/SKILL.md", folder_name
        )
        digest_target = pathlib.Path(root_info["digestRoot"]) / folder_name / "SKILL.md"
        skill["digestTarget"] = {
            "path": str(digest_target),
            "displayPath": f"{root_info['digestDisplayRoot']}/{folder_name}/SKILL.md",
            "root": root_info["digestDisplayRoot"],
            "exists": digest_target.is_file(),
            "sameAsSource": digest_target.resolve() == skill_file.resolve(),
        }
        skills.append(skill)
    return skills


def expand_input_path(inp: str, project_root: str) -> str | None:
    if inp.startswith("~/") or inp.startswith("~\\"):
        return str(pathlib.Path.home() / inp[2:])
    if inp.startswith("./") or inp.startswith(".\\"):
        return str(pathlib.Path(project_root) / inp[2:])
    if os.path.isabs(inp):
        return inp
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/find_skill.py <skill-name-or-path> [project-root]")
        return 2

    query = sys.argv[1]
    project_root = (
        str(pathlib.Path(sys.argv[2]).resolve()) if len(sys.argv) > 2 else str(pathlib.Path.cwd())
    )

    project_native = str(pathlib.Path(project_root) / ".coderai" / "skills")
    user_native = str(pathlib.Path.home() / ".coderai" / "skills")

    roots = [
        {
            "root": project_native,
            "displayRoot": "./.coderai/skills",
            "scope": "project",
            "kind": "native",
            "digestRoot": project_native,
            "digestDisplayRoot": "./.coderai/skills",
        },
        {
            "root": str(pathlib.Path(project_root) / ".agents" / "skills"),
            "displayRoot": "./.agents/skills",
            "scope": "project",
            "kind": "interoperable",
            "digestRoot": project_native,
            "digestDisplayRoot": "./.coderai/skills",
        },
        {
            "root": user_native,
            "displayRoot": "~/.coderai/skills",
            "scope": "user",
            "kind": "native",
            "digestRoot": user_native,
            "digestDisplayRoot": "~/.coderai/skills",
        },
        {
            "root": str(pathlib.Path.home() / ".agents" / "skills"),
            "displayRoot": "~/.agents/skills",
            "scope": "user",
            "kind": "interoperable",
            "digestRoot": user_native,
            "digestDisplayRoot": "~/.coderai/skills",
        },
    ]

    scanned: list[dict[str, Any]] = []
    for r in roots:
        for sk in collect(r):
            scanned.append({**sk, "root": r["displayRoot"], "scope": r["scope"], "kind": r["kind"]})

    active_by_name: dict[str, dict[str, Any]] = {}
    shadowed: list[dict[str, Any]] = []
    for sk in scanned:
        name = sk["name"]
        if name in active_by_name:
            shadowed.append({**sk, "shadowedBy": active_by_name[name]["displayPath"]})
        else:
            active_by_name[name] = sk

    input_path = expand_input_path(query, project_root)
    matches: list[dict[str, Any]] = []
    for sk in scanned:
        if sk["name"] == query or sk["folderName"] == query:
            matches.append(sk)
            continue
        if input_path:
            norm = str(pathlib.Path(input_path).resolve())
            sk_path = str(pathlib.Path(sk["path"]).resolve())
            sk_dir = str(pathlib.Path(sk["path"]).parent.resolve())
            if sk_path == norm or sk_dir == norm:
                matches.append(sk)

    active_matches = [
        sk for sk in matches if active_by_name.get(sk["name"], {}).get("path") == sk["path"]
    ]
    shadowed_matches = [
        sk for sk in matches if active_by_name.get(sk["name"], {}).get("path") != sk["path"]
    ]

    out = {
        "query": query,
        "projectRoot": project_root,
        "roots": roots,
        "found": len(matches) > 0,
        "activeMatches": active_matches,
        "shadowedMatches": [
            {**sk, "shadowedBy": active_by_name.get(sk["name"], {}).get("displayPath")}
            for sk in shadowed_matches
        ],
        "duplicateNames": shadowed,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
