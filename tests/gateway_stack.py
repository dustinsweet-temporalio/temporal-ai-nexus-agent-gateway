"""The whole system in one test process: gateway, Release Engineering, and Security Workers.

Each runs on its real Task Queue behind the real Endpoint names from the teams'
registration files, against the session's local dev server. Durations are short.
Each test calls `reset()` and uses its own client name, so it gets a fresh
registry, ledger, review desk, and session.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from gateway.contracts import CONTRACTS_DIR, team_registry_entries
from gateway.models import (
    PENDING,
    REGISTRY_ID,
    ApprovalDecision,
    CallRecord,
    CallResponse,
    PolicyDocument,
    RegistryEntry,
    RegistrySnapshot,
    SessionInput,
    SessionView,
    ToolCallRequest,
    session_id,
)
from gateway.policy import load_policies
from gateway.principals import load_directory
from gateway.registry_workflow import RegistryState, SetPolicy, ToolRegistryWorkflow
from gateway.session_workflow import AgentSessionWorkflow, SessionState
from release_eng.workflows import EnvironmentLedgerWorkflow, ledger_id, seed_ledger_state
from security.publish import publish
from security.review_desk_workflow import Decision, ReviewDeskWorkflow, desk_id

ROOT = Path(__file__).resolve().parent.parent
POLICIES = load_policies(ROOT / "config" / "policy.yaml")
NOAH = "noah@pinecrest.example"
ABE = "abe@pinecrest.example"
QUERY_TIMEOUT = timedelta(seconds=1)  # every Query fails fast


def entries_for(registration_file: str) -> list[RegistryEntry]:
    return [RegistryEntry(**e) for e in team_registry_entries(CONTRACTS_DIR / registration_file)]


async def terminate(client: Client, workflow_id: str) -> None:
    try:
        await client.get_workflow_handle(workflow_id).terminate("test reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise


@dataclass
class GatewayStack:
    platform: Client
    release_eng: Client
    security: Client

    async def reset(
        self,
        policy_version: int = 1,
        security_tool: bool = False,
        policy: PolicyDocument | None = None,
        registry_continue_as_new_after: int = 200,
    ) -> None:
        for client, workflow_id in [
            (self.platform, REGISTRY_ID),
            (self.release_eng, ledger_id("checkout-api")),
            (self.security, desk_id("checkout-api")),
        ]:
            await terminate(client, workflow_id)
        tools = {e.name: e for e in entries_for("release_engineering.registration.yaml")}
        if security_tool:
            tools.update({e.name: e for e in entries_for("security_review.registration.yaml")})
        state = RegistryState(
            tools=tools,
            policy=policy or POLICIES[policy_version],
            policy_history=[],
            blocked_releases=[],
            directory=load_directory(),
            registry_version=len(tools),
            continue_as_new_after=registry_continue_as_new_after,
        )
        await self.platform.start_workflow(
            ToolRegistryWorkflow.run, state, id=REGISTRY_ID, task_queue="gateway"
        )
        await self.release_eng.start_workflow(
            EnvironmentLedgerWorkflow.run,
            seed_ledger_state(datetime.now(UTC)),
            id=ledger_id("checkout-api"),
            task_queue="release-eng",
        )

    # ------------------------------------------------------------- registry

    def registry(self) -> Any:
        return self.platform.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)

    async def snapshot(self) -> RegistrySnapshot:
        return await self.registry().query(ToolRegistryWorkflow.snapshot, rpc_timeout=QUERY_TIMEOUT)

    async def apply_mandate(self) -> int:
        return await self.registry().execute_update(
            ToolRegistryWorkflow.set_policy,
            SetPolicy(
                POLICIES[2],
                "scenario control (Platform)",
                "Security mandate: production needs Security's review",
            ),
        )

    async def publish_security(self) -> int:
        """As Security does it: its publish Workflow in security, through platform-registry-security."""
        answer = await publish(
            self.security, CONTRACTS_DIR / "security_review.registration.yaml", "scenario control (Security)"
        )
        return answer["registry_version"]

    # ------------------------------------------------------------- sessions

    @staticmethod
    def new_client_name() -> str:
        return f"test-client-{uuid.uuid4().hex[:8]}"

    async def submit(
        self,
        client_name: str,
        tool: str,
        arguments: dict[str, Any],
        principal: str = NOAH,
        call_id: str | None = None,
        sync_budget_seconds: float = 2.0,
        idle_close_seconds: float = 300,
        continue_as_new_after: int = 500,
    ) -> CallResponse:
        start = WithStartWorkflowOperation(
            AgentSessionWorkflow.run,
            SessionState(
                SessionInput(principal, client_name, sync_budget_seconds, idle_close_seconds),
                continue_as_new_after=continue_as_new_after,
            ),
            id=session_id(principal, client_name),
            task_queue="gateway",
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        call_id = call_id or f"c-{uuid.uuid4()}"
        return await self.platform.execute_update_with_start_workflow(
            AgentSessionWorkflow.submit_tool_call,
            ToolCallRequest(call_id, tool, arguments, principal),
            start_workflow_operation=start,
            id=call_id,
        )

    def session(self, client_name: str, principal: str = NOAH) -> Any:
        return self.platform.get_workflow_handle_for(
            AgentSessionWorkflow.run, session_id(principal, client_name)
        )

    async def view(self, client_name: str, principal: str = NOAH) -> SessionView:
        return await self.session(client_name, principal).query(
            AgentSessionWorkflow.view, rpc_timeout=QUERY_TIMEOUT
        )

    async def call(self, client_name: str, call_id: str, principal: str = NOAH) -> CallRecord:
        record = await self.session(client_name, principal).query(
            AgentSessionWorkflow.get_call, call_id, rpc_timeout=QUERY_TIMEOUT
        )
        assert record is not None
        return record

    async def wait_call(
        self,
        client_name: str,
        call_id: str,
        check: Callable[[CallRecord], bool] = lambda r: r.status not in PENDING,
        timeout: float = 30,
    ) -> CallRecord:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = await self.call(client_name, call_id)
                if check(record):
                    return record
            except RPCError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"call {call_id} never reached the expected state")

    async def decide(
        self, client_name: str, call_id: str, decision: str, decided_by: str, reason: str = ""
    ) -> Any:
        return await self.session(client_name).execute_update(
            AgentSessionWorkflow.decide_approval,
            ApprovalDecision(call_id, decision, decided_by, reason),  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------- Security's desk

    async def wait_for_review(self, version: str, timeout: float = 30) -> None:
        desk = self.security.get_workflow_handle_for(ReviewDeskWorkflow.run, desk_id("checkout-api"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                view = await desk.query(ReviewDeskWorkflow.desk_view, rpc_timeout=QUERY_TIMEOUT)
                if any(r.version == version and r.status == "awaiting_decision" for r in view.reviews):
                    return
            except RPCError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"the review of {version} never reached a decision point")

    async def security_decides(self, version: str, decision: str = "approved", decided_by: str = ABE) -> Any:
        desk = self.security.get_workflow_handle_for(ReviewDeskWorkflow.run, desk_id("checkout-api"))
        return await desk.execute_update(
            ReviewDeskWorkflow.record_decision,
            Decision(version, decision, decided_by, "Reviewed findings; low risk."),  # type: ignore[arg-type]
        )
