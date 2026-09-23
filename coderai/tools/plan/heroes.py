"""Plan file slug generation using Marvel and DC hero names."""

from __future__ import annotations

import secrets
from pathlib import Path

from coderai.share import get_share_dir

PLANS_DIR = get_share_dir() / "plans"

HERO_NAMES: list[str] = [
    # --- Marvel ---
    "iron-man",
    "spider-man",
    "captain-america",
    "thor",
    "hulk",
    "black-widow",
    "hawkeye",
    "black-panther",
    "doctor-strange",
    "scarlet-witch",
    "vision",
    "falcon",
    "war-machine",
    "ant-man",
    "wasp",
    "captain-marvel",
    "gamora",
    "star-lord",
    "groot",
    "rocket",
    "drax",
    "mantis",
    "nebula",
    "shang-chi",
    "moon-knight",
    "ms-marvel",
    "she-hulk",
    "echo",
    "wolverine",
    "cyclops",
    "storm",
    "jean-grey",
    "rogue",
    "beast",
    "nightcrawler",
    "colossus",
    "shadowcat",
    "jubilee",
    "cable",
    "deadpool",
    "bishop",
    "magik",
    "iceman",
    "archangel",
    "psylocke",
    "dazzler",
    "forge",
    "havok",
    "polaris",
    "emma-frost",
    "namor",
    "silver-surfer",
    "adam-warlock",
    "nova",
    "quasar",
    "sentry",
    "blue-marvel",
    "spectrum",
    "squirrel-girl",
    "cloak",
    "dagger",
    "punisher",
    "elektra",
    "luke-cage",
    "iron-fist",
    "jessica-jones",
    "daredevil",
    "blade",
    "ghost-rider",
    "morbius",
    "venom",
    "carnage",
    "silk",
    "spider-gwen",
    "miles-morales",
    "america-chavez",
    "kate-bishop",
    "yelena-belova",
    "white-tiger",
    "moon-girl",
    "devil-dinosaur",
    "amadeus-cho",
    "riri-williams",
    "kamala-khan",
    "sam-alexander",
    "nova-prime",
    "medusa",
    "black-bolt",
    "crystal",
    "karnak",
    "gorgon",
    "lockjaw",
    "quake",
    "mockingbird",
    "bobbi-morse",
    "maria-hill",
    "nick-fury",
    "phil-coulson",
    "winter-soldier",
    "us-agent",
    "patriot",
    "speed",
    "wiccan",
    "hulkling",
    "stature",
    "yellowjacket",
    "tigra",
    "hellcat",
    "valkyrie",
    "sif",
    "beta-ray-bill",
    "hercules",
    "wonder-man",
    "taskmaster",
    "domino",
    "cannonball",
    "sunspot",
    "wolfsbane",
    "warpath",
    "multiple-man",
    "banshee",
    "siryn",
    "monet",
    "rictor",
    "shatterstar",
    "longshot",
    "daken",
    "x-23",
    "fantomex",
    "batman",
    "superman",
    "wonder-woman",
    "flash",
    "aquaman",
    "green-lantern",
    "martian-manhunter",
    "cyborg",
    "hawkgirl",
    "green-arrow",
    "black-canary",
    "zatanna",
    "constantine",
    "shazam",
    "blue-beetle",
    "booster-gold",
    "firestorm",
    "atom",
    "hawkman",
    "plastic-man",
    "red-tornado",
    "starfire",
    "raven",
    "beast-boy",
    "robin",
    "nightwing",
    "batgirl",
    "batwoman",
    "red-hood",
    "signal",
    "orphan",
    "spoiler",
    "catwoman",
    "huntress",
    "supergirl",
    "superboy",
    "power-girl",
    "steel",
    "stargirl",
    "wildcat",
    "doctor-fate",
    "mister-terrific",
    "hourman",
    "sandman",
    "spectre",
    "phantom-stranger",
    "swamp-thing",
    "animal-man",
    "deadman",
    "vixen",
    "black-lightning",
    "static",
    "icon",
    "rocket-dc",
    "captain-atom",
    "fire",
    "ice",
    "elongated-man",
    "metamorpho",
    "black-hawk",
    "crimson-avenger",
    "doctor-mid-nite",
    "jakeem-thunder",
    "mister-miracle",
    "big-barda",
    "orion",
    "lightray",
    "forager",
    "killer-frost",
    "jessica-cruz",
    "simon-baz",
    "john-stewart",
    "guy-gardner",
    "kyle-rayner",
    "hal-jordan",
    "wally-west",
    "barry-allen",
    "jay-garrick",
    "impulse",
    "kid-flash",
    "donna-troy",
    "tempest",
    "aqualad",
    "miss-martian",
    "terra",
    "jericho",
    "ravager",
    "red-star",
    "pantha",
    "argent",
    "damage",
    "jade",
    "obsidian",
    "cyclone",
    "atom-smasher",
    "maxima",
    "starman",
    "liberty-belle",
]

_slug_cache: dict[str, str] = {}


def seed_slug_cache(session_id: str, slug: str) -> None:
    """Pre-warm the in-process slug cache with a previously persisted slug."""
    _slug_cache[session_id] = slug


def get_or_create_slug(session_id: str) -> str:
    """Get or create a plan file slug for the given session."""
    if session_id in _slug_cache:
        return _slug_cache[session_id]
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    slug = ""
    for _ in range(20):
        words = [secrets.choice(HERO_NAMES) for _ in range(3)]
        slug = "-".join(words)
        if not (PLANS_DIR / f"{slug}.md").exists():
            break
    else:
        # All 20 attempts collided; append session prefix for uniqueness
        slug = f"{slug}-{session_id[:8]}"
    _slug_cache[session_id] = slug
    return slug


def get_plan_file_path(session_id: str, project_root: str | Path | None = None) -> Path:
    """Get the plan file path for the given session.

    When project_root is provided (or when working inside a project), the plan lives
    in ``<project_root>/.coderai/plans/<session_id>.md`` so it is inside the workspace.
    Otherwise falls back to ``~/.coderai/plans/<session_id>.md``.
    """
    if project_root:
        base = Path(project_root) / ".coderai" / "plans"
    else:
        base = PLANS_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base / f"{session_id}.md"


def read_plan_file(session_id: str, project_root: str | Path | None = None) -> str | None:
    """Read the plan file content for the given session, or None if not found."""
    path = get_plan_file_path(session_id, project_root=project_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None
