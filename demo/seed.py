"""The demo's start state and Platform's policy controls.

  uv run python -m demo.seed reset           registry, policy v1, ledger; clears sessions, deploys, the desk
  uv run python -m demo.seed apply-mandate   Platform loads policy version 2 (the Security mandate);
                                             refused until review_release is published
  uv run python -m demo.seed reload-policy   load the active policy version again from config/policy.yaml

Policy changes go through the registry's Updates, so they are recorded like any other
change. A team whose Namespace doesn't exist yet (Security, until it joins during the
demo) is left out of the registry's people and teams; refresh_directory adds it. Tools are never registered from here: each team publishes its own from its
own Namespace (release_eng/publish.py, security/publish.py), and reset runs Release
Engineering's publish Workflow in release-eng to register the start state's tools.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from console.sources import namespace_exists
from gateway.connection import connect, connect_config
from gateway.contracts import CONTRACTS_DIR, parse_registration, registration_files
from gateway.models import REGISTRY_ID
from gateway.policy import load_policies
from gateway.principals import load_directory
from gateway.registry_workflow import RegistryState, SetPolicy, ToolRegistryWorkflow
from release_eng.publish import publish
from release_eng.workflows import EnvironmentLedgerWorkflow, ledger_id, seed_ledger_state
from security.review_desk_workflow import desk_id

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = REPO_ROOT / "config" / "policy.yaml"
SERVICE = "checkout-api"


async def _terminate(client: Client, workflow_id: str) -> None:
    try:
        await client.get_workflow_handle(workflow_id).terminate("demo reset")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise


async def _terminate_running(client: Client, workflow_type: str) -> int:
    query = f"WorkflowType = '{workflow_type}' AND ExecutionStatus = 'Running'"
    ids = [w.id async for w in client.list_workflows(query)]
    for workflow_id in ids:
        await _terminate(client, workflow_id)
    return len(ids)


def team_namespace(team: str) -> str:
    try:
        return connect_config(team).get("namespace") or team
    except Exception:  # noqa: BLE001 - no profile for the team: its Namespace has its name
        return team


async def teams_not_joined(client: Client) -> set[str]:
    """Teams that publish tools but have no Namespace yet: Security, until it joins."""
    teams = {parse_registration(p.name, p.read_text())["owner_team"] for p in registration_files()}
    return {t for t in teams if not await namespace_exists(client, team_namespace(t))}


async def refresh_directory(platform: Client) -> None:
    """Give the registry every team that exists now, as when a team joins."""
    registry = platform.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
    await registry.execute_update(
        ToolRegistryWorkflow.set_directory, load_directory(without=await teams_not_joined(platform))
    )


async def reset(platform: Client, release_eng: Client, security: Client) -> None:
    """Back to the start state of the demo."""
    await _terminate_running(platform, "AgentSessionWorkflow")
    await _terminate_running(release_eng, "DeployWorkflow")
    for client, workflow_id in [
        (platform, REGISTRY_ID),
        (release_eng, ledger_id(SERVICE)),
        (security, desk_id(SERVICE)),  # Security's handler starts a fresh desk on the next review
    ]:
        await _terminate(client, workflow_id)

    state = RegistryState(
        tools={},
        policy=load_policies(POLICY_FILE)[1],
        policy_history=[],
        blocked_releases=[],
        directory=load_directory(without=await teams_not_joined(platform)),
    )
    await platform.start_workflow(ToolRegistryWorkflow.run, state, id=REGISTRY_ID, task_queue="gateway")
    # Release Engineering registers its tools the way every team does: its own publish
    # Workflow, in its own Namespace, through platform-registry-release-eng.
    await publish(release_eng, CONTRACTS_DIR / "release_engineering.registration.yaml", "setup")

    await release_eng.start_workflow(
        EnvironmentLedgerWorkflow.run,
        seed_ledger_state(datetime.now(UTC)),
        id=ledger_id(SERVICE),
        task_queue="release-eng",
    )


async def apply_mandate(platform: Client) -> int:
    """Scenario control: Platform loads policy version 2. Returns the new registry version."""
    registry = platform.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
    return await registry.execute_update(
        ToolRegistryWorkflow.set_policy,
        SetPolicy(
            load_policies(POLICY_FILE)[2],
            "scenario control (Platform)",
            "Security mandate: no production deploy without Security's review",
        ),
    )


async def reload_policy(platform: Client) -> int:
    """Load the active policy version again from config/policy.yaml, after editing its rules."""
    registry = platform.get_workflow_handle_for(ToolRegistryWorkflow.run, REGISTRY_ID)
    snapshot = await registry.query(ToolRegistryWorkflow.snapshot, rpc_timeout=timedelta(seconds=1))
    version = snapshot.policy.version
    await registry.execute_update(
        ToolRegistryWorkflow.set_policy,
        SetPolicy(
            load_policies(POLICY_FILE)[version],
            "Platform",
            f"Reloaded policy v{version} from config/policy.yaml",
        ),
    )
    return version


async def main(command: str, *args: str) -> None:
    load_dotenv(REPO_ROOT / ".env")
    platform = await connect("platform")
    if command == "reset":
        await reset(platform, await connect("release-eng"), await connect("security"))
        print("Reset: policy v1, Release Engineering's tools registered, ledger seeded, no sessions.")
    elif command == "apply-mandate":
        try:
            version = await apply_mandate(platform)
        except WorkflowUpdateFailedError as err:
            reason = err.cause.message if isinstance(err.cause, ApplicationError) else str(err.cause)
            raise SystemExit(reason) from None
        print(f"Policy v2 is active (registry v{version}): production deploys need Security's review.")
    elif command == "reload-policy":
        try:
            version = await reload_policy(platform)
        except WorkflowUpdateFailedError as err:
            reason = err.cause.message if isinstance(err.cause, ApplicationError) else str(err.cause)
            raise SystemExit(reason) from None
        print(f"Reloaded policy v{version} from config/policy.yaml.")
    else:
        raise SystemExit("usage: python -m demo.seed [reset|apply-mandate|reload-policy]")


if __name__ == "__main__":
    asyncio.run(main(*(sys.argv[1:] or ["reset"])))
