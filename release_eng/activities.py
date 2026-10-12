"""Release Engineering's deploy steps, run as Activities.

Each step talks to the simulated CD backend with an idempotency key the Workflow
built from the Nexus request ID and the step name. Activities can run more than
once (a Worker can die after a step took effect but before Temporal recorded it),
so the key is what keeps a repeated step from deploying twice.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from temporalio import activity
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from release_eng.cd_backend import CDBackend


@dataclass
class StepInput:
    idempotency_key: str
    service: str
    version: str
    environment: str
    step: str
    seconds: float


@dataclass
class RecordDeployment:
    ledger_workflow_id: str
    update_id: str
    deployment_id: str
    environment: str
    version: str
    deployed_by: str


class DeployActivities:
    def __init__(self, backend: CDBackend, client: Client) -> None:
        self._backend = backend
        self._client = client

    @activity.defn
    async def stage_artifact(self, step: StepInput) -> None:
        await self._run(step)

    @activity.defn
    async def roll_out_wave(self, step: StepInput) -> None:
        await self._run(step)

    @activity.defn
    async def verify_health(self, step: StepInput) -> None:
        await self._run(step)

    async def _run(self, step: StepInput) -> None:
        if await asyncio.to_thread(self._backend.is_applied, step.idempotency_key):
            activity.logger.info("Step %s already applied; nothing to do", step.idempotency_key)
            return
        # Stand-in for the time a real CD system takes. Heartbeats let Temporal
        # notice within seconds if this Worker dies mid-step.
        deadline = time.monotonic() + step.seconds
        while (remaining := deadline - time.monotonic()) > 0:
            activity.heartbeat(step.step)
            await asyncio.sleep(min(0.5, remaining))
        await asyncio.to_thread(
            self._backend.apply,
            step.idempotency_key,
            step.service,
            step.version,
            step.environment,
            step.step,
        )

    @activity.defn
    async def record_deployment(self, record: RecordDeployment) -> None:
        """Record the deploy on the ledger. Workflows can't send Updates, so an Activity does.

        The Update ID is derived from the Nexus request ID, so a retried Activity
        attaches to the same Update instead of recording the deploy twice.
        """
        ledger = self._client.get_workflow_handle(record.ledger_workflow_id)
        try:
            await ledger.execute_update("record_deployment", record, id=record.update_id)
        except WorkflowUpdateFailedError as err:
            message = getattr(err.cause, "message", str(err.cause))
            raise ApplicationError(message, type="DeploymentRejected", non_retryable=True) from err
