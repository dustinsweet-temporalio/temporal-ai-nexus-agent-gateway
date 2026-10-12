"""The gateway's data: registry entries, policy, people, and tool call records.

Plain dataclasses, shared by the registry and session Workflows, the policy
evaluator, and (later) the front door and console. Times are RFC 3339 text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

Effect = Literal["allow", "deny", "require_approval", "require_gate"]
CallStatus = Literal[
    "denied", "running", "awaiting_approval", "awaiting_gate", "succeeded", "failed", "rejected"
]
PENDING: tuple[str, ...] = ("running", "awaiting_approval", "awaiting_gate")

REGISTRY_ID = "tool-registry"


def session_id(principal: str, client_name: str) -> str:
    """One gateway session is one person using one client."""
    client = (client_name or "").strip().lower() or "unknown-client"
    digest = hashlib.sha256(f"{principal}|{client}".encode()).hexdigest()
    return f"session-{digest[:12]}"


# ---------------------------------------------------------------- people


@dataclass
class Person:
    email: str
    name: str
    title: str
    team: str
    roles: list[str] = field(default_factory=list)


@dataclass
class Directory:
    """People and teams from config/principals.yaml. Never holds tokens."""

    people: list[Person]
    teams: dict[str, str]

    def find(self, email: str) -> Person | None:
        return next((p for p in self.people if p.email == email), None)


# ---------------------------------------------------------------- registry


@dataclass
class RegistryEntry:
    name: str
    owner_team: str
    description: str
    input_schema: dict[str, Any]
    endpoint: str
    service: str
    operation: str
    mode_hint: str  # sync | async
    schedule_to_close_seconds: int
    contract: str
    registration: str
    contract_sha256: str
    # The owning team's one-line description of a call, e.g. "deploy {service} {version} to {environment}".
    summary_template: str = ""
    registered_at: str = ""
    registered_by: str = ""  # the owning team, as the registration Endpoint identified it
    registered_principal: str = ""  # who published, as the team's publish Workflow said
    registered_through: str = ""  # the registration Endpoint, platform-registry-<team>


@dataclass
class BlockSpec:
    """Declared on a rule that can reject a call: a rejection blocks the release these fields identify."""

    fields: list[str]
    label: str  # how messages name the release, e.g. "{service} {version} to {environment}"


@dataclass
class PolicyRule:
    id: str
    tool: str
    effect: Effect
    when: dict[str, str] = field(default_factory=dict)
    approver_role: str | None = None
    gate: str | None = None
    blocks_release: BlockSpec | None = None


@dataclass
class PolicyDocument:
    version: int
    default: Effect
    rules: list[PolicyRule]


@dataclass
class PolicyChange:
    version: int
    changed_at: str
    changed_by: str
    summary: str


@dataclass
class BlockedRelease:
    tool: str
    fields: dict[str, str]  # the argument values that identify the release, as the rule declared them
    label: str  # e.g. "checkout-api 2.3.0 to production"
    blocked_by: str  # email
    blocked_by_name: str
    reason: str
    at: str


@dataclass
class RegistrySnapshot:
    tools: dict[str, RegistryEntry]
    policy: PolicyDocument
    policy_history: list[PolicyChange]
    blocked_releases: list[BlockedRelease]
    directory: Directory
    registry_version: int


# ---------------------------------------------------------------- tool calls


@dataclass
class SessionInput:
    principal: str
    client_name: str
    # How long a call may run before it is handed back as a call handle.
    sync_budget_seconds: float = 5.0
    # A session with no pending calls closes after this long without a call.
    idle_close_seconds: float = 24 * 60 * 60
    # The front door's local UTC offset, so times in messages read as local time.
    display_utc_offset_minutes: int = 0


@dataclass
class ToolCallRequest:
    call_id: str
    tool: str
    arguments: dict[str, Any]
    principal: str


@dataclass
class StateChange:
    status: str
    at: str
    note: str = ""


@dataclass
class Approval:
    decision: Literal["approved", "rejected"]
    decided_by: str
    decided_by_name: str
    decided_at: str
    reason: str = ""


@dataclass
class GateRecord:
    tool: str
    arguments: dict[str, Any]
    endpoint: str
    service: str
    operation: str
    status: str = "running"  # running | approved | rejected | failed
    team: str = ""  # the gate's owning team, as people say it ("Security")
    result: dict[str, Any] | None = None
    failure: str | None = None


@dataclass
class CallRecord:
    call_id: str
    tool: str
    arguments: dict[str, Any]
    summary: str  # for the audit and the Temporal UI: "deploy checkout-api 2.3.1 to production for noah"
    description: str  # what the call does, for reply sentences: "deploy checkout-api 2.3.1 to production"
    principal: str
    principal_name: str
    client_name: str
    session_id: str
    status: CallStatus
    policy_version: int | None = None
    rule_id: str | None = None
    effect: str | None = None
    message: str = ""  # the plain sentence the agent sees: outcome first, no IDs
    history: list[StateChange] = field(default_factory=list)
    approver_role: str | None = None
    blocks_release: BlockSpec | None = None  # from the rule that decided this call
    approval: Approval | None = None
    gate: GateRecord | None = None
    endpoint: str | None = None
    service: str | None = None
    operation: str | None = None
    result: Any = None
    failure: str | None = None
    handed_back_after_seconds: float | None = None
    submitted_at: str = ""
    finished_at: str | None = None


@dataclass
class CallResponse:
    call_id: str
    status: str
    message: str
    result: Any = None
    note: str = ""  # "Already in progress" for an in-flight duplicate
    rule_id: str | None = None  # for the JSON block and the audit, never the sentence
    policy_version: int | None = None


@dataclass
class ApprovalDecision:
    call_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str
    reason: str = ""


@dataclass
class SessionView:
    session_id: str
    principal: str
    principal_name: str
    client_name: str
    calls: list[CallRecord]  # newest first
    pending: list[str]  # call IDs that are running or waiting
