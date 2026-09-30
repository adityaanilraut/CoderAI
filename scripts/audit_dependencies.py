"""Block on advisories in the actual installed graph, or incomplete audits.

Only the first-party project/SDK distributions are excluded (their source is gated).
No separately resolved requirements file is used. Exceptions must name a package,
advisory, reason and expiry, and are checked against all advisory aliases.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs/security/dependency-audit-policy.json"
FIRST_PARTY = {"coderai-agent", "coderai-sdk"}


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_inventory(report: dict[str, Any], installed: list[dict[str, str]]) -> list[str]:
    expected = {
        (normalized_name(d["name"]), d["version"])
        for d in installed
        if normalized_name(d["name"]) not in FIRST_PARTY
    }
    audited = {(normalized_name(d["name"]), d.get("version")) for d in report["dependencies"]}
    return [
        f"Unaudited installed dependency: {name}=={version}"
        for name, version in sorted(expected - audited)
    ]


def blocking_findings(report: dict[str, Any], policy: dict[str, Any], today: date) -> list[str]:
    exceptions = policy["exceptions"]
    if not isinstance(exceptions, list):
        raise ValueError("exceptions must be a list")
    for exception in exceptions:
        if not all(
            isinstance(exception.get(key), str) and exception[key].strip()
            for key in ("package", "id", "reason", "expires")
        ):
            raise ValueError("An audit exception requires package, id, reason and expires")
        expiry = date.fromisoformat(exception["expires"])
        if expiry <= today or (expiry - today).days > 30:
            raise ValueError("Audit exceptions must expire within 30 days and be unexpired")
    dependencies = report["dependencies"]
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("Audit returned no installed dependencies")
    problems = []
    for dependency in dependencies:
        name = dependency["name"]
        if dependency.get("skip_reason"):
            # Third-party packages must never be silently omitted.
            problems.append(f"Incomplete audit: {name}: {dependency['skip_reason']}")
        for vuln in dependency.get("vulns", []):
            ids = {vuln["id"], *vuln.get("aliases", [])}
            if not any(e["package"] == name and e["id"] in ids for e in exceptions):
                problems.append(f"{name} {dependency.get('version')}: {vuln['id']}")
    return problems


def audit_environment(output: Path, policy_path: Path = POLICY) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coderai-audit-") as temporary:
        raw = Path(temporary) / "audit.json"
        installed = sorted(
            (
                {"name": d.metadata["Name"], "version": d.version}
                for d in importlib.metadata.distributions()
            ),
            key=lambda d: d["name"].lower(),
        )
        # First-party source is verified by this repository's source/artifact gates.
        # Everything else, including audit tooling, is pinned from this interpreter.
        requirements = Path(temporary) / "installed.txt"
        requirements.write_text(
            "".join(
                f"{d['name']}=={d['version']}\n"
                for d in installed
                if normalized_name(d["name"]) not in FIRST_PARTY
            )
        )
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "-r",
            str(requirements),
            "--progress-spinner",
            "off",
            "--format",
            "json",
            "--desc",
            "off",
            "--output",
            str(raw),
        ]
        proc = subprocess.run(command, capture_output=True, text=True)
        try:
            report = json.loads(raw.read_text())
            problems = blocking_findings(report, json.loads(policy_path.read_text()), date.today())
            problems += validate_inventory(report, installed)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report = {}
            problems = [f"Audit/policy failure: {exc}"]
        # A 1 is pip-audit's vulnerability exit; the policy decides exceptions.
        # Collection/service errors must never become a successful gate.
        if proc.returncode not in (0, 1) or not report:
            problems.append(f"pip-audit failed ({proc.returncode}): {proc.stderr.strip()}")
        elif proc.returncode == 1 and not any(d.get("vulns") for d in report["dependencies"]):
            problems.append(f"Incomplete pip-audit: {proc.stderr.strip()}")
        inventory = json.dumps(installed, sort_keys=True)
        evidence = {
            "command": command,
            "exit_code": proc.returncode,
            "installed": installed,
            "excluded_first_party": sorted(FIRST_PARTY),
            "inventory_sha256": hashlib.sha256(inventory.encode()).hexdigest(),
            "audit": report,
            "blocking": problems,
            "passed": not problems,
        }
        output.write_text(json.dumps(evidence, indent=2) + "\n")
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"Dependency audit {'FAILED' if problems else 'passed'}; evidence: {output}")
    return int(bool(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".verification/dependency-audit.json")
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args()
    return audit_environment(args.output, args.policy)


if __name__ == "__main__":
    raise SystemExit(main())
