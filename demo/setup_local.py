"""Local setup: the Nexus Endpoints on the dev server.

The dev server creates the start state's Namespaces (platform, release-eng) from its
--namespace flags. This adds each team's tool Endpoint, targeting its Namespace and
Task Queue, and its registration Endpoint, platform-registry-<team>, targeting
Platform's registration Service (Namespace platform, Task Queue gateway). Security
isn't part of the start state: its Endpoints are created when it joins
(demo/supervisor.py). Running this again changes nothing.

The last column is each Endpoint's Temporal Cloud allowlist: the caller Namespaces
Temporal Cloud lets through. The local dev server has no allowlists: any Namespace
may call any Endpoint.

Run: uv run python -m demo.setup_local
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from temporalio.api.nexus.v1 import EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import CreateNexusEndpointRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from gateway.connection import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
# Endpoint -> (the team it belongs to, target Namespace, target Task Queue, Temporal Cloud allowlist)
ENDPOINTS = {
    "release-eng-tools": ("release-eng", "release-eng", "release-eng", ["platform"]),
    "platform-registry-release-eng": ("release-eng", "platform", "gateway", ["release-eng"]),
    "security-tools": ("security", "security", "security", []),  # no callers until Platform is added
    "platform-registry-security": ("security", "platform", "gateway", ["security"]),
}
START_TEAMS = ("release-eng",)  # Security joins later, during the demo


async def ensure_endpoints(client: Client, teams: tuple[str, ...] = START_TEAMS) -> list[str]:
    """Create these teams' Endpoints, if they aren't there yet. Returns the ones it created."""
    created = []
    for name, (team, namespace, task_queue, _cloud_allowlist) in ENDPOINTS.items():
        if team not in teams:
            continue
        target = EndpointTarget(worker=EndpointTarget.Worker(namespace=namespace, task_queue=task_queue))
        try:
            await client.operator_service.create_nexus_endpoint(
                CreateNexusEndpointRequest(spec=EndpointSpec(name=name, target=target))
            )
            created.append(name)
        except RPCError as err:
            if err.status != RPCStatusCode.ALREADY_EXISTS:
                raise
    return created


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    created = await ensure_endpoints(await connect("platform"))
    print(f"Endpoints created: {', '.join(created) or 'none (already there)'}")


if __name__ == "__main__":
    asyncio.run(main())
