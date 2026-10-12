"""Demo identity: who is who, from config/principals.yaml. DEMO AUTHENTICATION ONLY.

The people, teams, and roles go into the gateway's registry, so Workflows decide
who may approve what from recorded state. The bearer tokens stay here: the front
door maps a token to a person and never sends the token anywhere. In production,
identities come from your identity provider.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

import yaml

from gateway.models import Directory, Person

PRINCIPALS_FILE = Path(__file__).resolve().parent.parent / "config" / "principals.yaml"


def _read(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def load_directory(path: Path = PRINCIPALS_FILE, without: Collection[str] = ()) -> Directory:
    """People and teams, leaving out teams that haven't joined the demo yet (Security, at first)."""
    raw = _read(path)
    people = [
        Person(
            email=p["email"], name=p["name"], title=p["title"], team=p["team"], roles=list(p.get("roles", []))
        )
        for p in raw["people"]
        if p["team"] not in without
    ]
    return Directory(people=people, teams={k: v for k, v in raw["teams"].items() if k not in without})


def demo_tokens(path: Path = PRINCIPALS_FILE) -> dict[str, str]:
    """Bearer token to email, for the front door's demo authentication."""
    return {p["demo_token"]: p["email"] for p in _read(path)["people"]}
