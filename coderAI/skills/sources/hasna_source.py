"""@hasna/skills integration — hosted skill registry via CLI/MCP."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coderAI.skills.skill_manager import Skill
from coderAI.skills.sources.base import SkillSource

logger = logging.getLogger(__name__)

_HASNA_CLI = "skills"


def _find_hasna_cli() -> Optional[str]:
    """Locate the ``skills`` CLI binary."""
    path = shutil.which(_HASNA_CLI)
    if path is None:
        logger.debug("[HasnaSource] skills CLI not found on PATH")
    return path


class HasnaSkillSource(SkillSource):
    """Interacts with the ``@hasna/skills`` CLI to discover hosted skills.

    Uses ``skills search`` for relevance matching and ``skills info`` to
    load full skill details. Falls back gracefully when the CLI or auth
    are not present.
    """

    def __init__(self, project_root: str = ".") -> None:
        self._project_root = str(Path(project_root).resolve())
        self._cli: Optional[str] = _find_hasna_cli()
        self._enabled = self._cli is not None
        self._skill_cache: Dict[str, Skill] = {}
        if not self._enabled:
            logger.info("[HasnaSource] @hasna/skills CLI not available — source disabled")

    @property
    def source_name(self) -> str:
        return "hasna"

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _run_cli(self, *args: str, timeout: float = 15.0) -> str:
        """Execute the ``skills`` CLI and return stdout."""
        if not self._cli:
            raise RuntimeError("skills CLI not available")

        cmd = [self._cli, "--no-color", *args]
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                err = stderr.decode().strip()
                logger.debug("[HasnaSource] CLI error (code %s): %s", proc.returncode, err)
                return ""
            return stdout.decode().strip()
        except FileNotFoundError:
            logger.debug("[HasnaSource] CLI not found: %s", self._cli)
            self._enabled = False
            return ""
        except asyncio.TimeoutError:
            logger.warning("[HasnaSource] CLI timeout for: skills %s", " ".join(args))
            return ""
        except Exception as e:
            logger.warning("[HasnaSource] CLI invocation failed: %s", e)
            return ""

    def _parse_search_results(self, output: str) -> List[Tuple[str, str, str, str]]:
        """Parse ``skills search`` output into ``[(name, description, category, tags), ...]``.

        The output format looks like::

            Found 2 skill(s):

              read-csv [Data & Analysis]
                Price: Free
                Parse CSV files into structured JSON...
        """
        results: List[Tuple[str, str, str, str]] = []
        lines = output.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or ":" in line and line[0].isdigit():
                i += 1
                continue
            # Match: "  skill-name [Category]"
            if "[" in line and "]" in line:
                bracket_start = line.rfind("[")
                bracket_end = line.rfind("]")
                if bracket_start > 0 and bracket_end > bracket_start:
                    name = line[:bracket_start].strip()
                    category = line[bracket_start + 1 : bracket_end].strip()
                    # Skip price line, read description line
                    desc_line = ""
                    j = i + 1
                    while j < len(lines):
                        next_line = lines[j].strip()
                        if (
                            not next_line
                            or next_line.startswith("  ")
                            and not next_line.startswith("    Price")
                        ):
                            j += 1
                            if next_line and not next_line.startswith("  "):
                                desc_line = f"   {next_line} "  # Buffer description
                                break
                            continue
                        if next_line.startswith("    "):
                            desc_line = next_line.strip()
                            break
                        if "[" in next_line and "]" in next_line:
                            break
                        j += 1
                    desc = desc_line if desc_line else name
                    results.append((name, desc, category, ""))
                    i = j
                    continue
            i += 1
        return results

    async def discover(self) -> List[Skill]:
        """List pinned and available hasna skills.

        Falls back to ``skills list`` output.
        """
        if not self._enabled:
            return []

        output = await self._run_cli("list")
        if not output:
            return []

        skills: List[Skill] = []
        for name, desc, category, _ in self._parse_search_results(output):
            if name in self._skill_cache:
                skills.append(self._skill_cache[name])
            else:
                skill = Skill(
                    name=name,
                    description=desc,
                    category=category,
                    source="hasna",
                )
                self._skill_cache[name] = skill
                skills.append(skill)
        return skills

    async def search(self, query: str, top_n: int = 5) -> List[Tuple[Skill, float]]:
        """Use ``skills search`` to find relevant skills."""
        if not self._enabled:
            return []

        output = await self._run_cli("search", query)
        if not output:
            return []

        results: List[Tuple[Skill, float]] = []
        parsed = self._parse_search_results(output)
        # Results are already ranked by relevance; assign descending confidence
        for rank, (name, desc, category, _) in enumerate(parsed[:top_n]):
            confidence = max(0.5, 1.0 - (rank * 0.1))  # 1.0, 0.9, 0.8, ...
            if name in self._skill_cache:
                skill = self._skill_cache[name]
            else:
                skill = Skill(
                    name=name,
                    description=desc,
                    category=category,
                    source="hasna",
                )
                self._skill_cache[name] = skill
            results.append((skill, confidence))
        return results

    async def get_skill(self, name: str) -> Optional[Skill]:
        """Load full details for a hasna skill via ``skills info``."""
        if not self._enabled:
            return None

        if name in self._skill_cache:
            return self._skill_cache[name]

        output = await self._run_cli("info", name)
        if not output:
            return None

        skill = Skill(
            name=name,
            description=output.splitlines()[0] if output else name,
            source="hasna",
        )
        self._skill_cache[name] = skill
        return skill
