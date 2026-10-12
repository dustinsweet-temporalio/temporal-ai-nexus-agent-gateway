"""The tool registry: tools, policy, blocked releases, and people, held durably."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from gateway.models import PolicyDocument
from gateway.registry_workflow import BlockRelease, RegisterTools, SetPolicy, ToolRegistryWorkflow
from tests.gateway_stack import NOAH, GatewayStack, entries_for


async def test_setup_registers_release_engineering_with_policy_v1_and_the_roster(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    snapshot = await gateway_stack.snapshot()

    assert sorted(snapshot.tools) == ["deploy", "get_release_status"]
    assert snapshot.tools["deploy"].endpoint == "release-eng-tools"
    assert snapshot.tools["deploy"].service == "ReleaseEngineering"
    assert snapshot.policy.version == 1
    assert snapshot.blocked_releases == []
    people = {p.email: p for p in snapshot.directory.people}
    assert people[NOAH].roles == ["checkout-lead"]
    assert people["abe@pinecrest.example"].team == "security"
    assert not any(hasattr(p, "demo_token") for p in snapshot.directory.people), "no tokens in Workflow state"


async def test_publishing_a_tool_adds_it_and_bumps_the_version(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    before = (await gateway_stack.snapshot()).registry_version

    version = await gateway_stack.publish_security()
    snapshot = await gateway_stack.snapshot()

    assert version == snapshot.registry_version == before + 1
    entry = snapshot.tools["review_release"]
    assert (entry.endpoint, entry.service, entry.operation) == (
        "security-tools",
        "SecurityReview",
        "review_release",
    )
    assert (entry.registered_by, entry.registered_principal, entry.registered_through) == (
        "Security team",
        "scenario control (Security)",
        "platform-registry-security",
    )
    assert entry.registered_at
    assert entry.input_schema["required"] == ["service", "version"]


async def test_re_registering_records_the_new_contract_hash(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    (entry,) = [e for e in entries_for("release_engineering.registration.yaml") if e.name == "deploy"]
    changed = dataclasses.replace(entry, contract_sha256="0" * 64)

    await gateway_stack.registry().execute_update(
        ToolRegistryWorkflow.register_tools,
        RegisterTools([changed], "release-eng", "test", "platform-registry-release-eng"),
    )
    assert (await gateway_stack.snapshot()).tools["deploy"].contract_sha256 == "0" * 64


async def test_setting_policy_records_who_and_when(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset(security_tool=True)
    await gateway_stack.apply_mandate()
    snapshot = await gateway_stack.snapshot()

    assert snapshot.policy.version == 2
    (change,) = snapshot.policy_history
    assert (change.version, change.changed_by) == (2, "scenario control (Platform)")
    assert change.changed_at


async def test_policy_cannot_require_a_gate_that_is_not_registered(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.apply_mandate()
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == (
        "Policy v2 requires review_release, which isn't registered yet. Publish it first."
    )
    assert (await gateway_stack.snapshot()).policy.version == 1

    await gateway_stack.publish_security()
    assert await gateway_stack.apply_mandate() == 4


async def test_blocking_a_release_is_recorded_once(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    fields = {"service": "checkout-api", "version": "2.3.0", "environment": "production"}
    block = BlockRelease(
        "deploy", fields, "checkout-api 2.3.0 to production", NOAH, "Noah Proovle", "tax rounding bug"
    )
    await gateway_stack.registry().signal(ToolRegistryWorkflow.block_release, block)
    await gateway_stack.registry().signal(ToolRegistryWorkflow.block_release, block)
    await asyncio.sleep(0.5)

    blocked = (await gateway_stack.snapshot()).blocked_releases
    assert [(b.tool, b.fields, b.label, b.blocked_by_name) for b in blocked] == [
        ("deploy", fields, "checkout-api 2.3.0 to production", "Noah Proovle")
    ]


async def test_invalid_changes_are_refused_with_a_plain_sentence(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    broken = PolicyDocument(version=3, default="deny", rules=[])
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.registry().execute_update(
            ToolRegistryWorkflow.set_policy, SetPolicy(broken, "", "oops")
        )
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == "A policy change must say who made it."

    (entry,) = [e for e in entries_for("release_engineering.registration.yaml") if e.name == "deploy"]
    nameless = dataclasses.replace(entry, endpoint="")
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.registry().execute_update(
            ToolRegistryWorkflow.register_tools,
            RegisterTools([nameless], "release-eng", "test", "platform-registry-release-eng"),
        )
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == "A tool registration needs endpoint."


async def test_a_team_cant_register_a_tool_name_another_team_owns(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    (entry,) = [e for e in entries_for("release_engineering.registration.yaml") if e.name == "deploy"]
    taken = dataclasses.replace(entry, owner_team="security", endpoint="security-tools")
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.registry().execute_update(
            ToolRegistryWorkflow.register_tools,
            RegisterTools([taken], "security", "test", "platform-registry-security"),
        )
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == (
        "There's already a tool called deploy on the gateway, owned by Release Engineering."
    )
    assert (await gateway_stack.snapshot()).tools["deploy"].owner_team == "release-eng"
