"""The startup smoke test: one Update-backed Nexus Operation round trip.

A dev server started without the dynamic-config flags below fails silently: an
Update-backed Operation (like Security's review_release) starts and then never
returns. `make run` runs this check after the dev server and Workers start, and
stops with a plain message if it doesn't pass within 10 seconds.

The check is self-contained and internal: its own tiny Service behind its own
Endpoint (`gateway-smoke-check`, Namespace platform), served by a Worker that lives
only for the check. It touches no team's contract, calls no model, and never
appears in the console (the console draws only the teams' registration files).
When it passes, it deletes its two Workflows and its Endpoint, so the dev server
holds only the demo's state. When it fails, they stay, for a look in the Temporal UI.

It runs on the local profile only: the flags it checks are dev-server settings, and
Temporal Cloud doesn't take them.

Run by hand: uv run python -m demo.smoke
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import nexusrpc
from temporalio import nexus, workflow
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.nexus.v1 import EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import (
    CreateNexusEndpointRequest,
    DeleteNexusEndpointRequest,
    ListNexusEndpointsRequest,
)
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

SMOKE_ENDPOINT = "gateway-smoke-check"
SMOKE_TASK_QUEUE = "gateway-smoke-check"
SMOKE_TIMEOUT = timedelta(seconds=10)
REQUIRED_FLAGS = (
    "history.enableUpdateCallbacks=true",
    "history.enableCHASMSignalBacklinks=true",
    "history.enableSignalWithStartFromWorkflow=true",
)


def failure_message(seconds: float) -> str:
    flags = "\n".join(f"  --dynamic-config-value {flag}" for flag in REQUIRED_FLAGS)
    return (
        f"The startup smoke test failed: an Update-backed Nexus Operation didn't return within "
        f"{seconds:g} seconds. The dev server is probably missing these flags:\n{flags}\n"
        "Fix: run make reset, then make run (it starts the dev server with them)."
    )


@nexusrpc.service(name="SmokeCheck")
class SmokeCheck:
    echo: nexusrpc.Operation[str, str]


@workflow.defn
class SmokeTargetWorkflow:
    """Backs the Operation with an Update, as Security's review desk does, then ends."""

    def __init__(self) -> None:
        self.answered = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.answered and workflow.all_handlers_finished())

    @workflow.update
    async def echo(self, text: str) -> str:
        # Finish after a short timer, as a review finishes after a person decides: an
        # Update that completed at once would come back without the completion callback
        # the missing flags break, and the check would pass on a broken server.
        await workflow.sleep(timedelta(milliseconds=200))
        self.answered = True
        return text


@workflow.defn
class SmokeCallerWorkflow:
    @workflow.run
    async def run(self, text: str) -> str:
        client = workflow.create_nexus_client(service=SmokeCheck, endpoint=SMOKE_ENDPOINT)
        return await client.execute_operation(
            SmokeCheck.echo, text, schedule_to_close_timeout=SMOKE_TIMEOUT * 3
        )


@nexusrpc.handler.service_handler(service=SmokeCheck)
class SmokeCheckHandler:
    def __init__(self) -> None:
        self.targets: list[str] = []  # the Workflows this handler started, for clean-up

    @nexus.temporal_operation
    async def echo(
        self, ctx: nexus.TemporalStartOperationContext, client: nexus.TemporalNexusClient, input: str
    ) -> nexus.TemporalOperationResult[str]:
        target = await client.client.start_workflow(
            SmokeTargetWorkflow.run, id=f"smoke-target-{ctx.request_id}", task_queue=nexus.info().task_queue
        )
        self.targets.append(target.id)
        return await client.start_workflow_update(
            target.id, SmokeTargetWorkflow.echo, input, update_id=f"smoke-{ctx.request_id}"
        )


async def remove_traces(client: Client, workflow_ids: list[str]) -> list[str]:
    """Delete the check's Workflows (terminating any still running) and its Endpoint.

    Returns the Workflows whose delete the server accepted. The server deletes them in
    the background, so they can still show in the Temporal UI for about 30 seconds.
    """
    accepted = []
    for workflow_id in workflow_ids:
        try:
            await client.workflow_service.delete_workflow_execution(
                DeleteWorkflowExecutionRequest(
                    namespace=client.namespace, workflow_execution=WorkflowExecution(workflow_id=workflow_id)
                )
            )
            accepted.append(workflow_id)
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
    listed = await client.operator_service.list_nexus_endpoints(
        ListNexusEndpointsRequest(name=SMOKE_ENDPOINT)
    )
    for endpoint in listed.endpoints:
        await client.operator_service.delete_nexus_endpoint(
            DeleteNexusEndpointRequest(id=endpoint.id, version=endpoint.version)
        )
    return accepted


async def ensure_smoke_endpoint(client: Client) -> None:
    target = EndpointTarget(
        worker=EndpointTarget.Worker(namespace=client.namespace, task_queue=SMOKE_TASK_QUEUE)
    )
    try:
        await client.operator_service.create_nexus_endpoint(
            CreateNexusEndpointRequest(spec=EndpointSpec(name=SMOKE_ENDPOINT, target=target))
        )
    except RPCError as err:
        if err.status != RPCStatusCode.ALREADY_EXISTS:
            raise


@dataclass
class SmokeResult:
    passed: bool
    seconds: float
    message: str
    workflow_ids: list[str] = field(default_factory=list)  # the check's Workflows
    deleted: list[str] = field(default_factory=list)  # those whose delete the server accepted


async def run_smoke(client: Client, timeout: timedelta = SMOKE_TIMEOUT) -> SmokeResult:
    """One round trip through an Update-backed Operation, within the timeout."""
    await ensure_smoke_endpoint(client)
    started = asyncio.get_running_loop().time()
    text = f"smoke-{uuid.uuid4().hex[:8]}"
    handler = SmokeCheckHandler()
    async with Worker(
        client,
        task_queue=SMOKE_TASK_QUEUE,
        workflows=[SmokeCallerWorkflow, SmokeTargetWorkflow],
        nexus_service_handlers=[handler],
    ):
        handle = await client.start_workflow(
            SmokeCallerWorkflow.run, text, id=f"smoke-{text}", task_queue=SMOKE_TASK_QUEUE
        )
        try:
            answer = await asyncio.wait_for(handle.result(), timeout=timeout.total_seconds())
        except TimeoutError:
            await handle.terminate("smoke test timed out")
            seconds = timeout.total_seconds()
            return SmokeResult(False, seconds, failure_message(seconds))
    seconds = asyncio.get_running_loop().time() - started
    if answer != text:
        return SmokeResult(False, seconds, f"The startup smoke test got {answer!r} back instead of {text!r}.")
    workflow_ids = [handle.id, *handler.targets]
    deleted = await remove_traces(client, workflow_ids)
    return SmokeResult(
        True,
        seconds,
        f"Smoke test passed: an Update-backed Operation returned in {seconds:.2f}s.",
        workflow_ids,
        deleted,
    )


def write_skipped(state_dir: Path, profile: str) -> None:
    """On a profile other than local there is nothing to check; the console shows no banner."""
    state_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "status": "skipped",
        "message": f"The smoke test checks dev-server flags; PROFILE={profile} has none.",
    }
    (state_dir / "smoke.json").write_text(json.dumps({**body, "at": datetime.now(UTC).isoformat()}))


def write_result(state_dir: Path, result: SmokeResult | None) -> None:
    """Record the result for the console, which shows a banner unless it passed."""
    state_dir.mkdir(parents=True, exist_ok=True)
    body = (
        {"status": "running"}
        if result is None
        else {
            "status": "passed" if result.passed else "failed",
            "message": result.message,
            "seconds": round(result.seconds, 2),
        }
    )
    body["at"] = datetime.now(UTC).isoformat()
    (state_dir / "smoke.json").write_text(json.dumps(body))


async def main() -> None:
    from dotenv import load_dotenv

    from gateway.connection import connect

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    result = await run_smoke(await connect("platform"))
    print(result.message)
    raise SystemExit(0 if result.passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
