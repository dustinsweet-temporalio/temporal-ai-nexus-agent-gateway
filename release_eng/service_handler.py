"""Release Engineering's Nexus Service: the handler behind the release-eng-tools Endpoint.

Both Operations use the Temporal Operation Handler (`@nexus.temporal_operation`,
pre-release). `get_release_status` answers synchronously from a Query, well inside
the 10-second Nexus request deadline. `deploy` is asynchronous: it starts a
Deploy Workflow, and the Operation completes when that Workflow does, delivered
to the caller through the Nexus completion callback.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import nexusrpc
from nexusrpc import HandlerError, HandlerErrorType, OperationError, OperationErrorState
from temporalio import nexus
from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode

from release_eng.generated import (
    Candidate,
    DeployInput,
    Deployment,
    DeployOutput,
    Environments,
    ReleaseEngineering,
    ReleaseStatusInput,
    ReleaseStatusOutput,
)
from release_eng.workflows import (
    DeployedVersion,
    DeployRequest,
    DeployWorkflow,
    EnvironmentLedgerWorkflow,
    LedgerState,
    ledger_id,
)

# Every Query fails fast if no Worker serves the ledger, instead of blocking the caller.
QUERY_TIMEOUT = timedelta(seconds=1)


def requesting_principal(ctx: nexus.TemporalStartOperationContext) -> str:
    """Who the calling agent acts for, from the gateway's Nexus headers.

    Trust assumption: these headers are set by the agent gateway, and Release
    Engineering trusts them because on Temporal Cloud only the gateway's Namespace
    is allowed to call this Endpoint. Nexus authorizes Namespaces, not people, and
    the local dev server has no Endpoint allowlists at all. In production, sign the
    principal (for example, a short-lived JWT) and verify it here.
    """
    principal = ctx.headers.get("x-pinecrest-principal")
    if not principal:
        raise HandlerError("The call has no x-pinecrest-principal header.", type=HandlerErrorType.BAD_REQUEST)
    nexus.logger.info(
        "%s for %s (team %s) via %s, call %s",
        ctx.operation,
        principal,
        ctx.headers.get("x-pinecrest-team", "?"),
        ctx.headers.get("x-gateway-session", "?"),
        ctx.headers.get("x-gateway-call", "?"),
    )
    return principal


async def read_ledger(client: Client, service: str) -> LedgerState:
    ledger: WorkflowHandle = client.get_workflow_handle_for(EnvironmentLedgerWorkflow.run, ledger_id(service))
    try:
        return await ledger.query(EnvironmentLedgerWorkflow.status, rpc_timeout=QUERY_TIMEOUT)
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            raise OperationError(
                f"No release ledger for {service}. Run `make reset` to set one up.",
                state=OperationErrorState.FAILED,
            ) from err
        raise


def _deployment(d: DeployedVersion) -> Deployment:
    return Deployment(
        version=d.version, deployed_at=datetime.fromisoformat(d.deployed_at), deployed_by=d.deployed_by
    )


def status_output(ledger: LedgerState) -> ReleaseStatusOutput:
    """The ledger's state in the contract's shape."""
    return ReleaseStatusOutput(
        service=ledger.service,
        environments=Environments(
            staging=_deployment(ledger.environments["staging"]),
            production=_deployment(ledger.environments["production"]),
        ),
        candidates=[Candidate(version=c.version, summary=c.summary) for c in ledger.candidates],
    )


@nexusrpc.handler.service_handler(service=ReleaseEngineering)
class ReleaseEngineeringHandler:
    def __init__(self, deploy_seconds: dict[str, float]) -> None:
        # How long a simulated deploy takes, per environment (from .env).
        self._deploy_seconds = deploy_seconds

    # region snippet:handler-sync-status
    @nexus.temporal_operation
    async def get_release_status(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: ReleaseStatusInput,
    ) -> nexus.TemporalOperationResult[ReleaseStatusOutput]:
        requesting_principal(ctx)
        ledger = await read_ledger(client.client, input.service)  # a Query: fast, no history
        return nexus.TemporalOperationResult.sync(status_output(ledger))
        # endregion

    @nexus.temporal_operation
    async def deploy(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: DeployInput,
    ) -> nexus.TemporalOperationResult[DeployOutput]:
        principal = requesting_principal(ctx)
        ledger = await read_ledger(client.client, input.service)
        ready = [c.version for c in ledger.candidates]
        if input.version not in ready:
            # A bad request: Nexus fails the call once, immediately, and does not retry it.
            raise HandlerError(
                f"{input.version} is not a build that's ready to deploy. Ready: {', '.join(ready)}.",
                type=HandlerErrorType.BAD_REQUEST,
            )
        request = DeployRequest(
            request_id=ctx.request_id,
            service=input.service,
            version=input.version,
            environment=input.environment,
            requested_by=principal,
            duration_seconds=self._deploy_seconds[input.environment],
        )
        # region snippet:deploy-idempotent-id
        # The Workflow ID comes from the Nexus request ID. Nexus retries a start with
        # the same request ID, so a retry reaches this deploy instead of starting another.
        return await client.start_workflow(
            DeployWorkflow.run,
            request,
            id=f"deploy-{ctx.request_id}",
            static_summary=f"Deploy {input.service} {input.version} to {input.environment} for {principal}",
        )
        # endregion
