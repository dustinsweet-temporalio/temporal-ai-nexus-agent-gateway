"""Platform's registration Service: teams register their tools with the gateway over Nexus.

No team connects to the platform Namespace. Each team runs a small publish Workflow
in its own Namespace, which calls this Service through the team's own Endpoint,
platform-registry-<team>. Every one of those Endpoints routes here (Task Queue
"gateway"), and the Operation is backed by the registry's register_tools Update, so
the team's publish Workflow and the registry's history link to each other.

Which team is calling comes from the Endpoint the request arrived on:
nexus.info().endpoint is set by the server from the Nexus task, not by the caller.
The handler never authorizes on Nexus headers or links, because a caller's Workflow
can set its own (a caller-set `nexus-link` header replaces the server's). On
Temporal Cloud, each registration Endpoint's allowlist names exactly one team
Namespace, so the server decides who can reach it. The local dev server has no
allowlists: any Namespace can call any registration Endpoint, and the check below
only ties each Endpoint to its team's tools.
"""

from __future__ import annotations

import nexusrpc
import yaml
from nexusrpc import OperationError, OperationErrorState
from temporalio import nexus
from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.service import RPCError

from gateway.contracts import ContractError, registry_entries_from_text
from gateway.generated import PlatformRegistry, RegisterToolInput, RegisterToolOutput
from gateway.models import REGISTRY_ID, RegistryEntry
from gateway.registry_workflow import RegisterTools, ToolRegistryWorkflow

REGISTRATION_ENDPOINT_PREFIX = "platform-registry-"


def refuse(message: str) -> OperationError:
    return OperationError(message, state=OperationErrorState.FAILED)


def team_of(endpoint: str) -> str:
    """The team a registration Endpoint belongs to: platform-registry-security is security's."""
    if not endpoint.startswith(REGISTRATION_ENDPOINT_PREFIX):
        raise refuse(
            f"Registrations arrive through a {REGISTRATION_ENDPOINT_PREFIX}<team> Endpoint, not {endpoint}."
        )
    return endpoint.removeprefix(REGISTRATION_ENDPOINT_PREFIX)


async def require_endpoint(client: Client, name: str) -> None:
    """The Endpoint a team names for its tools must exist before the gateway can call it."""
    try:
        listed = await client.operator_service.list_nexus_endpoints(ListNexusEndpointsRequest(name=name))
    except RPCError as err:
        raise refuse(f"Platform couldn't check that the Nexus Endpoint {name} exists: {err.message}") from err
    if not any(e.spec.name == name for e in listed.endpoints):
        raise refuse(f"There's no Nexus Endpoint called {name}. Create it, then publish again.")


@nexusrpc.handler.service_handler(service=PlatformRegistry)
class PlatformRegistryHandler:
    # region snippet:registration-by-endpoint
    @nexus.temporal_operation
    async def register_tool(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: RegisterToolInput,
    ) -> nexus.TemporalOperationResult[RegisterToolOutput]:
        through = nexus.info().endpoint  # set by the server, so a caller can't choose it
        team = team_of(through)
        try:
            entries = registry_entries_from_text(input.registration_file, input.registration, input.contract)
        except (ContractError, yaml.YAMLError, KeyError, TypeError) as err:
            raise refuse(f"Platform couldn't read {input.registration_file}: {err}") from err
        owners = sorted({e["owner_team"] for e in entries})
        if owners != [team]:
            raise refuse(
                f"{input.registration_file} registers tools for {', '.join(owners)}, but it arrived through "
                f"{through}, which only {team} uses. Publish from the {owners[0]} Namespace through "
                f"{REGISTRATION_ENDPOINT_PREFIX}{owners[0]}."
            )
        for endpoint in sorted({e["endpoint"] for e in entries}):
            await require_endpoint(client.client, endpoint)
        request = RegisterTools([RegistryEntry(**e) for e in entries], team, input.published_by, through)
        try:
            return await client.start_workflow_update(
                REGISTRY_ID,
                ToolRegistryWorkflow.register_tools,
                request,
                update_id=f"register-{ctx.request_id}",
            )
        except OperationError as err:  # the registry refused: pass on its own words
            failure = err.__cause__
            if isinstance(failure, WorkflowUpdateFailedError) and failure.cause is not None:
                raise refuse(getattr(failure.cause, "message", str(failure.cause))) from err
            raise
        # endregion
