"""The gateway's front door: an MCP server over streamable HTTP at http://localhost:8080/mcp.

MCP is only the adapter that lets Claude Code and similar clients reach the
gateway. Every tool call becomes an Update on the caller's session Workflow
(update-with-start), so all state lives in Temporal. This process holds none:
restarting it loses nothing but the open MCP connections.

Demo authentication only: a bearer token from config/principals.yaml identifies
the person. A gateway session is one person on one client; the client's name
comes from MCP's client info.

Run: uv run python -m gateway.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import mcp_types as types
import uvicorn
from dotenv import load_dotenv
from mcp.server.lowlevel import Server
from mcp.server.session import ServerSession
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types import CLIENT_INFO_META_KEY
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from gateway.connection import connect
from gateway.models import (
    REGISTRY_ID,
    CallResponse,
    PolicyDocument,
    RegistryEntry,
    RegistrySnapshot,
    SessionInput,
    ToolCallRequest,
    session_id,
)
from gateway.principals import PRINCIPALS_FILE, demo_tokens
from gateway.registry_workflow import ToolRegistryWorkflow
from gateway.session_workflow import AgentSessionWorkflow, SessionState

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERY_TIMEOUT = timedelta(seconds=1)  # every Query fails fast
UNKNOWN_TOKEN = (
    "Unknown bearer token. This demo accepts only the tokens in config/principals.yaml "
    "(run `make mcp-command` to print Noah's)."
)
log = logging.getLogger("gateway.mcp_server")

CHECK_CALL = types.Tool(
    name="check_call",
    description=(
        "Check on a tool call the gateway handed back as a call handle: whether it is still running, "
        "waiting for a person or another team, or finished, and its result. Use it when the user asks how "
        "a call is going, or after a short wait. Don't poll it in a tight loop."
    ),
    input_schema={
        "type": "object",
        "properties": {"call_id": {"type": "string", "description": "The call_id from an earlier response."}},
        "required": ["call_id"],
        "additionalProperties": False,
    },
)


@dataclass
class FrontDoorSettings:
    port: int = 8080
    sync_budget_seconds: float = 5.0
    idle_close_seconds: float = 24 * 60 * 60

    @classmethod
    def from_env(cls) -> FrontDoorSettings:
        return cls(
            port=int(os.environ.get("GATEWAY_PORT", "8080")),
            sync_budget_seconds=float(os.environ.get("SYNC_BUDGET_SECONDS", "5")),
            idle_close_seconds=float(os.environ.get("SESSION_IDLE_CLOSE_SECONDS", str(24 * 60 * 60))),
        )


async def query_twice[T](query: Callable[[], Awaitable[T]]) -> T:
    """Run a fail-fast Query, and once more if it times out.

    Each attempt still gives up after a second. The retry covers a measured quirk:
    the first Query after a Workflow continues as new takes about a second.
    """
    try:
        return await query()
    except RPCError:
        return await query()


def local_utc_offset_minutes() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 60) if offset else 0


def policy_hint(policy: PolicyDocument, entry: RegistryEntry, sync_budget_seconds: float) -> str:
    """One sentence on what policy does with this tool, appended to its description."""
    parts = []
    for rule in (r for r in policy.rules if r.tool == entry.name):
        when = " and ".join(f"{k} is {v}" for k, v in rule.when.items())
        condition = f" when {when}" if when else ""
        if rule.effect == "allow":
            parts.append(f"allowed{condition}")
        elif rule.effect == "deny":
            parts.append(f"refused{condition}")
        elif rule.effect == "require_approval":
            parts.append(f"held for a {rule.approver_role}'s approval{condition}, returning a call handle")
        elif rule.effect == "require_gate":
            parts.append(f"held until {rule.gate} approves{condition}, returning a call handle")
    if not parts:
        return f"Policy v{policy.version} refuses it."
    sentence = f"Policy v{policy.version}: {'; '.join(parts)}"
    if entry.mode_hint == "async":
        sentence += f"; a call still running after {sync_budget_seconds:g}s also returns a call handle"
    return sentence + "."


def tool_result(response: CallResponse) -> types.CallToolResult:
    """A plain sentence first (outcome, then what the call does), then IDs and result as JSON.

    Rule IDs, the policy version, and the call ID stay out of the sentence.
    """
    sentence = f"Already in progress. {response.message}" if response.note else response.message
    payload = {
        "call_id": response.call_id,
        "status": response.status,
        "rule_id": response.rule_id,
        "policy_version": response.policy_version,
        "result": response.result,
    }
    text = f"{sentence}\n\n```json\n{json.dumps(payload, default=str)}\n```"
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        is_error=response.status in ("denied", "failed", "rejected"),
    )


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


class FrontDoor:
    def __init__(self, client: Client, settings: FrontDoorSettings, tokens: dict[str, str]) -> None:
        self.client = client
        self.settings = settings
        self.tokens = tokens
        self.bus = InMemorySubscriptionBus()
        self._handshake_sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()
        self.server = Server(
            "pinecrest-agent-gateway",
            version="1.0.0",
            instructions=(
                "Pinecrest Outfitters' agent gateway. Every tool call is checked against policy; some are "
                "held for a person's approval or another team's review and come back as a call handle. "
                "Relay the gateway's sentences to the user as written."
            ),
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
            on_subscriptions_listen=ListenHandler(self.bus),
        )

    # ------------------------------------------------------------- HTTP

    def app(self) -> Any:
        """The MCP app behind a demo bearer-token check."""
        mcp_app = self.server.streamable_http_app(streamable_http_path="/mcp")

        async def with_demo_auth(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and self._principal_from_headers(scope["headers"]) is None:
                body = json.dumps({"error": UNKNOWN_TOKEN}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            await mcp_app(scope, receive, send)

        return with_demo_auth

    def _principal_from_headers(self, headers: Any) -> str | None:
        raw = dict(headers).get(b"authorization", b"").decode()
        token = raw.removeprefix("Bearer ").strip() if raw.startswith("Bearer ") else ""
        return self.tokens.get(token)

    def _principal(self, ctx: Any) -> str:
        principal = self._principal_from_headers(ctx.request.scope["headers"]) if ctx.request else None
        if principal is None:  # the HTTP check above makes this unreachable over HTTP
            raise PermissionError(UNKNOWN_TOKEN)
        return principal

    @staticmethod
    def _client_name(ctx: Any) -> str:
        params = ctx.session.client_params
        if params is not None and params.client_info is not None:
            return params.client_info.name
        info = (ctx.meta or {}).get(CLIENT_INFO_META_KEY) or {}
        return str(info.get("name", "")) if isinstance(info, dict) else ""

    # ------------------------------------------------------------- tools/list

    async def snapshot(self) -> RegistrySnapshot:
        registry = self.client.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
        return await query_twice(
            lambda: registry.query(ToolRegistryWorkflow.snapshot, rpc_timeout=QUERY_TIMEOUT)
        )

    async def list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        if ctx.protocol_version <= "2025-11-25":
            self._handshake_sessions.add(ctx.session)  # these get list_changed on their own stream
        snapshot = await self.snapshot()
        tools = [
            types.Tool(
                name=entry.name,
                description=f"{entry.description} {policy_hint(snapshot.policy, entry, self.settings.sync_budget_seconds)}",
                input_schema=entry.input_schema,
            )
            for entry in snapshot.tools.values()
        ]
        return types.ListToolsResult(tools=[*tools, CHECK_CALL])

    async def watch_registry(self, interval: float = 1.0) -> None:
        """Tell connected clients when the registry changes, so new tools appear without reconnecting."""
        seen: int | None = None
        while True:
            try:
                version = (await self.snapshot()).registry_version
                if seen is not None and version != seen:
                    await self.notify_tools_changed()
                seen = version
            except (TimeoutError, RPCError) as err:
                log.debug("registry not readable yet: %s", err)
            await asyncio.sleep(interval)

    async def notify_tools_changed(self) -> None:
        await self.bus.publish(ToolsListChanged())  # 2026-07-28 clients, via subscriptions/listen
        for session in list(self._handshake_sessions):  # older clients, on their standalone stream
            try:
                await session.send_tool_list_changed()
            except Exception as err:  # noqa: BLE001 - a closed session just misses the notice
                log.debug("list_changed not delivered: %s", err)

    # ------------------------------------------------------------- tools/call

    async def call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = self._principal(ctx)
        client_name = self._client_name(ctx)
        arguments = dict(params.arguments or {})
        try:
            if params.name == "check_call":
                return await self.check_call(principal, client_name, str(arguments.get("call_id", "")))
            return tool_result(await self.submit(principal, client_name, params.name, arguments))
        except WorkflowUpdateFailedError as err:
            reason = err.cause.message if isinstance(err.cause, ApplicationError) else str(err.cause)
            return error_result(reason)  # the gateway's refusal, verbatim
        except RPCError as err:
            return error_result(f"The gateway can't reach Temporal right now: {err.message}")

    async def submit(
        self, principal: str, client_name: str, tool: str, arguments: dict[str, Any]
    ) -> CallResponse:
        call_id = f"c-{uuid.uuid4()}"  # also the Update ID, so Temporal dedupes our own retries
        session_input = SessionInput(
            principal=principal,
            client_name=client_name,
            sync_budget_seconds=self.settings.sync_budget_seconds,
            idle_close_seconds=self.settings.idle_close_seconds,
            display_utc_offset_minutes=local_utc_offset_minutes(),
        )
        start = WithStartWorkflowOperation(
            AgentSessionWorkflow.run,
            SessionState(session_input),
            id=session_id(principal, client_name),
            task_queue="gateway",
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return await self.client.execute_update_with_start_workflow(
            AgentSessionWorkflow.submit_tool_call,
            ToolCallRequest(call_id, tool, arguments, principal),
            start_workflow_operation=start,
            id=call_id,
        )

    async def check_call(self, principal: str, client_name: str, call_id: str) -> types.CallToolResult:
        session = self.client.get_workflow_handle_for(
            AgentSessionWorkflow.run, session_id(principal, client_name)
        )
        record = await query_twice(
            lambda: session.query(AgentSessionWorkflow.get_call, call_id, rpc_timeout=QUERY_TIMEOUT)
        )
        if record is None:
            return error_result(f"There's no call {call_id} in this session.")
        message = f"Running: {record.description}." if record.status == "running" else record.message
        return tool_result(
            CallResponse(
                record.call_id,
                record.status,
                message,
                record.result,
                rule_id=record.rule_id,
                policy_version=record.policy_version,
            )
        )


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s front-door %(levelname)s %(message)s")
    settings = FrontDoorSettings.from_env()
    door = FrontDoor(await connect("platform"), settings, demo_tokens(PRINCIPALS_FILE))
    server = uvicorn.Server(
        uvicorn.Config(door.app(), host="127.0.0.1", port=settings.port, log_level="warning")
    )
    watcher = asyncio.create_task(door.watch_registry())
    log.info("Gateway front door at http://localhost:%d/mcp", settings.port)
    try:
        await server.serve()
    finally:
        watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
