"""Teams register tools through Platform's registration Service, from their own Namespaces.

Platform decides which team is calling from the Endpoint the call arrived on
(nexus.info().endpoint, set by the server), never from headers or links, which a
caller can forge. These tests run real Workflows in the team Namespaces against the
local dev server, which has no Endpoint allowlists.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker

from gateway.contracts import CONTRACTS_DIR
from release_eng.publish import publish as release_eng_publish
from security.publish import publish_request, refusal
from tests.forging_publisher import ForgingPublisher
from tests.gateway_stack import GatewayStack

ROOT = Path(__file__).resolve().parent.parent
SECURITY_FILE = CONTRACTS_DIR / "security_review.registration.yaml"
RELEASE_ENG_FILE = CONTRACTS_DIR / "release_engineering.registration.yaml"
FORGED_LINK = (
    "<temporal:///namespaces/release-eng/workflows/forged/run/forged/history"
    "?referenceType=EventReference&eventID=1&eventType=NexusOperationScheduled>; "
    'type="temporal.api.common.v1.Link.WorkflowEvent"'
)
FORGED_HEADERS = {
    "nexus-link": FORGED_LINK,
    "x-pinecrest-team": "release-eng",
    "temporal-caller-namespace": "release-eng",
}


async def forge(client: Client, endpoint: str, request: dict[str, Any], headers: dict[str, str]) -> Any:
    async with Worker(client, task_queue="forging-test", workflows=[ForgingPublisher]):
        return await client.execute_workflow(
            ForgingPublisher.run,
            args=[endpoint, request, headers],
            id=f"forging-{uuid.uuid4().hex[:6]}",
            task_queue="forging-test",
        )


async def refused(client: Client, endpoint: str, request: dict[str, Any], headers: dict[str, str]) -> str:
    with pytest.raises(WorkflowFailureError) as failed:
        await forge(client, endpoint, request, headers)
    return refusal(failed.value)


async def test_a_team_registers_from_its_own_namespace_and_the_histories_link(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    version = await gateway_stack.publish_security()
    entry = (await gateway_stack.snapshot()).tools["review_release"]
    assert (entry.registered_by, entry.registered_through) == ("Security team", "platform-registry-security")
    assert version == (await gateway_stack.snapshot()).registry_version

    # The publish Workflow ran in security, and its Nexus event links to Platform's registry.
    published = []
    for _ in range(50):  # visibility catches up within moments
        published = [
            w async for w in gateway_stack.security.list_workflows("WorkflowType = 'PublishToolsWorkflow'")
        ]
        if published:
            break
        await asyncio.sleep(0.1)
    published = published[:1]
    assert published, "the publish Workflow is listed in the security Namespace"
    (published,) = published
    history = await gateway_stack.security.get_workflow_handle(published.id).fetch_history()
    started = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED]
    completed = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED]
    assert started or completed
    links = [link.workflow_event for e in started + completed for link in e.links]
    assert any(link.namespace == "platform" and link.workflow_id == "tool-registry" for link in links), links


async def test_the_start_state_is_registered_from_release_eng(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    answer = await release_eng_publish(gateway_stack.release_eng, RELEASE_ENG_FILE, "setup")
    assert sorted(answer["tools"]) == ["deploy", "get_release_status"]
    entry = (await gateway_stack.snapshot()).tools["deploy"]
    assert (entry.registered_by, entry.registered_principal) == ("Release Engineering team", "setup")


async def test_a_registration_through_another_teams_endpoint_is_refused(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    request = asdict(publish_request(SECURITY_FILE, "test"))
    message = await refused(gateway_stack.security, "platform-registry-release-eng", request, {})
    assert message == (
        "contracts/security_review.registration.yaml registers tools for security, but it arrived through "
        "platform-registry-release-eng, which only release-eng uses. Publish from the security Namespace "
        "through platform-registry-security."
    )
    assert "review_release" not in (await gateway_stack.snapshot()).tools


async def test_the_endpoint_decides_and_forged_links_and_headers_change_nothing(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    # A genuine release-eng caller, whose real links and forged headers both say release-eng,
    # sending Release Engineering's tools through Security's Endpoint: refused.
    ours = asdict(publish_request(RELEASE_ENG_FILE, "test"))
    message = await refused(gateway_stack.release_eng, "platform-registry-security", ours, FORGED_HEADERS)
    assert "but it arrived through platform-registry-security, which only security uses" in message

    # A security caller forging links and headers that name release-eng, through its own Endpoint:
    # registered as Security, because the Endpoint is security's.
    security = asdict(publish_request(SECURITY_FILE, "test"))
    answer = await forge(gateway_stack.security, "platform-registry-security", security, FORGED_HEADERS)
    assert answer["team"] == "security"
    assert (await gateway_stack.snapshot()).tools["review_release"].registered_by == "Security team"


async def test_a_tool_endpoint_that_doesnt_exist_is_refused(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    request = publish_request(SECURITY_FILE, "test")
    missing = replace(request, registration=request.registration.replace("security-tools", "nowhere-tools"))
    message = await refused(gateway_stack.security, "platform-registry-security", asdict(missing), {})
    assert message == "There's no Nexus Endpoint called nowhere-tools. Create it, then publish again."


async def test_a_contract_that_doesnt_parse_is_refused(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    broken = replace(publish_request(SECURITY_FILE, "test"), contract="nexusrpc: '1.0.0'\n")
    message = await refused(gateway_stack.security, "platform-registry-security", asdict(broken), {})
    assert message == (
        "Platform couldn't read contracts/security_review.registration.yaml: "
        "security_review.nexusrpc.yaml is not a NexusRPC document (no `services` section)"
    )


TEAM_FOLDERS = {"release_eng": "release-eng", "security": "security"}


def test_no_team_folder_connects_to_the_platform_namespace() -> None:
    """Team code names only its own Namespace; Platform is reached over Nexus Endpoints."""
    for folder, namespace in TEAM_FOLDERS.items():
        for path in (ROOT / folder).rglob("*.py"):
            if "generated" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            literals = {
                n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            assert "platform" not in literals, f"{path.relative_to(ROOT)} names the platform Namespace"
            imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            assert not [m for m in imports if m.split(".")[0] == "gateway"], (
                f"{path.name} imports the gateway"
            )
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                name = getattr(call.func, "id", getattr(call.func, "attr", ""))
                if (
                    name in ("connect", "connect_config")
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                ):
                    assert call.args[0].value == namespace, f"{path.name} connects to {call.args[0].value}"
