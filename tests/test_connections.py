"""Each team keeps its own copy of the connection helper. These tests keep the copies in step."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWNERSHIP_NOTE = (
    "Each team owns its connection code. In a real company these folders would be separate repos."
)
PROFILES = ["platform", "release-eng", "security"]
SCENARIOS = [
    {},
    {"PROFILE": "local"},
    {"PROFILE": "local", "TEMPORAL_PORT": "7299"},
]


def connection_modules() -> list[ModuleType]:
    paths = sorted(ROOT.glob("*/connection.py"))
    return [importlib.import_module(f"{p.parent.name}.connection") for p in paths]


def test_every_team_has_a_connection_helper_with_the_ownership_note() -> None:
    modules = connection_modules()
    assert {m.__name__.split(".")[0] for m in modules} >= {"release_eng"}
    for module in modules:
        assert OWNERSHIP_NOTE in (module.__doc__ or ""), f"{module.__name__} is missing the ownership note"


@pytest.mark.parametrize("env", SCENARIOS, ids=["defaults", "local", "local-custom-port"])
def test_all_copies_resolve_the_same_settings(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PROFILE", "TEMPORAL_PORT", "TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    resolved = {
        module.__name__: {profile: dict(module.connect_config(profile)) for profile in PROFILES}
        for module in connection_modules()
    }
    first = next(iter(resolved.values()))
    for name, settings in resolved.items():
        assert settings == first, f"{name} resolves different settings"

    expected_port = env.get("TEMPORAL_PORT", "7233")
    for profile in PROFILES:
        assert first[profile]["target_host"] == f"localhost:{expected_port}"
        assert first[profile]["namespace"] == profile
