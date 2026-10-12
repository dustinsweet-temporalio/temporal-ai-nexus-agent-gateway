"""The agent session: one Workflow per person per client, receiving every tool call.

For each call the session reads the registry, checks the arguments against the
tool's contract schema, applies policy, and then either refuses the call, runs it
over Nexus, holds it for a person's approval, or runs a gate tool first. Every
outcome is recorded on the call, and the Workflow history is the audit trail.

Durable execution is what makes this a control: a call held for approval waits
here whether the approver takes a minute or a weekend, and a gateway restart loses
nothing. Workflow code is deterministic; times are RFC 3339 text.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, NexusOperationError

with workflow.unsafe.imports_passed_through():
    from gateway.activities import GatewayActivities
    from gateway.contracts import argument_violations
    from gateway.models import (
        PENDING,
        REGISTRY_ID,
        Approval,
        ApprovalDecision,
        BlockedRelease,
        CallRecord,
        CallResponse,
        CallStatus,
        Directory,
        GateRecord,
        RegistryEntry,
        RegistrySnapshot,
        SessionInput,
        SessionView,
        StateChange,
        ToolCallRequest,
    )
    from gateway.policy import BLOCKED_RELEASE_RULE, DEFAULT_RULE, blocked_release_for, evaluate, render
    from gateway.registry_workflow import BlockRelease, ToolRegistryWorkflow

# A Workflow can have at most 30 Nexus Operations in flight. The session caps
# pending calls (running or waiting) below that.
MAX_PENDING_CALLS = 25
# The session continues as new after this many calls (or sooner, if the server
# suggests it), once no Update is in flight and no Nexus Operation is running.
SESSION_CONTINUE_AS_NEW_AFTER = 500
# Finished calls carried across Continue-As-New, for the console. History keeps them all.
FINISHED_CALLS_CARRIED = 100


@dataclass
class HeldCall:
    """What a waiting call needs later, captured when it was submitted."""

    entry: RegistryEntry
    directory: Directory  # the roster at submission: who may approve can't change under the call


@dataclass
class SessionState:
    input: SessionInput
    calls: dict[str, CallRecord] = field(default_factory=dict)
    held: dict[str, HeldCall] = field(default_factory=dict)
    blocked_here: list[BlockedRelease] = field(default_factory=list)
    principal_team: str = ""  # from the roster, sent with every call
    calls_this_run: int = 0
    continue_as_new_after: int = SESSION_CONTINUE_AS_NEW_AFTER
    finished_calls_carried: int = FINISHED_CALLS_CARRIED


def describe_call(tool: str, arguments: dict[str, Any], template: str = "") -> str:
    """What a call does, in the owning team's words when it gave some.

    With a template from the tool's registration ("deploy {service} {version} to
    {environment}") this reads "deploy checkout-api 2.3.1 to production". Without
    one, it falls back to the tool name and the argument values.
    """
    described = render(template, arguments) if template else None
    return described or " ".join([tool, *(str(v) for v in arguments.values())])


def call_summary(tool: str, arguments: dict[str, Any], principal: str, template: str = "") -> str:
    """One line for audit rows and the Temporal UI: what the call does, and for whom."""
    return f"{describe_call(tool, arguments, template)} for {principal.split('@')[0]}"


def approvers_sentence(directory: Directory, role: str) -> str:
    people = [f"{p.name} ({p.title})" for p in directory.people if role in p.roles]
    if not people:
        return f"Someone with the {role} role decides."
    return f"{' or '.join(people)} {'decides' if len(people) == 1 else 'decide'}."


def canonical(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


@workflow.defn
class AgentSessionWorkflow:
    @workflow.init
    def __init__(self, state: SessionState) -> None:
        self.state = state
        self._activity = 0  # bumps on every call, decision, and finished dispatch
        self._dispatching = 0  # Nexus Operations this run is waiting on

    # ------------------------------------------------------------- lifecycle

    @workflow.run
    async def run(self, state: SessionState) -> None:
        while True:
            seen = self._activity
            idle = None if self._pending() else timedelta(seconds=self.state.input.idle_close_seconds)
            try:
                await workflow.wait_condition(
                    lambda seen=seen: self._activity != seen or self._ready_to_continue_as_new(), timeout=idle
                )
            except TimeoutError:
                if not self._pending() and workflow.all_handlers_finished():
                    return  # idle: the session closes; the next call starts a new run
                continue
            if self._ready_to_continue_as_new():
                workflow.continue_as_new(self._carried())

    def _pending(self) -> list[CallRecord]:
        return [c for c in self.state.calls.values() if c.status in PENDING]

    def _ready_to_continue_as_new(self) -> bool:
        due = (
            self.state.calls_this_run >= self.state.continue_as_new_after
            or workflow.info().is_continue_as_new_suggested()
        )
        return due and self._dispatching == 0 and workflow.all_handlers_finished()

    def _carried(self) -> SessionState:
        finished = [c for c in self.state.calls.values() if c.status not in PENDING]
        carried = finished[-self.state.finished_calls_carried :] if self.state.finished_calls_carried else []
        keep = {c.call_id for c in self._pending()} | {c.call_id for c in carried}
        return SessionState(
            input=self.state.input,
            calls={k: v for k, v in self.state.calls.items() if k in keep},
            held={k: v for k, v in self.state.held.items() if k in keep},
            blocked_here=self.state.blocked_here,
            principal_team=self.state.principal_team,
            continue_as_new_after=self.state.continue_as_new_after,
            finished_calls_carried=self.state.finished_calls_carried,
        )

    # ------------------------------------------------------------- submit_tool_call

    @workflow.update
    async def submit_tool_call(self, request: ToolCallRequest) -> CallResponse:
        self._activity += 1
        if request.call_id in self.state.calls:
            return self._response(self.state.calls[request.call_id])
        snapshot = await self._read_registry()
        rec = self._new_record(request, snapshot)

        entry = snapshot.tools.get(request.tool)
        if entry is None:
            return self._deny(
                rec, "tool_not_registered", f"There's no tool called {request.tool} on the gateway."
            )
        rec.endpoint, rec.service, rec.operation = entry.endpoint, entry.service, entry.operation
        rec.summary = call_summary(request.tool, request.arguments, request.principal, entry.summary_template)
        rec.description = describe_call(request.tool, request.arguments, entry.summary_template)
        violations = argument_violations(entry.input_schema, request.arguments)
        if violations:
            return self._deny(
                rec,
                "invalid-arguments",
                f"The arguments don't match the tool's contract: {'; '.join(violations)}.",
            )

        offset = self.state.input.display_utc_offset_minutes
        blocked = snapshot.blocked_releases + self.state.blocked_here
        decision = evaluate(snapshot.policy, blocked, request.tool, request.arguments, offset)
        rec.policy_version, rec.rule_id, rec.effect = (
            decision.policy_version,
            decision.rule_id,
            decision.effect,
        )
        rec.blocks_release = decision.blocks_release
        if decision.rule_id == BLOCKED_RELEASE_RULE:
            # The "push it anyway" refusal keeps its exact wording.
            return self._deny(rec, decision.rule_id, decision.reason, sentence=decision.reason)
        if decision.effect == "deny":
            why = "No policy rule allows it." if decision.rule_id == DEFAULT_RULE else "Policy refuses it."
            return self._deny(rec, decision.rule_id, why)

        if decision.effect == "require_gate" and decision.gate not in snapshot.tools:
            return self._deny(
                rec,
                decision.rule_id,
                f"Policy requires {decision.gate} first, and {decision.gate} isn't registered yet.",
            )

        earlier = self._in_flight_duplicate(rec)
        if earlier is not None:  # nothing awaited since the check, so no second copy can slip in
            return CallResponse(**{**self._response(earlier).__dict__, "note": "Already in progress"})

        self._record(rec)
        if decision.effect == "allow":
            self._start(rec, entry)
            return await self._answer_within_budget(rec)
        if decision.effect == "require_approval":
            rec.approver_role = decision.approver_role
            self.state.held[rec.call_id] = HeldCall(entry, snapshot.directory)
            who = approvers_sentence(snapshot.directory, rec.approver_role or "")
            self._set(rec, "awaiting_approval", f"Held for approval: {rec.description}. {who}")
            return self._response(rec)
        return self._start_gate(rec, entry, snapshot, decision.gate or "")

    @submit_tool_call.validator
    def _validate_submit(self, request: ToolCallRequest) -> None:
        if request.principal != self.state.input.principal:
            raise ValueError(
                f"This session belongs to {self.state.input.principal}; {request.principal} can't use it."
            )
        if request.call_id not in self.state.calls and len(self._pending()) >= MAX_PENDING_CALLS:
            raise ValueError(
                f"This session already has {MAX_PENDING_CALLS} calls running or waiting. "
                "Wait for one to finish, then try again."
            )

    async def _read_registry(self) -> RegistrySnapshot:
        try:
            return await workflow.execute_local_activity_method(
                GatewayActivities.read_registry,
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200), maximum_attempts=3),
            )
        except ActivityError as err:
            raise ApplicationError(
                "The gateway couldn't read its tool registry. Run `make reset` to set it up.",
                type="RegistryUnavailable",
                non_retryable=True,
            ) from err

    def _new_record(self, request: ToolCallRequest, snapshot: RegistrySnapshot) -> CallRecord:
        person = snapshot.directory.find(request.principal)
        if person is not None:
            self.state.principal_team = person.team
        return CallRecord(
            call_id=request.call_id,
            tool=request.tool,
            arguments=request.arguments,
            summary=call_summary(request.tool, request.arguments, request.principal),
            description=describe_call(request.tool, request.arguments),
            principal=request.principal,
            principal_name=person.name if person else request.principal,
            client_name=self.state.input.client_name,
            session_id=workflow.info().workflow_id,
            status="running",
            submitted_at=workflow.now().isoformat(),
        )

    def _record(self, rec: CallRecord) -> None:
        self.state.calls[rec.call_id] = rec
        self.state.calls_this_run += 1

    def _set(self, rec: CallRecord, status: CallStatus, message: str, note: str = "") -> None:
        rec.status, rec.message = status, message
        rec.history.append(StateChange(status, workflow.now().isoformat(), note))
        if status not in PENDING:
            rec.finished_at = workflow.now().isoformat()
        self._activity += 1

    def _deny(self, rec: CallRecord, rule_id: str, why: str, sentence: str | None = None) -> CallResponse:
        rec.rule_id, rec.effect = rule_id, "deny"
        self._record(rec)
        self._set(rec, "denied", sentence or f"Denied: {rec.description}. {why}")
        return self._response(rec)

    def _in_flight_duplicate(self, rec: CallRecord) -> CallRecord | None:
        """The same person asking for the same tool with the same arguments, still in flight."""
        key = canonical(rec.arguments)
        return next(
            (
                c
                for c in self.state.calls.values()
                if c.status in PENDING
                and c.principal == rec.principal
                and c.tool == rec.tool
                and canonical(c.arguments) == key
            ),
            None,
        )

    async def _answer_within_budget(self, rec: CallRecord) -> CallResponse:
        """Sync-to-async conversion: answer fast either way."""
        budget = self.state.input.sync_budget_seconds
        try:
            await workflow.wait_condition(
                lambda: rec.status not in PENDING, timeout=timedelta(seconds=budget)
            )
        except TimeoutError:
            rec.handed_back_after_seconds = budget
            rec.history.append(
                StateChange("running", workflow.now().isoformat(), f"handed back after {budget:g}s")
            )
            rec.message = (
                f"Running: {rec.description}. It's still going after {budget:g}s, "
                "so it continues in the background; check on it with check_call."
            )
        return self._response(rec)

    # ------------------------------------------------------------- dispatch over Nexus

    def _start(self, rec: CallRecord, entry: RegistryEntry, why: str = "") -> None:
        self._set(rec, "running", f"Running: {rec.description}.{f' {why}' if why else ''}")
        self._dispatching += 1
        asyncio.create_task(self._run_tool(rec, entry))

    async def _run_tool(self, rec: CallRecord, entry: RegistryEntry) -> None:
        try:
            rec.result = await self._call(entry, rec.arguments, rec)
            self._set(rec, "succeeded", f"Succeeded: {rec.description}.")
        except NexusOperationError as err:
            rec.failure = failure_message(err)
            self._set(rec, "failed", f"Failed: {rec.description}. {rec.failure}")
        finally:
            self._dispatching -= 1

    # region snippet:dispatch-by-name
    async def _call(self, entry: RegistryEntry, arguments: dict[str, Any], rec: CallRecord) -> Any:
        """Call a team's Operation by its names in the registry. No team code is imported."""
        client = workflow.create_nexus_client(service=entry.service, endpoint=entry.endpoint)
        handle = await client.start_operation(
            entry.operation,
            arguments,  # a plain dict, already checked against the contract's schema
            schedule_to_close_timeout=timedelta(seconds=entry.schedule_to_close_seconds),
            headers=self._principal_headers(rec),  # who the agent acts for
            summary=call_summary(entry.operation, arguments, rec.principal, entry.summary_template),
        )
        return await handle
        # endregion

    def _principal_headers(self, rec: CallRecord) -> dict[str, str]:
        """Nexus headers that tell the team who the agent acts for.

        Nexus authorizes Namespaces, not people. Teams trust these headers because,
        on Temporal Cloud, only the gateway's Namespace may call their Endpoints. In
        production, sign the principal (for example, a short-lived JWT).
        """
        return {
            "x-pinecrest-principal": rec.principal,
            "x-pinecrest-team": self.state.principal_team,
            "x-gateway-session": rec.session_id,
            "x-gateway-call": rec.call_id,
        }

    # ------------------------------------------------------------- gates

    def _start_gate(
        self, rec: CallRecord, entry: RegistryEntry, snapshot: RegistrySnapshot, gate_tool: str
    ) -> CallResponse:
        gate_entry = snapshot.tools[gate_tool]  # checked before the call was recorded
        properties = gate_entry.input_schema.get("properties", {})
        gate_args = {k: v for k, v in rec.arguments.items() if k in properties}
        team = snapshot.directory.teams.get(gate_entry.owner_team, gate_entry.owner_team)
        rec.gate = GateRecord(
            gate_tool, gate_args, gate_entry.endpoint, gate_entry.service, gate_entry.operation, team=team
        )
        self.state.held[rec.call_id] = HeldCall(entry, snapshot.directory)
        self._set(
            rec, "awaiting_gate", f"Held for {team} review: {rec.description}. It runs if {team} approves."
        )
        self._dispatching += 1
        asyncio.create_task(self._run_gate(rec, gate_entry, entry))
        return self._response(rec)

    async def _run_gate(self, rec: CallRecord, gate_entry: RegistryEntry, entry: RegistryEntry) -> None:
        """The gate's result arrives through the Nexus completion callback: the Workflow is the callback."""
        gate = rec.gate
        assert gate is not None
        try:
            gate.result = await self._call(gate_entry, gate.arguments, rec)
        except NexusOperationError as err:
            gate.status, gate.failure = "failed", failure_message(err)
            self._set(
                rec,
                "failed",
                f"Failed: {rec.description}. The {gate.tool} gate failed, so it didn't run: {gate.failure}",
            )
            self._dispatching -= 1
            return
        self._dispatching -= 1
        result = gate.result if isinstance(gate.result, dict) else {}
        decided_by = str(result.get("decided_by", ""))
        held = self.state.held.pop(rec.call_id)
        person = held.directory.find(decided_by)
        name = person.name if person else decided_by
        if result.get("decision") == "approved":
            gate.status = "approved"
            rec.history.append(
                StateChange("awaiting_gate", workflow.now().isoformat(), f"{gate.tool} approved by {name}")
            )
            self._start(rec, entry, f"{gate.team} approved it.")
        else:
            gate.status = "rejected"
            self._reject(rec, decided_by, name, f"{gate.team} rejected it ({name}).")

    # ------------------------------------------------------------- approvals

    # region snippet:approval-held
    @workflow.update
    def decide_approval(self, decision: ApprovalDecision) -> CallRecord:
        """A held call waits here, durably, until a person with the role decides."""
        rec = self.state.calls[decision.call_id]
        held = self.state.held.pop(rec.call_id)
        person = held.directory.find(decision.decided_by)
        name = person.name if person else decision.decided_by
        rec.approval = Approval(
            decision.decision, decision.decided_by, name, workflow.now().isoformat(), decision.reason
        )
        if decision.decision == "approved":
            self._start(rec, held.entry, f"Approved by {name}.")  # runs now, over Nexus
        else:
            self._reject(rec, decision.decided_by, name, rejected_by(name, decision.reason))
        return rec
        # endregion

    @decide_approval.validator
    def _validate_decide_approval(self, decision: ApprovalDecision) -> None:
        rec = self.state.calls.get(decision.call_id)
        if rec is None or rec.call_id not in self.state.held or rec.status != "awaiting_approval":
            done = f" It is {rec.status}." if rec is not None else ""
            raise ValueError(f"Call {decision.call_id} isn't waiting for approval.{done}")
        if decision.decision not in ("approved", "rejected"):
            raise ValueError("A decision is approved or rejected.")
        role = rec.approver_role or ""
        person = self.state.held[rec.call_id].directory.find(decision.decided_by)
        if person is None or role not in person.roles:
            team = person.team if person else "not on the roster"
            raise ValueError(f"Only {role} can approve this. Signed in as {decision.decided_by} ({team}).")

    def _reject(self, rec: CallRecord, decided_by: str, name: str, why: str) -> None:
        message = f"Rejected: {rec.description}. {why}"
        self._set(rec, "rejected", message)
        release = blocked_release_for(rec.blocks_release, rec.tool, rec.arguments)
        if release is None:
            return  # the rule declared no release to block
        fields, label = release
        block = BlockRelease(rec.tool, fields, label, decided_by, name, message)
        # Remembered here at once, and sent to the registry so every session sees it.
        self.state.blocked_here.append(
            BlockedRelease(rec.tool, fields, label, decided_by, name, message, workflow.now().isoformat())
        )
        self._dispatching += 1
        asyncio.create_task(self._send_block(block))

    async def _send_block(self, block: BlockRelease) -> None:
        try:
            registry = workflow.get_external_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
            await registry.signal(ToolRegistryWorkflow.block_release, block)
        finally:
            self._dispatching -= 1

    # ------------------------------------------------------------- queries

    def _response(self, rec: CallRecord) -> CallResponse:
        return CallResponse(
            rec.call_id,
            rec.status,
            rec.message,
            rec.result,
            rule_id=rec.rule_id,
            policy_version=rec.policy_version,
        )

    @workflow.query
    def view(self) -> SessionView:
        first = next(iter(self.state.calls.values()), None)
        return SessionView(
            session_id=workflow.info().workflow_id,
            principal=self.state.input.principal,
            principal_name=first.principal_name if first else self.state.input.principal,
            client_name=self.state.input.client_name,
            calls=list(reversed(self.state.calls.values())),
            pending=[c.call_id for c in self._pending()],
        )

    @workflow.query
    def get_call(self, call_id: str) -> CallRecord | None:
        return self.state.calls.get(call_id)


def rejected_by(name: str, reason: str) -> str:
    return f"{name} rejected it: {reason.strip().rstrip('.')}." if reason.strip() else f"{name} rejected it."


def failure_message(err: NexusOperationError) -> str:
    """Why a Nexus call failed, in the team's own words: the error's .message, never str()."""
    cause: BaseException | None = err.cause
    while cause is not None:
        message = getattr(cause, "message", None)
        if message and message != "nexus operation completed unsuccessfully":
            return str(message)
        cause = getattr(cause, "cause", None) or cause.__cause__
    return err.message
