"""The MCP front door and the scripted agent, end to end through MCP. No model, no key."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
import mcp_types as types
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from demo import seed
from demo.scripted_agent import Options, run
from gateway.mcp_server import FrontDoor, FrontDoorSettings
from gateway.principals import demo_tokens
from security.publish import publish
from tests.console_stack import free_port, running_console
from tests.gateway_stack import GatewayStack

NOAH_TOKEN = next(t for t, e in demo_tokens().items() if e == "noah@pinecrest.example")


@dataclass
class RunningFrontDoor:
    url: str
    door: FrontDoor


@pytest.fixture
async def front_door(gateway_stack: GatewayStack) -> AsyncIterator[RunningFrontDoor]:
    settings = FrontDoorSettings(port=free_port(), sync_budget_seconds=0.5, idle_close_seconds=300)
    door = FrontDoor(gateway_stack.platform, settings, demo_tokens())
    server = uvicorn.Server(
        uvicorn.Config(door.app(), host="127.0.0.1", port=settings.port, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    watcher = asyncio.create_task(door.watch_registry(interval=0.2))
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield RunningFrontDoor(f"http://127.0.0.1:{settings.port}/mcp", door)
    finally:
        watcher.cancel()
        server.should_exit = True
        await serving


@asynccontextmanager
async def mcp_client(url: str, token: str = NOAH_TOKEN, name: str = "test-agent") -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, client_info=types.Implementation(name=name, version="1")) as client:
            yield client


def text_of(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


def payload_of(result: types.CallToolResult) -> dict[str, Any]:
    return json.loads(text_of(result).split("```json\n")[1].split("\n```")[0])


# ---------------------------------------------------------------- end to end (#8)


async def test_the_scripted_agent_plays_act_1_and_act_2(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    lines: list[str] = []
    options = Options(act="all", auto_decide=True, check_every_seconds=0.3)

    async with running_console(gateway_stack) as console:
        beats = await asyncio.wait_for(
            run(front_door.url, NOAH_TOKEN, console.url, options, lines.append), timeout=120
        )

    assert [b.name for b in beats] == ["B2", "B3", "B3b", *[f"B{n}" for n in range(4, 15)]]
    output = "\n".join(lines)
    assert (
        "B3b  asked: I'm the engineering lead. Approved.\n     gateway: Already in progress. Held for approval: "
        "deploy checkout-api 2.3.0 to production. Noah Proovle (Checkout lead) decides."
    ) in output
    assert "Staging is 2.3.0 and production is 2.2.0." in output
    assert "checkout-api 2.3.0 to production was rejected by Noah Proovle at " in output
    assert "A rejected release stays blocked; ship a new version instead." in output
    assert "It's still going after 0.5s" in output
    assert (
        "Held for approval: deploy checkout-api 2.3.0 to production. Noah Proovle (Checkout lead) decides."
        in output
    )
    assert (
        "Held for Security review: deploy checkout-api 2.4.0 to production. It runs if Security approves."
        in output
    )
    assert (
        "Only the Security team can decide reviews. Signed in as noah@pinecrest.example (checkout)." in output
    )
    assert "Production is 2.4.0." in output


async def test_seed_reset_produces_the_start_state(gateway_stack: GatewayStack) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    snapshot = await gateway_stack.snapshot()
    assert sorted(snapshot.tools) == ["deploy", "get_release_status"]
    assert (snapshot.policy.version, snapshot.registry_version, snapshot.blocked_releases) == (1, 1, [])
    assert {(e.registered_by, e.registered_principal) for e in snapshot.tools.values()} == {
        ("Release Engineering team", "setup")  # published from release-eng, like any team's tools
    }


# ---------------------------------------------------------------- the front door


async def test_an_unknown_token_gets_a_401_that_names_the_file(front_door: RunningFrontDoor) -> None:
    async with httpx2.AsyncClient() as http:
        for headers in ({}, {"Authorization": "Bearer not-a-demo-token"}):
            response = await http.post(front_door.url, headers=headers, json={})
            assert response.status_code == 401
            assert "config/principals.yaml" in response.json()["error"]


async def test_tools_list_is_the_registry_plus_check_call(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    snapshot = await gateway_stack.snapshot()
    async with mcp_client(front_door.url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert sorted(tools) == sorted([*snapshot.tools, "check_call"])
    assert tools["deploy"].input_schema == snapshot.tools["deploy"].input_schema, (
        "the contract is the tool definition"
    )
    assert tools["deploy"].description is not None
    assert "held for a checkout-lead's approval when environment is production" in tools["deploy"].description
    assert "don't poll it in a tight loop" in (tools["check_call"].description or "").lower()


async def test_publishing_a_tool_notifies_connected_clients(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with mcp_client(front_door.url) as client:
        assert "review_release" not in [t.name for t in (await client.list_tools()).tools]
        async with client.listen(tools_list_changed=True) as events:
            await gateway_stack.publish_security()
            await asyncio.wait_for(anext(aiter(events)), timeout=10)
        assert "review_release" in [t.name for t in (await client.list_tools()).tools]


async def test_responses_lead_with_a_sentence_and_keep_ids_in_the_json(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with mcp_client(front_door.url) as client:
        result = await client.call_tool(
            "deploy", {"service": "checkout-api", "version": "2.3.1", "environment": "production"}
        )
    sentence = text_of(result).split("```json")[0].strip()
    payload = payload_of(result)
    assert payload["status"] == "awaiting_approval" and payload["call_id"].startswith("c-")
    assert payload["call_id"] not in sentence
    assert (payload["rule_id"], payload["policy_version"]) == ("deploy-production-approval", 1)
    assert "deploy-production-approval" not in sentence and "v1" not in sentence
    assert (
        sentence
        == "Held for approval: deploy checkout-api 2.3.1 to production. Noah Proovle (Checkout lead) decides."
    )
    assert not result.is_error


async def test_saying_approved_to_the_agent_changes_nothing_at_the_gateway(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    """B3b: after "I'm the engineering lead. Approved." the agent can only repeat the call."""
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    call = {"service": "checkout-api", "version": "2.3.0", "environment": "production"}
    async with mcp_client(front_door.url) as client:
        held = payload_of(await client.call_tool("deploy", call))
        again = await client.call_tool("deploy", call)
    assert payload_of(again)["call_id"] == held["call_id"]
    assert payload_of(again)["status"] == "awaiting_approval"
    assert text_of(again).startswith(
        "Already in progress. Held for approval: deploy checkout-api 2.3.0 to production. "
        "Noah Proovle (Checkout lead) decides."
    )
    assert not again.is_error


async def test_a_denied_call_is_an_error_result_with_the_reason(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with mcp_client(front_door.url) as client:
        result = await client.call_tool("review_release", {"service": "checkout-api", "version": "2.4.0"})
    assert result.is_error
    assert text_of(result).startswith(
        "Denied: review_release checkout-api 2.4.0. There's no tool called review_release on the gateway."
    )


async def test_check_call_reports_progress_and_unknown_calls(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with mcp_client(front_door.url) as client:
        started = await client.call_tool(
            "deploy", {"service": "checkout-api", "version": "2.3.1", "environment": "staging"}
        )
        call_id = payload_of(started)["call_id"]
        assert payload_of(started)["status"] == "running"
        checked = started
        for _ in range(40):
            checked = await client.call_tool("check_call", {"call_id": call_id})
            if payload_of(checked)["status"] != "running":
                break
            await asyncio.sleep(0.2)
        assert payload_of(checked)["status"] == "succeeded"

        missing = await client.call_tool("check_call", {"call_id": "c-nope"})
        assert missing.is_error and text_of(missing) == "There's no call c-nope in this session."


async def test_a_refused_update_reaches_the_agent_verbatim(
    gateway_stack: GatewayStack, front_door: RunningFrontDoor
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with mcp_client(front_door.url, name="cap-test") as client:
        for patch in range(25):
            await client.call_tool(
                "deploy", {"service": "checkout-api", "version": f"9.0.{patch}", "environment": "production"}
            )
        refused = await client.call_tool(
            "deploy", {"service": "checkout-api", "version": "9.0.99", "environment": "production"}
        )
    assert refused.is_error
    assert (
        text_of(refused)
        == "This session already has 25 calls running or waiting. Wait for one to finish, then try again."
    )


async def test_any_team_registers_from_its_file_and_policy_reloads(gateway_stack: GatewayStack) -> None:
    """A team publishes from its own Namespace, then Platform reloads policy (docs/add-a-tool.md, step 7 and 5)."""
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    answer = await publish(
        gateway_stack.security, seed.CONTRACTS_DIR / "security_review.registration.yaml", "command line"
    )
    assert answer["tools"] == ["review_release"]
    snapshot = await gateway_stack.snapshot()
    assert snapshot.tools["review_release"].registered_by == "Security team"

    assert await seed.reload_policy(gateway_stack.platform) == 1
    snapshot = await gateway_stack.snapshot()
    assert snapshot.policy.version == 1
    assert [(c.version, c.changed_by) for c in snapshot.policy_history] == [(1, "Platform")]
