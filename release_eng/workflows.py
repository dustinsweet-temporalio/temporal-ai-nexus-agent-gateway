"""Release Engineering's Workflows: the environment ledger and deploys.

Workflow code here is deterministic: no I/O, randomness, or wall-clock reads.
Time comes from `workflow.now()`, and every side effect runs in an Activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from release_eng.activities import DeployActivities, RecordDeployment, StepInput
    from release_eng.generated import DeployOutput

# The ledger continues as new after this many recorded deploys (or sooner, if the
# server suggests it), so its history stays small however long it runs.
LEDGER_CONTINUE_AS_NEW_AFTER = 500


def ledger_id(service: str) -> str:
    return f"ledger-{service}"


@dataclass
class DeployedVersion:
    version: str
    deployed_at: str  # RFC 3339. Workflow state keeps plain strings; the handler converts.
    deployed_by: str


@dataclass
class CandidateBuild:
    version: str
    summary: str


@dataclass
class LedgerState:
    service: str
    environments: dict[str, DeployedVersion]
    candidates: list[CandidateBuild]
    recorded_this_run: int = 0
    continue_as_new_after: int = LEDGER_CONTINUE_AS_NEW_AFTER


def seed_ledger_state(now: datetime) -> LedgerState:
    """The start state used by setup and `make reset`."""
    return LedgerState(
        service="checkout-api",
        environments={
            "staging": DeployedVersion(
                "2.3.0", (now - timedelta(days=1)).isoformat(), "noah@pinecrest.example"
            ),
            "production": DeployedVersion(
                "2.2.0", (now - timedelta(days=6)).isoformat(), "noah@pinecrest.example"
            ),
        },
        candidates=[
            # Short enough to read whole in a narrow pipeline card.
            CandidateBuild("2.3.0", "Tax rounding"),
            CandidateBuild("2.3.1", "Tax rounding fix"),
            CandidateBuild("2.4.0", "Trail pricing"),
        ],
    )


@workflow.defn
class EnvironmentLedgerWorkflow:
    """What is live in each environment, and which builds are ready. One per service."""

    @workflow.init
    def __init__(self, state: LedgerState) -> None:
        self.state = state

    @workflow.run
    async def run(self, state: LedgerState) -> None:
        # At its threshold or when the server suggests it, whichever comes first, and
        # only once no Update is in flight.
        await workflow.wait_condition(
            lambda: (
                (
                    self.state.recorded_this_run >= self.state.continue_as_new_after
                    or workflow.info().is_continue_as_new_suggested()
                )
                and workflow.all_handlers_finished()
            )
        )
        self.state.recorded_this_run = 0
        workflow.continue_as_new(self.state)

    @workflow.query
    def status(self) -> LedgerState:
        return self.state

    @workflow.update
    def record_deployment(self, record: RecordDeployment) -> DeployedVersion:
        deployed = DeployedVersion(record.version, workflow.now().isoformat(), record.deployed_by)
        self.state.environments[record.environment] = deployed
        self.state.recorded_this_run += 1
        return deployed

    @record_deployment.validator
    def _validate_record_deployment(self, record: RecordDeployment) -> None:
        if record.environment not in self.state.environments:
            raise ValueError(f"Unknown environment {record.environment!r}.")
        ready = [c.version for c in self.state.candidates]
        if record.version not in ready:
            raise ValueError(
                f"{record.version} is not a build that's ready to deploy. Ready: {', '.join(ready)}."
            )


@dataclass
class DeployRequest:
    # The Nexus request ID. It names this deploy and keys every side effect in it.
    request_id: str
    service: str
    version: str
    environment: Literal["staging", "production"]
    requested_by: str
    duration_seconds: float


@dataclass
class DeployProgress:
    step: str
    label: str
    percent: int
    waves_done: int = 0
    waves_total: int = 4
    history: list[str] = field(default_factory=list)
    # What is being deployed, so a status view can show it on the right environment.
    service: str = ""
    version: str = ""
    environment: str = ""
    requested_by: str = ""


WAVES = 4
# Share of the deploy's duration spent in each step: stage, each wave, verify.
STAGE_SHARE, WAVE_SHARE, VERIFY_SHARE = 0.10, 0.175, 0.20

STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)


@workflow.defn
class DeployWorkflow:
    """Stage the artifact, roll out in four waves of 25 percent, verify health, record it.

    Rolling out in waves makes progress something the Workflow knows (how many
    waves finished) and makes recovery granular: a killed Worker repeats at most
    the one wave in flight, and the CD backend ignores it if it already applied.
    """

    def __init__(self) -> None:
        self._progress = DeployProgress(step="queued", label="Queued", percent=0)

    @workflow.query
    def progress(self) -> DeployProgress:
        return self._progress

    def _advance(self, step: str, label: str, percent: float) -> None:
        self._progress.step, self._progress.label = step, label
        self._progress.percent = round(percent)
        self._progress.history.append(step)

    async def _run_step(self, activity: Any, step: StepInput) -> None:
        await workflow.execute_activity_method(
            activity,
            step,
            start_to_close_timeout=timedelta(seconds=60),
            # A step that stops heartbeating (its Worker died) is retried within seconds.
            heartbeat_timeout=timedelta(seconds=5),
            retry_policy=STEP_RETRY,
        )

    @workflow.run
    async def run(self, req: DeployRequest) -> DeployOutput:
        def step(name: str, share: float) -> StepInput:
            return StepInput(
                idempotency_key=f"{req.request_id}:{name}",
                service=req.service,
                version=req.version,
                environment=req.environment,
                step=name,
                seconds=req.duration_seconds * share,
            )

        p = self._progress
        p.service, p.version, p.environment, p.requested_by = (
            req.service,
            req.version,
            req.environment,
            req.requested_by,
        )
        status: Literal["succeeded", "failed"] = "succeeded"
        try:
            self._advance("stage", "Staging artifact", 0)
            await self._run_step(DeployActivities.stage_artifact, step("stage", STAGE_SHARE))
            done = STAGE_SHARE
            for wave in range(1, WAVES + 1):
                self._advance(f"wave-{wave}", f"Rolling out (wave {wave} of {WAVES})", done * 100)
                await self._run_step(DeployActivities.roll_out_wave, step(f"wave-{wave}", WAVE_SHARE))
                self._progress.waves_done = wave
                done += WAVE_SHARE
            self._advance("verify", "Verifying health", done * 100)
            await self._run_step(DeployActivities.verify_health, step("verify", VERIFY_SHARE))
            self._advance("record", "Recording the release", 100)
            await workflow.execute_activity_method(
                DeployActivities.record_deployment,
                RecordDeployment(
                    ledger_workflow_id=ledger_id(req.service),
                    update_id=f"{req.request_id}:record",
                    deployment_id=workflow.info().workflow_id,
                    environment=req.environment,
                    version=req.version,
                    deployed_by=req.requested_by,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STEP_RETRY,
            )
            self._advance("done", "Deployed", 100)
        except ActivityError as err:
            status = "failed"
            cause = err.cause.message if isinstance(err.cause, ApplicationError) else str(err)
            self._advance("failed", f"Failed: {cause}", self._progress.percent)

        return DeployOutput(
            deployment_id=workflow.info().workflow_id,
            service=req.service,
            version=req.version,
            environment=req.environment,
            status=status,
            completed_at=workflow.now(),
        )
