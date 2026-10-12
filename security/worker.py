"""Security's Worker: Namespace "security", Task Queue "security".

Runs the review desk Workflow, the publish Workflow that registers this team's tools
with the gateway, the reviewer agent's step Activities, and the Nexus Service
handler behind the security-tools Endpoint. It is its own OS process, so
stopping it is exactly "the Security Worker is down" (make demo KILL_DURING_REVIEW=1 does).

Run: uv run python -m security.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import Interceptor, Worker

from security.activities import ReviewerActivities
from security.connection import connect
from security.publish import PublishToolsWorkflow
from security.review_desk_workflow import DeskConfig, Person, ReviewDeskWorkflow
from security.reviewer import ClaudeReviewer, MockReviewer, Reviewer
from security.service_handler import SecurityReviewHandler

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_QUEUE = "security"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PRINCIPALS_FILE = REPO_ROOT / "config" / "principals.yaml"
# This Worker reports a heartbeat to the server every second (the SDK default is 60),
# so the release console shows a stopped Worker within a few seconds.
WORKER_HEARTBEAT_INTERVAL = timedelta(seconds=1)
log = logging.getLogger("security.worker")


class ConfigError(Exception):
    """A setting in .env can't be used as given."""


def build_reviewer() -> Reviewer:
    """MockReviewer unless REVIEWER=claude. Never logs or prints the API key."""
    choice = os.environ.get("REVIEWER", "mock").strip().lower()
    if choice == "mock":
        return MockReviewer(step_seconds=float(os.environ.get("REVIEW_STEP_SECONDS", "5")))
    if choice == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ConfigError(
                "REVIEWER=claude needs ANTHROPIC_API_KEY in .env. Set it, or use REVIEWER=mock."
            )
        return ClaudeReviewer.from_key(api_key, os.environ.get("REVIEWER_MODEL", "claude-sonnet-5-5"))
    raise ConfigError(f"REVIEWER must be mock or claude, not {choice!r}.")


def load_people(principals_file: Path = PRINCIPALS_FILE) -> list[Person]:
    with principals_file.open() as f:
        directory = yaml.safe_load(f)
    return [Person(p["email"], p["name"], p["team"]) for p in directory["people"]]


def desk_config_factory(
    principals_file: Path = PRINCIPALS_FILE, fixtures_dir: Path = FIXTURES_DIR
) -> Callable[[str], DeskConfig]:
    """Builds a desk's roster and review list, read fresh for every request.

    The handler passes both to the desk in the Signal-with-Start, so a roster change
    or a new change set takes effect on the next request. The desk never reads files.
    """

    def desk_config(service: str) -> DeskConfig:
        prefix = f"{service}-"
        versions = sorted(p.stem.removeprefix(prefix) for p in fixtures_dir.glob(f"{prefix}*.json"))
        return DeskConfig(service=service, people=load_people(principals_file), reviewable_versions=versions)

    return desk_config


def build_worker(
    client: Client,
    reviewer: Reviewer,
    task_queue: str = TASK_QUEUE,
    principals_file: Path = PRINCIPALS_FILE,
    fixtures_dir: Path = FIXTURES_DIR,
    interceptors: Sequence[Interceptor] = (),
) -> Worker:
    # The client's default data converter runs NexGen's validators on every team
    # payload this Worker decodes or encodes.
    activities = ReviewerActivities(reviewer, fixtures_dir)
    desk_config = desk_config_factory(principals_file, fixtures_dir)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ReviewDeskWorkflow, PublishToolsWorkflow],
        activities=[activities.read_change_set, activities.check_dependencies, activities.assess_risk],
        nexus_service_handlers=[SecurityReviewHandler(desk_config)],
        interceptors=interceptors,
        # After a crash, Workflow tasks wait on the dead Worker's sticky queue for this
        # long before any Worker can take them (SDK default: 10 seconds). A short value
        # makes recovery visible within seconds; the cost is more cache misses under load.
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=3),
    )


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s security %(levelname)s %(message)s")
    reviewer = build_reviewer()
    runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=WORKER_HEARTBEAT_INTERVAL)
    client = await connect("security", runtime)
    worker = build_worker(client, reviewer)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    log.info("Security Worker polling %s on %s (reviewer: %s)", TASK_QUEUE, client.namespace, reviewer.model)
    async with worker:
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
