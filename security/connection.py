"""How this team's processes connect to Temporal.

Each team owns its connection code. In a real company these folders would be separate repos.

Settings come from Temporal's environment configuration: the profile named after
this team's Namespace in config/profiles/<PROFILE>.toml (PROFILE defaults to
local). On the local profile, TEMPORAL_PORT moves the dev server off 7233.
TEMPORAL_* environment variables override the file, as Temporal's docs describe.
"""

from __future__ import annotations

import os
from pathlib import Path

from temporalio.client import Client
from temporalio.envconfig import ClientConfig, ClientConnectConfig
from temporalio.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parent.parent


def connect_config(profile: str) -> ClientConnectConfig:
    name = os.environ.get("PROFILE", "local")
    profile_file = REPO_ROOT / "config" / "profiles" / f"{name}.toml"
    config = ClientConfig.load_client_connect_config(profile=profile, config_file=str(profile_file))
    if name == "local" and (port := os.environ.get("TEMPORAL_PORT")):
        config["target_host"] = f"localhost:{port}"
    return config


async def connect(profile: str, runtime: Runtime | None = None) -> Client:
    return await Client.connect(**connect_config(profile), runtime=runtime)
