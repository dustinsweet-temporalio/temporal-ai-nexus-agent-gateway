"""A minimal caller Workflow for tests: calls one Nexus Operation by name.

It dispatches the way the gateway does, with string names, a plain dict input,
and principal headers, and imports no team code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow


@dataclass
class NexusCall:
    endpoint: str
    service: str
    operation: str
    arguments: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@workflow.defn
class NexusCallerWorkflow:
    @workflow.run
    async def run(self, call: NexusCall) -> Any:
        client = workflow.create_nexus_client(service=call.service, endpoint=call.endpoint)
        return await client.execute_operation(
            call.operation,
            call.arguments,
            headers=call.headers,
            schedule_to_close_timeout=timedelta(minutes=2),
        )
