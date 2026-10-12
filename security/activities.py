"""The reviewer agent's three steps, each an Activity.

Each step heartbeats every second while the reviewer works. If the Security Worker
dies mid-step, the heartbeats stop, Temporal times the step out after five seconds,
and retries it on the next Worker. Steps that already finished are in the review
desk's history, so they don't run again and their tokens aren't spent again.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from security.reviewer import Reviewer, StepResult


@dataclass
class StepRequest:
    service: str
    version: str
    step: str
    earlier: list[StepResult]


def change_set_path(fixtures_dir: Path, service: str, version: str) -> Path:
    return fixtures_dir / f"{service}-{version}.json"


class ReviewerActivities:
    def __init__(self, reviewer: Reviewer, fixtures_dir: Path) -> None:
        self._reviewer = reviewer
        self._fixtures_dir = fixtures_dir

    @activity.defn
    async def read_change_set(self, request: StepRequest) -> StepResult:
        return await self._run(request)

    @activity.defn
    async def check_dependencies(self, request: StepRequest) -> StepResult:
        return await self._run(request)

    @activity.defn
    async def assess_risk(self, request: StepRequest) -> StepResult:
        return await self._run(request)

    async def _run(self, request: StepRequest) -> StepResult:
        path = change_set_path(self._fixtures_dir, request.service, request.version)
        if not path.exists():
            raise ApplicationError(f"No change set on file at {path.name}.", non_retryable=True)
        change_set = json.loads(await asyncio.to_thread(path.read_text))

        work = asyncio.create_task(self._reviewer.review_step(request.step, change_set, request.earlier))
        try:
            while not work.done():
                activity.heartbeat(request.step)
                await asyncio.wait({work}, timeout=1.0)
        finally:
            work.cancel()  # no-op when finished; stops the reviewer if this step is cancelled
        result = work.result()
        result.attempt = activity.info().attempt
        return result
