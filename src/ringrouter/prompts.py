"""Model instructions, loaded from prompts.toml at the project root (one reviewable file)."""

from __future__ import annotations

import tomllib
from datetime import datetime
from functools import lru_cache
from pathlib import Path

PROMPTS_FILE = Path(__file__).resolve().parents[2] / "prompts.toml"


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(PROMPTS_FILE, "rb") as f:
        return tomllib.load(f)


def prompt(name: str) -> str:
    return _load()[name]["text"].strip()


def now_line(now: datetime | None = None) -> str:
    """The current local date and time, for prompts ("this weekend", "tomorrow", "is it open now")."""
    now = (now or datetime.now()).astimezone()
    return f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({now.tzname()})."
