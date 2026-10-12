"""A Workflow that calls Platform's registration Service however it likes, for the tests.

It picks any registration Endpoint and sets any Nexus headers, including a forged
`nexus-link`, which replaces the link the server would send. Its own module, so the
Workflow sandbox imports only this.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from security.publish import refusal


@workflow.defn
class ForgingPublisher:
    """A caller that picks any registration Endpoint and sets whatever headers it likes."""

    @workflow.run
    async def run(self, endpoint: str, request: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        registry = workflow.create_nexus_client(service="PlatformRegistry", endpoint=endpoint)
        try:
            return await registry.execute_operation(
                "register_tool", request, headers=headers, schedule_to_close_timeout=timedelta(seconds=30)
            )
        except Exception as err:
            raise ApplicationError(refusal(err), non_retryable=True) from err
