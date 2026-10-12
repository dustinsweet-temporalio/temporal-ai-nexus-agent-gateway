"""Release Engineering's Worker: Namespace "release-eng", Task Queue "release-eng".

Runs the ledger and Deploy Workflows, the publish Workflow that registers this team's
tools with the gateway, the deploy step Activities, and the Nexus Service handler
behind the release-eng-tools Endpoint. It is its own OS process,
so stopping it is exactly "the Release Engineering Worker is down".

Run: uv run python -m release_eng.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import Interceptor, Worker

from release_eng.activities import DeployActivities
from release_eng.cd_backend import CDBackend
from release_eng.connection import connect
from release_eng.publish import PublishToolsWorkflow
from release_eng.service_handler import ReleaseEngineeringHandler
from release_eng.workflows import DeployWorkflow, EnvironmentLedgerWorkflow

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_QUEUE = "release-eng"
# This Worker reports a heartbeat to the server every second (the SDK default is 60),
# so the release console shows a stopped Worker within a few seconds.
WORKER_HEARTBEAT_INTERVAL = timedelta(seconds=1)
log = logging.getLogger("release_eng.worker")


def deploy_seconds() -> dict[str, float]:
    return {
        "staging": float(os.environ.get("DEPLOY_SECONDS_STAGING", "8")),
        "production": float(os.environ.get("DEPLOY_SECONDS_PRODUCTION", "12")),
    }


def build_worker(
    client: Client,
    backend: CDBackend,
    task_queue: str = TASK_QUEUE,
    interceptors: Sequence[Interceptor] = (),
) -> Worker:
    # The client's default data converter runs NexGen's validators on every team
    # payload this Worker decodes or encodes.
    activities = DeployActivities(backend, client)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[EnvironmentLedgerWorkflow, DeployWorkflow, PublishToolsWorkflow],
        activities=[
            activities.stage_artifact,
            activities.roll_out_wave,
            activities.verify_health,
            activities.record_deployment,
        ],
        nexus_service_handlers=[ReleaseEngineeringHandler(deploy_seconds())],
        interceptors=interceptors,
        # After a crash, Workflow tasks wait on the dead Worker's sticky queue for this
        # long before any Worker can take them (SDK default: 10 seconds). A short value
        # makes recovery visible within seconds; the cost is more cache misses under load.
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=3),
    )


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s release-eng %(levelname)s %(message)s")
    runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=WORKER_HEARTBEAT_INTERVAL)
    client = await connect("release-eng", runtime)
    state_dir = Path(os.environ.get("DEMO_STATE_DIR") or REPO_ROOT / ".demo-state")  # empty means default
    worker = build_worker(client, CDBackend(state_dir / "cd_backend.sqlite"))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    log.info("Release Engineering Worker polling %s on %s", TASK_QUEUE, client.namespace)
    async with worker:
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
