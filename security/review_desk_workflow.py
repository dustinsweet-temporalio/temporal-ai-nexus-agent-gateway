"""Security's review desk: one long-lived Workflow per service.

The desk queues release reviews, runs the reviewer agent for each (three Activities),
and holds Security's decision. Each `review_release` request is a Workflow Update,
and that Update backs the Nexus Operation the gateway called: the caller gets its
answer when the Update returns, however long a person takes to decide.

Workflow code here is deterministic. Times are stored as RFC 3339 text and turned
into datetimes only at the contract boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from security.activities import ReviewerActivities, StepRequest
    from security.generated import Finding, ReviewReleaseOutput
    from security.reviewer import STEP_LABELS, STEPS, FindingRecord, StepResult

# The desk continues as new after this many decided reviews (or sooner, if the
# server suggests it), and only when no request is still open.
DESK_CONTINUE_AS_NEW_AFTER = 50
# Temporal allows at most 10 in-flight Updates per Workflow. Review requests may use
# 9, which leaves room for Security's decision.
MAX_OPEN_REQUESTS = 9
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
# Reviewer errors whose message already names the step they stopped at.
STEP_NAMED_ERRORS = ("ModelRefusal", "ModelOutputTruncated")

STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)


def desk_id(service: str) -> str:
    return f"review-desk-{service}"


@dataclass
class Person:
    email: str
    name: str
    team: str


@dataclass
class DeskConfig:
    service: str
    people: list[Person]  # from config/principals.yaml
    reviewable_versions: list[str]  # versions with a change set on file
    deciding_team: str = "security"


@dataclass
class ReviewRequest:
    service: str
    version: str
    requested_by: str


@dataclass
class Decision:
    version: str
    decision: Literal["approved", "rejected"]
    decided_by: str
    note: str = ""


@dataclass
class StepRecord:
    step: str
    label: str
    status: str = "pending"  # pending | running | done | failed
    attempt: int = 0
    summary: str = ""
    findings: list[FindingRecord] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class Review:
    service: str
    version: str
    requested_by: str
    requested_at: str
    status: str = "reviewing"  # reviewing | awaiting_decision | decided | failed
    steps: list[StepRecord] = field(default_factory=list)
    risk: str | None = None
    recommendation: str | None = None
    summary: str = ""
    decision: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    note: str = ""
    failure: str | None = None

    @property
    def findings(self) -> list[FindingRecord]:
        """The findings the decision rests on: the risk assessment's, most severe first.

        Earlier steps report what they noticed, and the risk assessment carries
        forward what matters, so taking every step's findings would repeat them.
        """
        final = self.steps[-1].findings if self.steps else []
        return sorted(final, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


@dataclass
class DeskState:
    config: DeskConfig
    reviews: dict[str, Review] = field(default_factory=dict)
    decided_this_run: int = 0
    continue_as_new_after: int = DESK_CONTINUE_AS_NEW_AFTER


@dataclass
class DeskView:
    service: str
    deciding_team: str
    open_requests: int
    reviews: list[Review]  # newest first


@workflow.defn
class ReviewDeskWorkflow:
    @workflow.init
    def __init__(self, state: DeskState) -> None:
        self.state = state
        self.open_requests = 0

    @workflow.run
    async def run(self, state: DeskState) -> None:
        await workflow.wait_condition(
            lambda: (
                (
                    self.state.decided_this_run >= self.state.continue_as_new_after
                    or workflow.info().is_continue_as_new_suggested()
                )
                and workflow.all_handlers_finished()
            )
        )
        self.state.decided_this_run = 0
        workflow.continue_as_new(self.state)

    @workflow.signal
    def configure(self, config: DeskConfig) -> None:
        """Sent with Signal-with-Start on every request, so roster changes take effect."""
        self.state.config = config

    # ------------------------------------------------------------- review_release

    @workflow.update
    async def review_release(self, request: ReviewRequest) -> ReviewReleaseOutput:
        self.open_requests += 1
        try:
            review = self.state.reviews.get(request.version)
            if review is None or review.status == "failed":
                review = self._new_review(request)
                await self._run_reviewer(review)
            # Every request for this version, first or later, waits for the same decision.
            await workflow.wait_condition(lambda: review.status in ("decided", "failed"))
            if review.status == "failed":
                raise ApplicationError(review.failure or "The review failed.", non_retryable=True)
            return self._result(review)
        finally:
            self.open_requests -= 1

    @review_release.validator
    def _validate_review_release(self, request: ReviewRequest) -> None:
        config = self.state.config
        if request.service != config.service:
            raise ValueError(f"This desk reviews {config.service}, not {request.service}.")
        if request.version not in config.reviewable_versions:
            raise ValueError(
                f"Security's review list doesn't include {request.service} {request.version}: "
                "there's no change set on file for it."
            )
        if self.open_requests >= MAX_OPEN_REQUESTS:
            raise ValueError("Security's review desk is at capacity. Try again when a review finishes.")

    def _new_review(self, request: ReviewRequest) -> Review:
        review = Review(
            service=request.service,
            version=request.version,
            requested_by=request.requested_by,
            requested_at=workflow.now().isoformat(),
            steps=[StepRecord(step, STEP_LABELS[step]) for step in STEPS],
        )
        self.state.reviews[request.version] = review
        return review

    async def _run_reviewer(self, review: Review) -> None:
        """The reviewer agent: three Activities in order, each result kept as it lands."""
        earlier: list[StepResult] = []
        for record in review.steps:
            record.status, record.started_at = "running", workflow.now().isoformat()
            try:
                result = await workflow.execute_activity_method(
                    getattr(ReviewerActivities, record.step),
                    StepRequest(review.service, review.version, record.step, earlier),
                    start_to_close_timeout=timedelta(seconds=60),
                    # Heartbeats arrive every second; five seconds without one means the
                    # Worker is gone, and the step is retried on the next Worker.
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=STEP_RETRY,
                )
            except ActivityError as err:
                record.status, review.status = "failed", "failed"
                cause = err.cause
                if isinstance(cause, ApplicationError) and cause.type in STEP_NAMED_ERRORS:
                    review.failure = cause.message  # already names the step
                else:
                    detail = cause.message if isinstance(cause, ApplicationError) else str(err)
                    review.failure = (
                        f'The reviewer agent couldn\'t finish the step "{record.label}": {detail}'
                    )
                return
            record.status, record.completed_at = "done", workflow.now().isoformat()
            record.attempt, record.summary, record.findings = result.attempt, result.summary, result.findings
            record.input_tokens, record.output_tokens = result.input_tokens, result.output_tokens
            record.model = result.model
            earlier.append(result)
        final = earlier[-1]
        review.summary, review.risk, review.recommendation = final.summary, final.risk, final.recommendation
        review.status = "awaiting_decision"

    def _result(self, review: Review) -> ReviewReleaseOutput:
        assert review.decision in ("approved", "rejected") and review.decided_by and review.decided_at
        assert review.recommendation in ("approve", "reject")
        return ReviewReleaseOutput(
            service=review.service,
            version=review.version,
            decision=review.decision,
            decided_by=review.decided_by,
            findings=[Finding(severity=f.severity, title=f.title, detail=f.detail) for f in review.findings],  # type: ignore[arg-type]
            recommendation=review.recommendation,
            summary=review.summary,
            decided_at=datetime.fromisoformat(review.decided_at),
        )

    # ------------------------------------------------------------- record_decision

    @workflow.update
    def record_decision(self, decision: Decision) -> Review:
        review = self.state.reviews[decision.version]
        review.decision, review.decided_by, review.note = (
            decision.decision,
            decision.decided_by,
            decision.note,
        )
        review.decided_at = workflow.now().isoformat()
        review.status = "decided"
        self.state.decided_this_run += 1
        return review

    @record_decision.validator
    def _validate_record_decision(self, decision: Decision) -> None:
        config = self.state.config
        person = next((p for p in config.people if p.email == decision.decided_by), None)
        if person is None or person.team != config.deciding_team:
            team = person.team if person else "not on the roster"
            raise ValueError(
                f"Only the Security team can decide reviews. Signed in as {decision.decided_by} ({team})."
            )
        if decision.decision not in ("approved", "rejected"):
            raise ValueError("A decision is approved or rejected.")
        review = self.state.reviews.get(decision.version)
        if review is None:
            raise ValueError(f"There's no review of {config.service} {decision.version} to decide.")
        if review.status == "reviewing":
            raise ValueError(
                f"The reviewer agent is still reviewing {decision.version}. Decide when it's done."
            )
        if review.status == "decided":
            raise ValueError(f"{decision.version} was already {review.decision} by {review.decided_by}.")
        if review.status == "failed":
            raise ValueError(f"The review of {decision.version} failed; request it again.")

    # ------------------------------------------------------------- desk_view

    @workflow.query
    def desk_view(self) -> DeskView:
        reviews = sorted(self.state.reviews.values(), key=lambda r: r.requested_at, reverse=True)
        return DeskView(
            self.state.config.service, self.state.config.deciding_team, self.open_requests, reviews
        )
