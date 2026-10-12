"""The agent session: policy, sync-to-async conversion, approvals, gates, and duplicates, end to end."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from gateway.models import session_id
from release_eng.workflows import EnvironmentLedgerWorkflow, ledger_id
from tests.conftest import after_continue_as_new
from tests.gateway_stack import ABE, NOAH, QUERY_TIMEOUT, GatewayStack

STATUS = {"service": "checkout-api"}


def deploy(version: str, environment: str) -> dict[str, Any]:
    return {"service": "checkout-api", "version": version, "environment": environment}


async def deploys_started(stack: GatewayStack) -> int:
    return len([w async for w in stack.release_eng.list_workflows("WorkflowType = 'DeployWorkflow'")])


async def live_version(stack: GatewayStack, environment: str) -> str:
    ledger = stack.release_eng.get_workflow_handle_for(
        EnvironmentLedgerWorkflow.run, ledger_id("checkout-api")
    )
    state = await ledger.query(EnvironmentLedgerWorkflow.status, rpc_timeout=QUERY_TIMEOUT)
    return state.environments[environment].version


def test_a_session_is_one_person_on_one_client() -> None:
    assert session_id(NOAH, "claude-code") == session_id(NOAH, "Claude-Code ")
    assert session_id(NOAH, "claude-code") != session_id(ABE, "claude-code")
    assert session_id(NOAH, "") == session_id(NOAH, "unknown-client")
    assert session_id(NOAH, "claude-code").startswith("session-") and len(session_id(NOAH, "x")) == 20


# ---------------------------------------------------------------- allow, sync and async


async def test_a_sync_call_returns_inside_the_budget(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    started = time.monotonic()
    response = await gateway_stack.submit(client, "get_release_status", STATUS, sync_budget_seconds=2)
    elapsed = time.monotonic() - started

    assert response.status == "succeeded"
    assert response.result["environments"]["production"]["version"] == "2.2.0"
    assert elapsed < 2
    record = await gateway_stack.call(client, response.call_id)
    assert (record.rule_id, record.policy_version, record.effect) == ("status-read", 1, "allow")
    assert (record.endpoint, record.service, record.operation) == (
        "release-eng-tools",
        "ReleaseEngineering",
        "get_release_status",
    )


async def test_a_slow_call_is_handed_back_as_a_call_handle(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    started = time.monotonic()
    response = await gateway_stack.submit(
        client, "deploy", deploy("2.3.1", "staging"), sync_budget_seconds=0.5
    )
    handed_back = time.monotonic() - started

    assert response.status == "running"
    assert response.message == (
        "Running: deploy checkout-api 2.3.1 to staging. It's still going after 0.5s, "
        "so it continues in the background; check on it with check_call."
    )
    assert 0.5 <= handed_back < 1.5
    record = await gateway_stack.wait_call(client, response.call_id)
    assert record.status == "succeeded"
    assert record.result["status"] == "succeeded"
    assert record.handed_back_after_seconds == 0.5
    assert await live_version(gateway_stack, "staging") == "2.3.1"


# ---------------------------------------------------------------- approvals


async def test_a_production_deploy_is_held_until_the_checkout_lead_approves(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    response = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "production"))

    assert response.status == "awaiting_approval"
    assert response.message == (
        "Held for approval: deploy checkout-api 2.3.1 to production. Noah Proovle (Checkout lead) decides."
    )
    record = await gateway_stack.call(client, response.call_id)
    assert (record.rule_id, record.approver_role) == ("deploy-production-approval", "checkout-lead")

    await gateway_stack.decide(client, response.call_id, "approved", NOAH, "Fix verified on staging.")
    record = await gateway_stack.wait_call(client, response.call_id)
    assert record.status == "succeeded"
    assert record.approval is not None and record.approval.decided_by_name == "Noah Proovle"
    assert await live_version(gateway_stack, "production") == "2.3.1"


async def test_the_wrong_approver_is_refused_with_a_plain_sentence(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    response = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "production"))

    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.decide(client, response.call_id, "approved", ABE)
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == (
        "Only checkout-lead can approve this. Signed in as abe@pinecrest.example (security)."
    )
    assert (await gateway_stack.call(client, response.call_id)).status == "awaiting_approval"


async def test_a_rejected_release_stays_blocked_and_push_it_anyway_is_refused(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))
    await gateway_stack.decide(client, held.call_id, "rejected", NOAH, "2.3.0 has the tax rounding bug.")
    assert (await gateway_stack.call(client, held.call_id)).status == "rejected"
    before = await deploys_started(gateway_stack)

    again = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))

    assert again.status == "denied"
    assert again.message.startswith("checkout-api 2.3.0 to production was rejected by Noah Proovle at ")
    assert again.message.endswith(". A rejected release stays blocked; ship a new version instead.")
    record = await gateway_stack.call(client, again.call_id)
    assert (record.rule_id, record.policy_version) == ("blocked-release", 1)
    assert await deploys_started(gateway_stack) == before, "nothing reached Release Engineering"

    # Every other session sees the block too, through the registry.
    for _ in range(20):
        if (await gateway_stack.snapshot()).blocked_releases:
            break
        await asyncio.sleep(0.1)
    other = await gateway_stack.submit(
        gateway_stack.new_client_name(), "deploy", deploy("2.3.0", "production")
    )
    assert other.status == "denied"


# ---------------------------------------------------------------- in-flight duplicates


async def test_a_duplicate_while_running_returns_the_same_handle(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    first = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "staging"), sync_budget_seconds=0.3)
    assert first.status == "running"

    second = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "staging"), sync_budget_seconds=0.3)

    assert (second.call_id, second.note) == (first.call_id, "Already in progress")
    assert len((await gateway_stack.view(client)).calls) == 1
    await gateway_stack.wait_call(client, first.call_id)


async def test_a_duplicate_while_awaiting_approval_returns_the_same_handle(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    first = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "production"))
    second = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "production"))
    assert (second.call_id, second.status, second.note) == (
        first.call_id,
        "awaiting_approval",
        "Already in progress",
    )


async def test_a_duplicate_after_completion_runs_again(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    first = await gateway_stack.submit(client, "get_release_status", STATUS)
    second = await gateway_stack.submit(client, "get_release_status", STATUS)
    assert first.call_id != second.call_id
    assert first.status == second.status == "succeeded"
    assert second.note == ""


async def test_a_duplicate_of_a_blocked_release_is_denied(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))
    await gateway_stack.decide(client, held.call_id, "rejected", NOAH, "tax rounding bug")
    duplicate = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))
    assert duplicate.call_id != held.call_id
    assert (duplicate.status, duplicate.note) == ("denied", "")


async def test_the_session_caps_calls_in_flight(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    for patch in range(25):  # held for approval: pending, but nothing dispatched
        held = await gateway_stack.submit(client, "deploy", deploy(f"9.0.{patch}", "production"))
        assert held.status == "awaiting_approval"

    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await gateway_stack.submit(client, "deploy", deploy("9.0.99", "production"))
    assert isinstance(refused.value.cause, ApplicationError)
    assert refused.value.cause.message == (
        "This session already has 25 calls running or waiting. Wait for one to finish, then try again."
    )


# ---------------------------------------------------------------- denials


async def test_an_unknown_tool_is_denied(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    response = await gateway_stack.submit(
        gateway_stack.new_client_name(), "review_release", deploy("2.4.0", "x")
    )
    assert (response.status, response.message) == (
        "denied",
        # A tool with no summary template is described by its argument values, in key order.
        "Denied: review_release x checkout-api 2.4.0. There's no tool called review_release on the gateway.",
    )


async def test_arguments_that_break_the_contract_are_denied_with_every_violation(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    before = await deploys_started(gateway_stack)
    response = await gateway_stack.submit(
        gateway_stack.new_client_name(),
        "deploy",
        {"service": "checkout-api", "version": "2.3", "environment": "moon"},
    )
    assert response.status == "denied"
    assert response.message.startswith(
        "Denied: deploy checkout-api 2.3 to moon. The arguments don't match the tool's contract: "
    )
    assert "environment: 'moon' is not one of" in response.message
    assert "version: '2.3' does not match" in response.message
    assert await deploys_started(gateway_stack) == before


# ---------------------------------------------------------------- gates (policy v2)


async def test_under_the_mandate_production_runs_only_after_security_approves(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset(policy_version=2, security_tool=True)
    client = gateway_stack.new_client_name()
    response = await gateway_stack.submit(client, "deploy", deploy("2.4.0", "production"))

    assert response.status == "awaiting_gate"
    assert response.message == (
        "Held for Security review: deploy checkout-api 2.4.0 to production. It runs if Security approves."
    )
    await gateway_stack.wait_for_review("2.4.0")
    record = await gateway_stack.call(client, response.call_id)
    assert record.status == "awaiting_gate" and record.gate is not None and record.gate.status == "running"

    await gateway_stack.security_decides("2.4.0")
    record = await gateway_stack.wait_call(client, response.call_id)
    assert record.status == "succeeded"
    assert record.gate is not None and record.gate.status == "approved"
    assert record.gate.result is not None and record.gate.result["decided_by"] == ABE
    assert await live_version(gateway_stack, "production") == "2.4.0"


async def test_a_security_rejection_blocks_the_release(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset(policy_version=2, security_tool=True)
    client = gateway_stack.new_client_name()
    response = await gateway_stack.submit(client, "deploy", deploy("2.4.0", "production"))
    await gateway_stack.wait_for_review("2.4.0")
    await gateway_stack.security_decides("2.4.0", decision="rejected")

    record = await gateway_stack.wait_call(client, response.call_id)
    assert record.status == "rejected"
    assert (
        record.message
        == "Rejected: deploy checkout-api 2.4.0 to production. Security rejected it (Abe Roover)."
    )
    again = await gateway_stack.submit(client, "deploy", deploy("2.4.0", "production"))
    assert again.status == "denied" and "rejected by Abe Roover" in again.message


async def test_a_gate_that_isnt_registered_yet_denies_the_call(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset(policy_version=2, security_tool=False)
    response = await gateway_stack.submit(
        gateway_stack.new_client_name(), "deploy", deploy("2.4.0", "production")
    )
    assert response.status == "denied"
    assert (
        response.message == "Denied: deploy checkout-api 2.4.0 to production. "
        "Policy requires review_release first, and review_release isn't registered yet."
    )


# ---------------------------------------------------------------- lifecycle


async def test_an_idle_session_closes_and_the_next_call_starts_a_new_run(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    await gateway_stack.submit(client, "get_release_status", STATUS, idle_close_seconds=1)
    first_run = (await gateway_stack.session(client).describe()).run_id
    for _ in range(50):
        description = await gateway_stack.session(client).describe()
        if description.status is not None and description.status.name == "COMPLETED":
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the idle session never closed")

    response = await gateway_stack.submit(client, "get_release_status", STATUS, idle_close_seconds=1)
    assert response.status == "succeeded"
    assert (await gateway_stack.session(client).describe()).run_id != first_run


async def test_session_and_registry_histories_replay_deterministically(gateway_stack: GatewayStack) -> None:
    """Replaying real histories through the current code must make the same decisions."""
    from temporalio.worker import Replayer

    from gateway.models import REGISTRY_ID
    from gateway.registry_workflow import ToolRegistryWorkflow
    from gateway.session_workflow import AgentSessionWorkflow

    await gateway_stack.reset(policy_version=2, security_tool=True)
    client = gateway_stack.new_client_name()
    await gateway_stack.submit(client, "get_release_status", STATUS)
    gated = await gateway_stack.submit(client, "deploy", deploy("2.4.0", "production"))
    await gateway_stack.wait_for_review("2.4.0")
    await gateway_stack.security_decides("2.4.0")
    await gateway_stack.wait_call(client, gated.call_id)
    held = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "staging"), sync_budget_seconds=0.2)
    await gateway_stack.wait_call(client, held.call_id)

    replayer = Replayer(workflows=[AgentSessionWorkflow, ToolRegistryWorkflow])
    for workflow_id in (session_id(NOAH, client), REGISTRY_ID):
        history = await gateway_stack.platform.get_workflow_handle(workflow_id).fetch_history()
        await replayer.replay_workflow(history)


# ---------------------------------------------------------------- summaries and declared blocking


def test_the_owning_teams_summary_template_renders() -> None:
    from gateway.session_workflow import call_summary

    template = "deploy {service} {version} to {environment}"
    assert call_summary("deploy", deploy("2.3.1", "production"), NOAH, template) == (
        "deploy checkout-api 2.3.1 to production for noah"
    )


def test_the_generic_summary_is_the_fallback() -> None:
    from gateway.session_workflow import call_summary

    assert call_summary("get_release_status", STATUS, NOAH) == "get_release_status checkout-api for noah"
    # A template that doesn't fit the arguments falls back too.
    assert call_summary("deploy", STATUS, NOAH, "deploy {service} {version}") == (
        "deploy checkout-api for noah"
    )


async def test_calls_carry_the_registered_summary(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(client, "deploy", deploy("2.3.1", "production"))
    status = await gateway_stack.submit(client, "get_release_status", STATUS)

    assert (
        await gateway_stack.call(client, held.call_id)
    ).summary == "deploy checkout-api 2.3.1 to production for noah"
    assert (
        await gateway_stack.call(client, status.call_id)
    ).summary == "read the release status of checkout-api for noah"


async def test_a_rejection_under_a_rule_without_the_declaration_blocks_nothing(
    gateway_stack: GatewayStack,
) -> None:
    import dataclasses

    from tests.gateway_stack import POLICIES

    undeclared = dataclasses.replace(
        POLICIES[1],
        rules=[dataclasses.replace(r, blocks_release=None) for r in POLICIES[1].rules],
    )
    await gateway_stack.reset(policy=undeclared)
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))
    await gateway_stack.decide(client, held.call_id, "rejected", NOAH, "not today")

    again = await gateway_stack.submit(client, "deploy", deploy("2.3.0", "production"))
    assert again.status == "awaiting_approval", "nothing was blocked, so it is held for approval again"
    await asyncio.sleep(0.3)
    assert (await gateway_stack.snapshot()).blocked_releases == []


# ---------------------------------------------------------------- Continue-As-New


async def test_the_session_continues_as_new_only_once_no_nexus_operation_is_in_flight(
    gateway_stack: GatewayStack,
) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    # Session settings are fixed when the session starts, so the first call sets them.
    await gateway_stack.submit(
        client, "get_release_status", STATUS, sync_budget_seconds=0.2, continue_as_new_after=2
    )
    first_run = (await gateway_stack.session(client).describe()).run_id
    running = await gateway_stack.submit(
        client, "deploy", deploy("2.3.1", "staging"), sync_budget_seconds=0.2, continue_as_new_after=2
    )
    assert running.status == "running"
    assert (await gateway_stack.session(client).describe()).run_id == first_run, "a deploy is still in flight"

    await gateway_stack.wait_call(client, running.call_id)
    for _ in range(50):
        if (await gateway_stack.session(client).describe()).run_id != first_run:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the session never continued as new")
    view = await after_continue_as_new(lambda: gateway_stack.view(client))
    assert {c.call_id for c in view.calls} >= {running.call_id}, "finished calls are carried"


async def test_a_held_approval_is_carried_across_continue_as_new(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset()
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(
        client, "deploy", deploy("2.3.1", "production"), continue_as_new_after=2
    )
    first_run = (await gateway_stack.session(client).describe()).run_id
    await gateway_stack.submit(client, "get_release_status", STATUS, continue_as_new_after=2)
    for _ in range(50):
        if (await gateway_stack.session(client).describe()).run_id != first_run:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the session never continued as new")

    await gateway_stack.decide(client, held.call_id, "approved", NOAH, "carried over")
    record = await gateway_stack.wait_call(client, held.call_id)
    assert record.status == "succeeded"


async def test_the_registry_continues_as_new_and_keeps_its_state(gateway_stack: GatewayStack) -> None:
    await gateway_stack.reset(registry_continue_as_new_after=2)
    first_run = (await gateway_stack.registry().describe()).run_id
    await gateway_stack.publish_security()
    await gateway_stack.apply_mandate()
    for _ in range(50):
        if (await gateway_stack.registry().describe()).run_id != first_run:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the registry never continued as new")
    # The first Query after Continue-As-New takes about a second on the dev server, so
    # a 1-second Query may time out once. Ask again, as the front door does.
    for _ in range(5):
        try:
            snapshot = await gateway_stack.snapshot()
            break
        except RPCError:
            await asyncio.sleep(0.2)
    else:
        pytest.fail("the continued registry never answered")
    assert "review_release" in snapshot.tools and snapshot.policy.version == 2
    assert snapshot.registry_version == 4
