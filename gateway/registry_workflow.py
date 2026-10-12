"""The gateway's tool registry: registered tools, the active policy, blocked releases, and people.

One long-lived Workflow, ID `tool-registry`. Nexus Endpoints don't publish their
Operations or schemas at runtime, so this registry is the gateway's stand-in for
discovery: each entry is built from the owning team's contract and registration
files, and registering a tool is a deliberate, recorded act. Teams register through
Platform's registration Service over Nexus (gateway/registry_service.py), from their
own Namespaces; no team connects to this one.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from gateway.generated import RegisterToolOutput
    from gateway.models import (
        BlockedRelease,
        Directory,
        PolicyChange,
        PolicyDocument,
        RegistryEntry,
        RegistrySnapshot,
    )
    from gateway.policy import gate_not_registered, policy_problems, unregistered_gate

# The registry continues as new after this many changes (or sooner, if the server
# suggests it), and only when no Update is in flight.
REGISTRY_CONTINUE_AS_NEW_AFTER = 200
ENTRY_FIELDS = ("name", "owner_team", "description", "endpoint", "service", "operation")


@dataclass
class RegistryState:
    tools: dict[str, RegistryEntry]
    policy: PolicyDocument
    policy_history: list[PolicyChange]
    blocked_releases: list[BlockedRelease]
    directory: Directory
    registry_version: int = 0
    changes_this_run: int = 0
    continue_as_new_after: int = REGISTRY_CONTINUE_AS_NEW_AFTER


@dataclass
class RegisterTools:
    """One team's tools, registered together through Platform's registration Service."""

    entries: list[RegistryEntry]
    team: str  # from the registration Endpoint the call arrived on, never from the caller's input
    published_by: str  # who published, as the team's publish Workflow says
    through: str  # that registration Endpoint, platform-registry-<team>


@dataclass
class SetPolicy:
    document: PolicyDocument
    changed_by: str
    summary: str


@dataclass
class BlockRelease:
    tool: str
    fields: dict[str, str]  # the argument values that identify the release, as the rule declared
    label: str
    blocked_by: str
    blocked_by_name: str
    reason: str


@workflow.defn
class ToolRegistryWorkflow:
    @workflow.init
    def __init__(self, state: RegistryState) -> None:
        self.state = state

    @workflow.run
    async def run(self, state: RegistryState) -> None:
        await workflow.wait_condition(
            lambda: (
                (
                    self.state.changes_this_run >= self.state.continue_as_new_after
                    or workflow.info().is_continue_as_new_suggested()
                )
                and workflow.all_handlers_finished()
            )
        )
        self.state.changes_this_run = 0
        workflow.continue_as_new(self.state)

    def _changed(self) -> int:
        self.state.registry_version += 1
        self.state.changes_this_run += 1
        return self.state.registry_version

    @workflow.update
    def register_tools(self, request: RegisterTools) -> RegisterToolOutput:
        """Add or replace a team's tools. Re-registering a tool records its new contract hash."""
        team_name = self.state.directory.teams.get(request.team, request.team)
        for entry in request.entries:
            entry.registered_at = workflow.now().isoformat()
            entry.registered_by = f"{team_name} team"
            entry.registered_principal, entry.registered_through = request.published_by, request.through
            self.state.tools[entry.name] = entry
        return RegisterToolOutput(
            tools=[e.name for e in request.entries], registry_version=self._changed(), team=request.team
        )

    @register_tools.validator
    def _validate_register_tools(self, request: RegisterTools) -> None:
        if not request.entries:
            raise ValueError("A registration needs at least one tool.")
        if not request.published_by:
            raise ValueError("A tool registration must say who published it.")
        for entry in request.entries:
            missing = [f for f in ENTRY_FIELDS if not getattr(entry, f)]
            if missing:
                raise ValueError(f"A tool registration needs {', '.join(missing)}.")
            if entry.input_schema.get("type") != "object":
                raise ValueError(f"The input schema of {entry.name} must describe an object.")
            if entry.mode_hint not in ("sync", "async"):
                raise ValueError(f"The mode_hint of {entry.name} must be sync or async.")
            if entry.owner_team != request.team:
                raise ValueError(
                    f"{entry.name} belongs to {entry.owner_team}, so {request.team} can't register it."
                )
            existing = self.state.tools.get(entry.name)
            if existing and existing.owner_team != request.team:
                owner = self.state.directory.teams.get(existing.owner_team, existing.owner_team)
                raise ValueError(
                    f"There's already a tool called {entry.name} on the gateway, owned by {owner}."
                )

    @workflow.update
    def set_policy(self, request: SetPolicy) -> int:
        self.state.policy = request.document
        self.state.policy_history.append(
            PolicyChange(
                request.document.version, workflow.now().isoformat(), request.changed_by, request.summary
            )
        )
        return self._changed()

    @set_policy.validator
    def _validate_set_policy(self, request: SetPolicy) -> None:
        problems = policy_problems(request.document)
        if problems:
            raise ValueError(f"Policy v{request.document.version} can't be used: {'; '.join(problems)}.")
        if not request.changed_by:
            raise ValueError("A policy change must say who made it.")
        # A gate must be on the gateway before policy can require it: publish, then mandate.
        gate = unregistered_gate(request.document, self.state.tools)
        if gate:
            raise ValueError(gate_not_registered(request.document.version, gate))

    @workflow.update
    def set_directory(self, directory: Directory) -> None:
        """Replace the people and teams, as when a new team joins. Not a tool or policy change."""
        self.state.directory = directory

    @workflow.signal
    def block_release(self, request: BlockRelease) -> None:
        """Sent by a session when a person or a gate rejects a production release.

        A Signal, because Workflows can send Signals (not Updates). Blocking the same
        release twice changes nothing.
        """
        if any(b.tool == request.tool and b.fields == request.fields for b in self.state.blocked_releases):
            return
        self.state.blocked_releases.append(
            BlockedRelease(
                tool=request.tool,
                fields=request.fields,
                label=request.label,
                blocked_by=request.blocked_by,
                blocked_by_name=request.blocked_by_name,
                reason=request.reason,
                at=workflow.now().isoformat(),
            )
        )
        self._changed()

    @workflow.query
    def snapshot(self) -> RegistrySnapshot:
        s = self.state
        return RegistrySnapshot(
            tools=s.tools,
            policy=s.policy,
            policy_history=s.policy_history,
            blocked_releases=s.blocked_releases,
            directory=s.directory,
            registry_version=s.registry_version,
        )
