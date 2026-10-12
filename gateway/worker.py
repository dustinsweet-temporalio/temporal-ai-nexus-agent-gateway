"""The gateway's Worker: Namespace "platform", Task Queue "gateway".

Runs the tool registry and agent session Workflows, the gateway's Activities, and
Platform's registration Service, which every platform-registry-<team> Endpoint targets.
It is its own OS process, so stopping it is exactly "the gateway Worker is down":
sessions pause, Operations already running in team Namespaces carry on, and their
results are delivered when the Worker returns.

Run: uv run python -m gateway.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import Interceptor, Worker

from gateway.activities import GatewayActivities
from gateway.connection import connect
from gateway.registry_service import PlatformRegistryHandler
from gateway.registry_workflow import ToolRegistryWorkflow
from gateway.session_workflow import AgentSessionWorkflow

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_QUEUE = "gateway"
# This Worker reports a heartbeat to the server every second (the SDK default is 60),
# so the release console shows a stopped Worker within a few seconds.
WORKER_HEARTBEAT_INTERVAL = timedelta(seconds=1)
log = logging.getLogger("gateway.worker")


def build_worker(
    client: Client, task_queue: str = TASK_QUEUE, interceptors: Sequence[Interceptor] = ()
) -> Worker:
    activities = GatewayActivities(client)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ToolRegistryWorkflow, AgentSessionWorkflow],
        activities=[activities.read_registry],
        nexus_service_handlers=[PlatformRegistryHandler()],
        interceptors=interceptors,
        # After a crash, Workflow tasks wait on the dead Worker's sticky queue for this
        # long before any Worker can take them (SDK default: 10 seconds). A short value
        # makes recovery visible within seconds; the cost is more cache misses under load.
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=3),
    )


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s gateway %(levelname)s %(message)s")
    runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=WORKER_HEARTBEAT_INTERVAL)
    client = await connect("platform", runtime)
    worker = build_worker(client)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    log.info("Gateway Worker polling %s on %s", TASK_QUEUE, client.namespace)
    async with worker:
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
