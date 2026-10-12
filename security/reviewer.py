"""Security's reviewer agent: three review steps over a release's change set.

Two implementations share one interface:
- MockReviewer (default): scripted, deterministic output for each step, no model,
  no key, zero tokens. Each step takes REVIEW_STEP_SECONDS so there is time to see it.
- ClaudeReviewer (REVIEWER=claude): asks a Claude model for each step and records
  the tokens it used.

The reviewer agent recommends; it never decides. A Security engineer makes the
decision. Durable execution keeps the agent's finished steps from being lost or
paid for twice. It doesn't make the agent's findings correct.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from temporalio.exceptions import ApplicationError

if TYPE_CHECKING:
    import anthropic

STEPS = ("read_change_set", "check_dependencies", "assess_risk")
STEP_LABELS = {
    "read_change_set": "Reading the change set",
    "check_dependencies": "Checking dependencies",
    "assess_risk": "Assessing risk",
}


@dataclass
class FindingRecord:
    severity: str  # info | low | medium | high
    title: str
    detail: str


@dataclass
class StepResult:
    step: str
    summary: str
    findings: list[FindingRecord] = field(default_factory=list)
    recommendation: str | None = None  # assess_risk only: approve | reject
    risk: str | None = None  # assess_risk only: low | medium | high
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "mock"
    attempt: int = 1


class Reviewer(Protocol):
    model: str

    async def review_step(
        self, step: str, change_set: dict[str, Any], earlier: list[StepResult]
    ) -> StepResult: ...


# ---------------------------------------------------------------- mock reviewer

_MOCK_SCRIPT: dict[str, dict[str, StepResult]] = {
    "2.4.0": {
        "read_change_set": StepResult(
            step="read_change_set",
            summary=(
                "18 files changed. The release adds a client for the internal trail-pricing service "
                "and shows an estimated trip price on the checkout summary, behind the "
                "trail_pricing_estimates flag (off by default). The client times out after 800 ms "
                "and falls back to a cached estimate or hides it. New code ships with unit tests."
            ),
        ),
        "check_dependencies": StepResult(
            step="check_dependencies",
            summary=(
                "Two dependency changes. The pinecrest-http package moves from 3.4.1 to 3.5.0, a "
                "minor version bump with no known advisories. The trailcalc package, at 0.4.2, is new, "
                "under the MIT license, with no known advisories."
            ),
        ),
        "assess_risk": StepResult(
            step="assess_risk",
            summary=(
                "The change is additive and sits behind a flag that is off by default, so "
                "turning it on is a separate, reversible decision. The new outbound call is bounded "
                "by a short timeout with a fallback, and neither dependency change carries a known "
                "advisory. I recommend approving 2.4.0."
            ),
            findings=[
                FindingRecord(
                    severity="info",
                    title="New outbound call on the checkout page",
                    detail=(
                        "Checkout now calls the trail-pricing service. A slow or failing pricing "
                        "service degrades the estimate, not checkout, because of the 800 ms timeout "
                        "and cached fallback. Worth a dashboard for the call's latency."
                    ),
                )
            ],
            recommendation="approve",
            risk="low",
        ),
    },
}


class MockReviewer:
    """Scripted, key-free reviewer. Deterministic for each version it knows."""

    model = "mock"

    def __init__(self, step_seconds: float = 5.0) -> None:
        self.step_seconds = step_seconds

    async def review_step(
        self, step: str, change_set: dict[str, Any], earlier: list[StepResult]
    ) -> StepResult:
        script = _MOCK_SCRIPT.get(change_set["version"])
        if script is None:
            raise ApplicationError(
                f"The mock reviewer has no script for {change_set['version']}.", non_retryable=True
            )
        await asyncio.sleep(self.step_seconds)  # stand-in for the agent's work
        scripted = script[step]
        return StepResult(**{**scripted.__dict__, "findings": list(scripted.findings)})


# ---------------------------------------------------------------- Claude reviewer

_SYSTEM = (
    "You are the security reviewer agent for Pinecrest Outfitters, an online outdoor-gear "
    "retailer. You review one release of one service, one step at a time, from the change set "
    "you are given. Be specific and brief. Report only what the change set supports; do not "
    "invent files, packages, or advisories. A Security engineer makes the final decision, so "
    "state your reasoning plainly."
)

_STEP_INSTRUCTIONS = {
    "read_change_set": (
        "Step 1 of 3: read the change set. Summarize what the release changes and where, in two "
        "or three sentences. Report findings only for changes with a security or reliability "
        "concern."
    ),
    "check_dependencies": (
        "Step 2 of 3: check the dependency changes. Summarize each change, its license, and its "
        "known advisories, in two or three sentences. Report a finding for any advisory, risky "
        "license, or unusual change."
    ),
    "assess_risk": (
        "Step 3 of 3: assess the release's risk using the change set and your earlier steps. "
        "Give an overall risk level and recommend approve or reject. Write the summary as one "
        "short paragraph that a Security engineer can decide from. List the findings that "
        "should inform the decision, most severe first."
    ),
}

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["info", "low", "medium", "high"]},
        "title": {"type": "string"},
        "detail": {"type": "string"},
    },
    "required": ["severity", "title", "detail"],
    "additionalProperties": False,
}


def _step_schema(step: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
    }
    if step == "assess_risk":
        properties["risk"] = {"type": "string", "enum": ["low", "medium", "high"]}
        properties["recommendation"] = {"type": "string", "enum": ["approve", "reject"]}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class ClaudeReviewer:
    """Reviews each step with a Claude model and records the tokens it used."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str, max_tokens: int = 1024) -> None:
        self._client = client
        self.model = model
        self.max_tokens = max_tokens  # small and fixed: each step returns a short JSON object

    # region snippet:llm-no-retries
    @classmethod
    def from_key(cls, api_key: str, model: str) -> ClaudeReviewer:
        import anthropic

        # Temporal owns retries. The Activity's retry policy decides when to try a
        # step again, so the client must not retry on its own (its default is 2).
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
        return cls(client, model)

    # endregion

    async def review_step(
        self, step: str, change_set: dict[str, Any], earlier: list[StepResult]
    ) -> StepResult:
        import anthropic

        context = {"change_set": change_set, "earlier_steps": [_earlier(r) for r in earlier]}
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_SYSTEM,
                # The lowest reasoning setting this model allows: short steps don't need it.
                thinking={"type": "between_tools"},
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _step_schema(step)},
                },
                messages=[
                    {
                        "role": "user",
                        "content": f"{_STEP_INSTRUCTIONS[step]}\n\n{json.dumps(context, indent=2)}",
                    }
                ],
            )
        except (
            anthropic.BadRequestError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as err:
            # A retry can't fix these; fail the step instead of retrying it.
            raise ApplicationError(f"Model request rejected: {err.message}", non_retryable=True) from err
        # Rate limits, server errors, and connection errors propagate, and Temporal retries the step.

        label = STEP_LABELS[step]
        if response.stop_reason == "refusal":
            # No fallback model: a declined step fails once, and the desk shows why.
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            reason = f" ({category})" if category else ""
            raise ApplicationError(
                f'The reviewer model declined the step "{label}"{reason}.',
                type="ModelRefusal",
                non_retryable=True,
            )
        if response.stop_reason == "max_tokens":
            raise ApplicationError(
                f'The reviewer model ran out of output tokens in the step "{label}".',
                type="ModelOutputTruncated",
                non_retryable=True,
            )
        data = json.loads(next(block.text for block in response.content if block.type == "text"))
        return StepResult(
            step=step,
            summary=data["summary"],
            findings=[FindingRecord(**f) for f in data["findings"]],
            recommendation=data.get("recommendation"),
            risk=data.get("risk"),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=self.model,
        )


def _earlier(result: StepResult) -> dict[str, Any]:
    return {
        "step": result.step,
        "summary": result.summary,
        "findings": [f.__dict__ for f in result.findings],
    }
