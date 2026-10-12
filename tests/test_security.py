"""Security: the review desk, the Update-backed Operation, and recovery, through real Nexus calls.

No test calls a model. The reviewer is the scripted mock, and ClaudeReviewer is
checked against a fake client.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from nexusrpc import HandlerError
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError, NexusOperationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from security.review_desk_workflow import Decision, DeskView, ReviewDeskWorkflow, desk_id
from security.reviewer import ClaudeReviewer, MockReviewer, StepResult
from security.worker import build_worker
from tests.conftest import after_continue_as_new, create_endpoint, namespace_client
from tests.nexus_caller import NexusCall, NexusCallerWorkflow

NOAH = {"x-pinecrest-principal": "noah@pinecrest.example", "x-pinecrest-team": "checkout"}
ABE = "abe@pinecrest.example"
DESK = desk_id("checkout-api")
ROOT = Path(__file__).resolve().parent.parent


class CountingReviewer(MockReviewer):
    """The mock reviewer, counting calls per step, with a chosen duration per step."""

    def __init__(self, seconds: dict[str, float]) -> None:
        super().__init__()
        self.seconds = seconds
        self.calls: Counter[str] = Counter()

    async def review_step(
        self, step: str, change_set: dict[str, Any], earlier: list[StepResult]
    ) -> StepResult:
        self.calls[step] += 1
        self.step_seconds = self.seconds.get(step, 0.2)
        return await super().review_step(step, change_set, earlier)


@dataclass
class SecurityDesk:
    env: WorkflowEnvironment
    platform: Client
    client: Client
    endpoint: str
    task_queue: str
    caller_queue: str
    reviewer: CountingReviewer

    async def request_review(self, version: str = "2.4.0", headers: dict[str, str] = NOAH) -> WorkflowHandle:
        return await self.platform.start_workflow(
            NexusCallerWorkflow.run,
            NexusCall(self.endpoint, "SecurityReview", "review_release",
                      {"service": "checkout-api", "version": version}, headers),
            id=f"caller-{uuid.uuid4()}",
            task_queue=self.caller_queue,
        )  # fmt: skip

    async def view(self) -> DeskView:
        # A short timeout, as the console uses: a Query to a run with no Worker blocks otherwise.
        return await self.client.get_workflow_handle_for(ReviewDeskWorkflow.run, DESK).query(
            ReviewDeskWorkflow.desk_view, rpc_timeout=timedelta(seconds=1)
        )

    async def wait_for(self, check: Callable[[DeskView], bool], timeout: float = 20) -> DeskView:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                view = await self.view()
                if check(view):
                    return view
            except RPCError:
                pass  # the desk isn't up yet
            await asyncio.sleep(0.1)
        raise TimeoutError("the desk never reached the expected state")

    async def decide(self, decided_by: str, decision: str = "approved", version: str = "2.4.0") -> Any:
        desk = self.client.get_workflow_handle_for(ReviewDeskWorkflow.run, DESK)
        return await desk.execute_update(
            ReviewDeskWorkflow.record_decision,
            Decision(version, decision, decided_by, "Reviewed findings; low risk."),  # type: ignore[arg-type]
        )


def message_of(failure: WorkflowFailureError) -> str:
    """The reason a caller sees for a failed Nexus call: the error's .message, never str()."""
    assert isinstance(failure.cause, NexusOperationError)
    reason = failure.cause.cause
    assert isinstance(reason, ApplicationError | HandlerError), f"unexpected {type(reason)}"
    return reason.message


def awaiting_decision(view: DeskView) -> bool:
    return bool(view.reviews) and view.reviews[0].status == "awaiting_decision"


@pytest.fixture
async def security(temporal_env: WorkflowEnvironment) -> AsyncIterator[SecurityDesk]:
    suffix = uuid.uuid4().hex[:8]
    harness = SecurityDesk(
        env=temporal_env,
        platform=await namespace_client(temporal_env, "platform"),
        client=await namespace_client(temporal_env, "security"),
        endpoint=f"security-tools-{suffix}",
        task_queue=f"security-{suffix}",
        caller_queue=f"caller-{suffix}",
        reviewer=CountingReviewer({"read_change_set": 0.2, "check_dependencies": 0.2, "assess_risk": 0.2}),
    )
    await create_endpoint(temporal_env, harness.endpoint, "security", harness.task_queue)
    try:
        await harness.client.get_workflow_handle(DESK).terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    worker = build_worker(harness.client, harness.reviewer, harness.task_queue)
    caller = Worker(harness.platform, task_queue=harness.caller_queue, workflows=[NexusCallerWorkflow])
    async with worker, caller:
        yield harness


async def test_review_runs_three_steps_then_waits_for_a_decision(security: SecurityDesk) -> None:
    caller = await security.request_review()
    view = await security.wait_for(awaiting_decision)

    review = view.reviews[0]
    assert [s.step for s in review.steps] == ["read_change_set", "check_dependencies", "assess_risk"]
    assert all(s.status == "done" and s.attempt == 1 for s in review.steps)
    assert all(s.input_tokens == 0 and s.output_tokens == 0 for s in review.steps)
    assert (review.requested_by, review.recommendation, review.risk) == (
        "noah@pinecrest.example",
        "approve",
        "low",
    )
    assert [f.severity for f in review.findings] == ["info"]
    status = (await caller.describe()).status
    assert status is not None and status.name == "RUNNING", "the Operation waits for a person"


async def test_noah_cannot_decide_a_security_review(security: SecurityDesk) -> None:
    await security.request_review()
    await security.wait_for(awaiting_decision)

    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await security.decide("noah@pinecrest.example")
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == (
        "Only the Security team can decide reviews. Signed in as noah@pinecrest.example (checkout)."
    )
    assert (await security.view()).reviews[0].status == "awaiting_decision"


async def test_abes_decision_completes_the_operation(security: SecurityDesk) -> None:
    caller = await security.request_review()
    await security.wait_for(awaiting_decision)

    await security.decide(ABE)
    result = await caller.result()

    assert (result["decision"], result["decided_by"], result["recommendation"]) == (
        "approved",
        ABE,
        "approve",
    )
    assert result["findings"][0]["severity"] == "info"
    assert result["summary"].startswith("The change is additive")
    # The console shows the risk beside the summary, so the summary doesn't open with it.
    assert not result["summary"].lower().startswith(result["recommendation"])
    assert not result["summary"].lower().startswith("low risk")
    assert result["decided_at"]


async def test_two_requests_for_one_version_share_one_review(security: SecurityDesk) -> None:
    first = await security.request_review()
    second = await security.request_review()
    await security.wait_for(awaiting_decision)
    await security.decide(ABE)

    results = await asyncio.gather(first.result(), second.result())
    assert results[0] == results[1]
    assert security.reviewer.calls == Counter(
        {"read_change_set": 1, "check_dependencies": 1, "assess_risk": 1}
    ), "the reviewer agent ran once for both requests"


async def test_a_version_with_no_change_set_is_refused(security: SecurityDesk) -> None:
    caller = await security.request_review(version="9.9.9")
    with pytest.raises(WorkflowFailureError) as failure:
        await caller.result()
    assert "Security's review list doesn't include" in message_of(failure.value)
    assert security.reviewer.calls == Counter()


async def test_deciding_before_the_reviewer_finishes_is_refused(security: SecurityDesk) -> None:
    security.reviewer.seconds["check_dependencies"] = 3
    await security.request_review()
    await security.wait_for(lambda v: bool(v.reviews) and v.reviews[0].steps[1].status == "running")
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await security.decide(ABE)
    assert isinstance(refused.value.cause, ApplicationError)
    assert "still reviewing 2.4.0" in refused.value.cause.message


async def test_review_requests_without_a_principal_are_refused(security: SecurityDesk) -> None:
    caller = await security.request_review(headers={})
    with pytest.raises(WorkflowFailureError) as failure:
        await caller.result()
    assert message_of(failure.value) == "The call has no x-pinecrest-principal header."


async def test_review_survives_the_security_worker_dying_during_step_two(
    temporal_env: WorkflowEnvironment,
) -> None:
    """Step 1 isn't rerun; step 2 is retried on the next Worker and completes."""
    suffix = uuid.uuid4().hex[:8]
    client = await namespace_client(temporal_env, "security")
    platform = await namespace_client(temporal_env, "platform")
    endpoint, queue = f"security-tools-{suffix}", f"security-{suffix}"
    await create_endpoint(temporal_env, endpoint, "security", queue)
    try:
        await client.get_workflow_handle(DESK).terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    reviewer = CountingReviewer({"read_change_set": 0.2, "check_dependencies": 2.0, "assess_risk": 0.2})
    harness = SecurityDesk(temporal_env, platform, client, endpoint, queue, f"caller-{suffix}", reviewer)

    async with Worker(platform, task_queue=harness.caller_queue, workflows=[NexusCallerWorkflow]):
        first_worker = build_worker(client, reviewer, queue)
        async with first_worker:
            await harness.request_review()
            await harness.wait_for(lambda v: bool(v.reviews) and v.reviews[0].steps[1].status == "running")
            await asyncio.sleep(0.5)
        # The first Worker is gone, mid step 2. A new one picks the review up.
        async with build_worker(client, reviewer, queue):
            view = await harness.wait_for(awaiting_decision, timeout=30)

    steps = {s.step: s for s in view.reviews[0].steps}
    assert reviewer.calls["read_change_set"] == 1, "step 1 must not run again"
    assert reviewer.calls["check_dependencies"] == 2
    assert steps["read_change_set"].attempt == 1
    assert steps["check_dependencies"].attempt == 2
    assert steps["assess_risk"].status == "done"


# ---------------------------------------------------------------- ClaudeReviewer, offline


def test_claude_reviewer_client_never_retries_on_its_own() -> None:
    reviewer = ClaudeReviewer.from_key("not-a-real-key", "claude-sonnet-5-5")
    assert reviewer._client.max_retries == 0  # Temporal owns retries


async def test_claude_reviewer_records_tokens_and_asks_for_the_lowest_reasoning_setting() -> None:
    sent: dict[str, Any] = {}

    async def create(**kwargs: Any) -> Any:
        sent.update(kwargs)
        body = {
            "summary": "Low risk.",
            "findings": [{"severity": "info", "title": "t", "detail": "d"}],
            "risk": "low",
            "recommendation": "approve",
        }
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(body))],
            usage=SimpleNamespace(input_tokens=1234, output_tokens=210),
        )

    fake_client: Any = SimpleNamespace(messages=SimpleNamespace(create=create))
    reviewer = ClaudeReviewer(fake_client, "claude-sonnet-5-5")
    result = await reviewer.review_step("assess_risk", {"version": "2.4.0"}, [])

    assert (result.input_tokens, result.output_tokens) == (1234, 210)
    assert (result.recommendation, result.risk, result.model) == ("approve", "low", "claude-sonnet-5-5")
    assert sent["thinking"] == {"type": "between_tools"}
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"


async def test_a_tenth_open_request_gets_a_plain_refusal(security: SecurityDesk) -> None:
    """Nine open requests use nine of Temporal's ten in-flight Update slots; the tenth is refused."""
    open_callers = [await security.request_review() for _ in range(9)]
    await security.wait_for(lambda v: v.open_requests == 9)

    tenth = await security.request_review()
    with pytest.raises(WorkflowFailureError) as failure:
        await tenth.result()
    assert (
        message_of(failure.value)
        == "Security's review desk is at capacity. Try again when a review finishes."
    )
    for caller in open_callers:
        await caller.terminate("test cleanup")


class RefusingClient:
    """A fake Anthropic client whose model declines every step. No network."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            stop_reason="refusal",
            stop_details=SimpleNamespace(type="refusal", category="cyber", explanation=None),
            content=[],
            usage=SimpleNamespace(input_tokens=900, output_tokens=0),
        )


async def test_a_model_refusal_is_not_retried_and_names_the_step() -> None:
    client: Any = RefusingClient()
    with pytest.raises(ApplicationError) as refused:
        await ClaudeReviewer(client, "claude-sonnet-5-5").review_step("check_dependencies", {}, [])
    assert refused.value.non_retryable
    assert refused.value.type == "ModelRefusal"
    assert refused.value.message == 'The reviewer model declined the step "Checking dependencies" (cyber).'


async def test_a_model_refusal_fails_the_review_once_and_the_desk_shows_why(
    temporal_env: WorkflowEnvironment,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    client = await namespace_client(temporal_env, "security")
    platform = await namespace_client(temporal_env, "platform")
    endpoint, queue = f"security-tools-{suffix}", f"security-{suffix}"
    await create_endpoint(temporal_env, endpoint, "security", queue)
    try:
        await client.get_workflow_handle(DESK).terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    model: Any = RefusingClient()
    reviewer = ClaudeReviewer(model, "claude-sonnet-5-5")
    harness = SecurityDesk(
        temporal_env, platform, client, endpoint, queue, f"caller-{suffix}", CountingReviewer({})
    )

    caller_worker = Worker(platform, task_queue=harness.caller_queue, workflows=[NexusCallerWorkflow])
    async with caller_worker, build_worker(client, reviewer, queue):
        caller = await harness.request_review()
        with pytest.raises(WorkflowFailureError) as failure:
            await caller.result()
        view = await harness.wait_for(lambda v: bool(v.reviews) and v.reviews[0].status == "failed")

    expected = 'The reviewer model declined the step "Reading the change set" (cyber).'
    assert model.calls == 1, "a refusal must not be retried"
    assert view.reviews[0].failure == expected
    assert view.reviews[0].steps[0].status == "failed"
    assert message_of(failure.value) == expected


async def test_a_roster_change_takes_effect_on_the_next_request(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The handler reads the roster on every request and sends it with the Signal-with-Start."""
    suffix = uuid.uuid4().hex[:8]
    client = await namespace_client(temporal_env, "security")
    platform = await namespace_client(temporal_env, "platform")
    endpoint, queue = f"security-tools-{suffix}", f"security-{suffix}"
    await create_endpoint(temporal_env, endpoint, "security", queue)
    try:
        await client.get_workflow_handle(DESK).terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    principals = tmp_path / "principals.yaml"
    roster = yaml.safe_load((ROOT / "config" / "principals.yaml").read_text())
    without_abe = {**roster, "people": [p for p in roster["people"] if p["team"] != "security"]}
    principals.write_text(yaml.safe_dump(without_abe))
    reviewer = CountingReviewer({})
    harness = SecurityDesk(temporal_env, platform, client, endpoint, queue, f"caller-{suffix}", reviewer)

    caller_worker = Worker(platform, task_queue=harness.caller_queue, workflows=[NexusCallerWorkflow])
    async with caller_worker, build_worker(client, reviewer, queue, principals_file=principals):
        first = await harness.request_review()
        await harness.wait_for(awaiting_decision)
        with pytest.raises(WorkflowUpdateFailedError) as refused:
            await harness.decide(ABE)
        assert isinstance(refused.value.cause, ApplicationError)
        assert refused.value.cause.message.endswith(f"Signed in as {ABE} (not on the roster).")

        principals.write_text(yaml.safe_dump(roster))  # Abe joins Security
        second = await harness.request_review()
        await harness.wait_for(lambda v: v.open_requests == 2)
        await harness.decide(ABE)
        assert (await first.result())["decided_by"] == ABE
        assert (await second.result())["decided_by"] == ABE


async def test_the_desk_continues_as_new_once_requests_finish_and_keeps_decided_reviews(
    security: SecurityDesk,
) -> None:
    """At its threshold, only when no Update is in flight, carrying its reviews and roster."""
    from security.review_desk_workflow import DeskState
    from security.worker import desk_config_factory

    config = desk_config_factory()("checkout-api")
    await security.client.start_workflow(
        ReviewDeskWorkflow.run,
        DeskState(config, continue_as_new_after=1),
        id=DESK,
        task_queue=security.task_queue,
    )
    desk = security.client.get_workflow_handle_for(ReviewDeskWorkflow.run, DESK)
    first_run = (await desk.describe()).run_id
    caller = await security.request_review()
    await security.wait_for(awaiting_decision)
    assert (await desk.describe()).run_id == first_run, "the review request is still open"

    await security.decide(ABE)
    first = await caller.result()
    for _ in range(50):
        if (await desk.describe()).run_id != first_run:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the desk never continued as new")

    assert (await after_continue_as_new(security.view)).reviews[0].status == "decided"
    again = await (await security.request_review()).result()
    assert again == first, "the decided review is answered from the carried state"
    assert security.reviewer.calls["read_change_set"] == 1
