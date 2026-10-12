"""Release Engineering: status, deploys, and idempotency, through real Nexus calls."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nexusrpc import HandlerError, HandlerErrorType
from temporalio.client import Client, WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.exceptions import NexusOperationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import (
    ExecuteNexusOperationStartInput,
    Interceptor,
    NexusOperationInboundInterceptor,
    Worker,
)

from release_eng.activities import DeployActivities, RecordDeployment, StepInput
from release_eng.cd_backend import CDBackend
from release_eng.worker import build_worker
from release_eng.workflows import (
    DeployProgress,
    DeployWorkflow,
    EnvironmentLedgerWorkflow,
    LedgerState,
    ledger_id,
    seed_ledger_state,
)
from tests.conftest import after_continue_as_new, create_endpoint, namespace_client
from tests.nexus_caller import NexusCall, NexusCallerWorkflow

QUERY_TIMEOUT = timedelta(seconds=1)  # every Query fails fast
NOAH = {"x-pinecrest-principal": "noah@pinecrest.example", "x-pinecrest-team": "checkout"}


@dataclass
class ReleaseEng:
    env: WorkflowEnvironment
    platform: Client
    client: Client
    backend: CDBackend
    endpoint: str
    task_queue: str
    caller_queue: str
    start_ids: list[str] = field(default_factory=list)  # deploy starts that succeeded
    deploy_attempts: list[str] = field(default_factory=list)  # every deploy start attempt

    async def call(self, operation: str, arguments: dict[str, Any], headers: dict[str, str] = NOAH) -> Any:
        return await self.platform.execute_workflow(
            NexusCallerWorkflow.run,
            NexusCall(self.endpoint, "ReleaseEngineering", operation, arguments, headers),
            id=f"caller-{uuid.uuid4()}",
            task_queue=self.caller_queue,
        )

    async def ledger(self) -> LedgerState:
        handle = self.client.get_workflow_handle_for(EnvironmentLedgerWorkflow.run, ledger_id("checkout-api"))
        return await handle.query(EnvironmentLedgerWorkflow.status, rpc_timeout=QUERY_TIMEOUT)


@pytest.fixture
async def release_eng(
    temporal_env: WorkflowEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ReleaseEng]:
    monkeypatch.setenv("DEPLOY_SECONDS_STAGING", "0.6")
    monkeypatch.setenv("DEPLOY_SECONDS_PRODUCTION", "0.6")
    suffix = uuid.uuid4().hex[:8]
    harness = ReleaseEng(
        env=temporal_env,
        platform=await namespace_client(temporal_env, "platform"),
        client=await namespace_client(temporal_env, "release-eng"),
        backend=CDBackend(tmp_path / "cd_backend.sqlite"),
        endpoint=f"release-eng-tools-{suffix}",
        task_queue=f"release-eng-{suffix}",
        caller_queue=f"caller-{suffix}",
    )
    await create_endpoint(temporal_env, harness.endpoint, "release-eng", harness.task_queue)

    # A fresh, seeded ledger for each test.
    ledger = harness.client.get_workflow_handle(ledger_id("checkout-api"))
    try:
        await ledger.terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:  # not found just means no ledger yet
            raise
    await harness.client.start_workflow(
        EnvironmentLedgerWorkflow.run,
        seed_ledger_state(datetime.now(UTC)),
        id=ledger_id("checkout-api"),
        task_queue=harness.task_queue,
    )

    worker = build_worker(
        harness.client,
        harness.backend,
        harness.task_queue,
        [RecordStartIds(harness.start_ids, attempts=harness.deploy_attempts)],
    )
    caller = Worker(harness.platform, task_queue=harness.caller_queue, workflows=[NexusCallerWorkflow])
    async with worker, caller:
        yield harness


class RecordStartIds(Interceptor):
    """Records deploy start attempts and successes; can fail the first one after it took effect."""

    def __init__(
        self, request_ids: list[str], fail_first_deploy: bool = False, attempts: list[str] | None = None
    ) -> None:
        self.request_ids = request_ids
        self.fail_first_deploy = fail_first_deploy
        self.attempts = attempts if attempts is not None else []

    def intercept_nexus_operation(
        self, next: NexusOperationInboundInterceptor
    ) -> NexusOperationInboundInterceptor:
        outer = self

        class Inbound(NexusOperationInboundInterceptor):
            async def execute_nexus_operation_start(self, input: ExecuteNexusOperationStartInput) -> Any:
                if input.ctx.operation == "deploy":
                    outer.attempts.append(input.ctx.request_id)
                result = await super().execute_nexus_operation_start(input)
                if input.ctx.operation == "deploy":
                    outer.request_ids.append(input.ctx.request_id)
                    if outer.fail_first_deploy:
                        outer.fail_first_deploy = False
                        # The Deploy Workflow has started, but the caller never hears so.
                        raise HandlerError("simulated lost response", type=HandlerErrorType.UNAVAILABLE)
                return result

        return Inbound(next)


async def test_get_release_status_returns_seeded_ledger(release_eng: ReleaseEng) -> None:
    started = time.monotonic()
    status = await release_eng.call("get_release_status", {"service": "checkout-api"})
    elapsed = time.monotonic() - started

    assert status["service"] == "checkout-api"
    assert status["environments"]["staging"]["version"] == "2.3.0"
    assert status["environments"]["production"]["version"] == "2.2.0"
    assert status["environments"]["staging"]["deployed_by"] == "noah@pinecrest.example"
    assert [c["version"] for c in status["candidates"]] == ["2.3.0", "2.3.1", "2.4.0"]
    assert elapsed < 2, f"synchronous status call took {elapsed:.2f}s"


async def test_deploy_rolls_out_in_waves_and_records_the_principal(release_eng: ReleaseEng) -> None:
    result = await release_eng.call(
        "deploy", {"service": "checkout-api", "version": "2.3.1", "environment": "staging"}
    )

    assert result["status"] == "succeeded"
    (request_id,) = release_eng.start_ids
    assert result["deployment_id"] == f"deploy-{request_id}"
    staging = (await release_eng.ledger()).environments["staging"]
    assert (staging.version, staging.deployed_by) == ("2.3.1", "noah@pinecrest.example")
    steps = [s.step for s in release_eng.backend.applied_steps(request_id)]
    assert steps == ["stage", "wave-1", "wave-2", "wave-3", "wave-4", "verify"]
    deploy = release_eng.client.get_workflow_handle_for(DeployWorkflow.run, result["deployment_id"])
    progress: DeployProgress = await deploy.query(DeployWorkflow.progress, rpc_timeout=QUERY_TIMEOUT)
    assert (progress.step, progress.percent, progress.waves_done) == ("done", 100, 4)


async def test_retried_deploy_start_reaches_the_same_workflow(
    release_eng: ReleaseEng, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start whose response is lost is retried with the same request ID: one deploy, not two."""
    # The deploy must outlast Nexus's retry backoff. A shorter one completes through
    # its callback (attached at start) before the retry is even sent.
    monkeypatch.setenv("DEPLOY_SECONDS_STAGING", "5")
    release_eng.start_ids.clear()
    interceptor = RecordStartIds(release_eng.start_ids, fail_first_deploy=True)
    queue = f"{release_eng.task_queue}-retry"
    endpoint = f"{release_eng.endpoint}-retry"
    await create_endpoint(release_eng.env, endpoint, "release-eng", queue)
    async with build_worker(release_eng.client, release_eng.backend, queue, [interceptor]):
        result = await release_eng.platform.execute_workflow(
            NexusCallerWorkflow.run,
            NexusCall(
                endpoint,
                "ReleaseEngineering",
                "deploy",
                {"service": "checkout-api", "version": "2.3.1", "environment": "staging"},
                NOAH,
            ),
            id=f"caller-{uuid.uuid4()}",
            task_queue=release_eng.caller_queue,
        )

    assert len(release_eng.start_ids) == 2, "the handler should have seen the start twice"
    assert len(set(release_eng.start_ids)) == 1, "Nexus should retry with the same request ID"
    workflow_id = f"deploy-{release_eng.start_ids[0]}"
    runs = [w async for w in release_eng.client.list_workflows(f"WorkflowId = '{workflow_id}'")]
    assert len(runs) == 1
    assert result["status"] == "succeeded"
    assert len(release_eng.backend.applied_steps(release_eng.start_ids[0])) == 6


async def test_deploying_a_build_that_is_not_ready_fails_fast(release_eng: ReleaseEng) -> None:
    """A bad version is a bad request: one attempt, an immediate failure, no retries."""
    started = time.monotonic()
    with pytest.raises(WorkflowFailureError) as failure:
        await release_eng.call(
            "deploy", {"service": "checkout-api", "version": "9.9.9", "environment": "staging"}
        )
    elapsed = time.monotonic() - started

    assert isinstance(failure.value.cause, NexusOperationError)
    handler_error = failure.value.cause.cause
    assert isinstance(handler_error, HandlerError)
    assert handler_error.type == HandlerErrorType.BAD_REQUEST
    assert not handler_error.retryable
    assert "9.9.9 is not a build that's ready to deploy. Ready: 2.3.0, 2.3.1, 2.4.0." in str(handler_error)
    assert len(release_eng.deploy_attempts) == 1, "Nexus must not retry a bad request"
    assert release_eng.start_ids == []
    assert elapsed < 2, f"took {elapsed:.2f}s; a retried request would take longer"


async def test_ledger_rejects_a_version_that_is_not_a_candidate(release_eng: ReleaseEng) -> None:
    ledger = release_eng.client.get_workflow_handle_for(
        EnvironmentLedgerWorkflow.run, ledger_id("checkout-api")
    )
    record = RecordDeployment(
        "ledger-checkout-api", "u1", "deploy-x", "production", "9.9.9", "noah@pinecrest.example"
    )
    with pytest.raises(WorkflowUpdateFailedError):
        await ledger.execute_update(EnvironmentLedgerWorkflow.record_deployment, record)
    assert (await release_eng.ledger()).environments["production"].version == "2.2.0"


async def test_calls_without_a_principal_are_rejected(release_eng: ReleaseEng) -> None:
    with pytest.raises(WorkflowFailureError) as failure:
        await release_eng.call("get_release_status", {"service": "checkout-api"}, headers={})
    assert "The call has no x-pinecrest-principal header." in str(
        failure.value.cause.__cause__ or failure.value.cause
    )


def test_cd_backend_ignores_a_repeated_idempotency_key(tmp_path: Path) -> None:
    backend = CDBackend(tmp_path / "cd.sqlite")
    assert backend.apply("r1:wave-2", "checkout-api", "2.3.1", "staging", "wave-2") is True
    assert backend.apply("r1:wave-2", "checkout-api", "2.3.1", "staging", "wave-2") is False
    assert [s.step for s in backend.applied_steps("r1:")] == ["wave-2"]


async def test_a_retried_wave_is_applied_once(tmp_path: Path) -> None:
    """An Activity retry with the same key finds the step applied and does nothing."""
    backend = CDBackend(tmp_path / "cd.sqlite")
    activities = DeployActivities(backend, client=None)  # type: ignore[arg-type]  # steps don't use it
    step = StepInput("r2:wave-1", "checkout-api", "2.3.1", "staging", "wave-1", seconds=0.1)
    env = ActivityEnvironment()
    await env.run(activities.roll_out_wave, step)
    started = time.monotonic()
    await env.run(activities.roll_out_wave, step)
    assert time.monotonic() - started < 0.1, "the repeat should skip the work"
    assert len(backend.applied_steps("r2:")) == 1


async def test_the_ledger_continues_as_new_and_keeps_its_state(release_eng: ReleaseEng) -> None:
    """At its threshold, once no Update is in flight, carrying what is live."""
    ledger_wf = release_eng.client.get_workflow_handle_for(
        EnvironmentLedgerWorkflow.run, ledger_id("checkout-api")
    )
    await ledger_wf.terminate("test: lower the threshold")
    state = seed_ledger_state(datetime.now(UTC))
    state.continue_as_new_after = 2
    await release_eng.client.start_workflow(
        EnvironmentLedgerWorkflow.run, state, id=ledger_id("checkout-api"), task_queue=release_eng.task_queue
    )
    first_run = (await ledger_wf.describe()).run_id
    for i, version in enumerate(["2.3.1", "2.4.0"]):
        record = RecordDeployment(
            ledger_id("checkout-api"), f"u{i}", f"d{i}", "staging", version, "noah@pinecrest.example"
        )
        await ledger_wf.execute_update(EnvironmentLedgerWorkflow.record_deployment, record)

    for _ in range(50):
        if (await ledger_wf.describe()).run_id != first_run:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the ledger never continued as new")
    carried = await after_continue_as_new(release_eng.ledger)
    assert carried.environments["staging"].version == "2.4.0"
    assert (carried.recorded_this_run, carried.continue_as_new_after) == (0, 2)
