"""Local JSON storage for user profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path("data/profiles")


def ensure_profiles_dir() -> None:
    """Ensure the base profiles directory exists."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def _owner_dir(owner_sub: str) -> Path:
    return BASE_DIR / owner_sub


def save_profile(owner_sub: str, house_slug: str, profile: Dict[str, Any]) -> Path:
    owner_dir = _owner_dir(owner_sub)
    owner_dir.mkdir(parents=True, exist_ok=True)
    path = owner_dir / f"{house_slug}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)
    return path


def load_profile(owner_sub: str, house_slug: str) -> Dict[str, Any] | None:
    path = _owner_dir(owner_sub) / f"{house_slug}.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_profiles(owner_sub: str) -> List[str]:
    if not BASE_DIR.exists():
        return []
    owner_dir = _owner_dir(owner_sub)
    if not owner_dir.exists():
        return []
    return sorted(p.stem for p in owner_dir.glob("*.json"))