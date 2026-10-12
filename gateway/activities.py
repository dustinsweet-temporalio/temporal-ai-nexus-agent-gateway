"""Gateway Activities.

`read_registry` runs as a local Activity at the start of every tool call. Its
result (the registry snapshot: tools, policy, blocked releases, people) is recorded
in the session's history, so replaying the session makes the same decision.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity
from temporalio.client import Client

from gateway.models import REGISTRY_ID, RegistrySnapshot
from gateway.registry_workflow import ToolRegistryWorkflow

# Every Query fails fast if no Worker serves the registry, instead of blocking.
QUERY_TIMEOUT = timedelta(seconds=1)


class GatewayActivities:
    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn
    async def read_registry(self) -> RegistrySnapshot:
        registry = self._client.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
        return await registry.query(ToolRegistryWorkflow.snapshot, rpc_timeout=QUERY_TIMEOUT)
