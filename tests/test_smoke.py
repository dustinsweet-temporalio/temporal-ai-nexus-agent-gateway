"""The startup smoke test: passes with the dev server's flags, fails plainly without them."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from demo.smoke import REQUIRED_FLAGS, SMOKE_ENDPOINT, SMOKE_TASK_QUEUE, run_smoke
from tests.conftest import TEMPORAL_CLI_VERSION


async def test_a_passing_smoke_test_deletes_its_workflows_and_endpoint(
    temporal_env: WorkflowEnvironment,
) -> None:
    """After make run or make reset, the dev server holds only the demo's state."""
    client = await temporal_env.connect_client(namespace="platform")
    result = await run_smoke(client)
    assert result.passed, result.message
    assert result.seconds < 10
    assert len(result.workflow_ids) == 2  # the caller and the Update-backed target
    assert result.deleted == result.workflow_ids
    assert await smoke_endpoints(client) == []


@pytest.mark.slow
async def test_the_deleted_smoke_workflows_are_gone_within_a_minute(
    temporal_env: WorkflowEnvironment,
) -> None:
    """Slow: the server deletes Workflows in the background, about 30 seconds after the request."""
    client = await temporal_env.connect_client(namespace="platform")
    assert (await run_smoke(client)).passed
    left: list[str] = []
    for _ in range(60):
        left = [w.id async for w in client.list_workflows(f"TaskQueue = '{SMOKE_TASK_QUEUE}'")]
        if not left:
            break
        await asyncio.sleep(1)
    assert left == []


async def smoke_endpoints(client: Client) -> list[str]:
    listed = await client.operator_service.list_nexus_endpoints(
        ListNexusEndpointsRequest(name=SMOKE_ENDPOINT)
    )
    return [e.spec.name for e in listed.endpoints]


async def test_the_smoke_test_fails_plainly_without_the_flags() -> None:
    async with await WorkflowEnvironment.start_local(
        dev_server_download_version=TEMPORAL_CLI_VERSION, dev_server_extra_args=["--namespace", "platform"]
    ) as bare:
        client = await bare.connect_client(namespace="platform")
        result = await run_smoke(client, timeout=timedelta(seconds=3))
        assert await smoke_endpoints(client) == [SMOKE_ENDPOINT], "a failed check leaves its traces"
    assert not result.passed
    assert result.message.startswith(
        "The startup smoke test failed: an Update-backed Nexus Operation didn't return within 3 seconds."
    )
    for flag in REQUIRED_FLAGS:
        assert f"--dynamic-config-value {flag}" in result.message
    assert result.message.endswith("Fix: run make reset, then make run (it starts the dev server with them).")


def test_the_smoke_check_runs_on_the_local_profile_only_and_stays_out_of_the_console(tmp_path: Path) -> None:
    from console.server import Console, Settings, load_registrations
    from demo.smoke import write_skipped
    from demo.supervisor import Settings as RunSettings
    from demo.supervisor import Supervisor

    assert SMOKE_ENDPOINT == "gateway-smoke-check"
    assert Supervisor(RunSettings(profile="local", state_dir=tmp_path)).smoke_applies
    assert not Supervisor(RunSettings(profile="cloud", state_dir=tmp_path)).smoke_applies
    # The console's diagrams are drawn from the teams' registration files only.
    assert SMOKE_ENDPOINT not in [r.endpoint for r in load_registrations()]
    # A skipped check (not local) shows no banner; a missing one does.
    console = Console(Settings(state_dir=tmp_path))
    assert console.smoke_problems() and "hasn't passed" in console.smoke_problems()[0]["text"]
    write_skipped(tmp_path, "cloud")
    assert console.smoke_problems() == []
