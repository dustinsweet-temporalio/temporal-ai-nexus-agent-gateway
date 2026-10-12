"""Security's Nexus Service: the handler behind the security-tools Endpoint.

`review_release` uses the Temporal Operation Handler (`@nexus.temporal_operation`,
pre-release) and is backed by a Workflow Update on the review desk. The handler
makes sure the desk exists (Signal-with-Start, a synchronous side effect), starts
the Update, and returns once the desk accepts it. The Operation completes when the
Update returns, which is when a Security engineer decides, and the result reaches
the caller through the Nexus completion callback.

SDK note (temporalio 1.34.0): `start_workflow_update` takes no `wait_for_stage`
argument here, although the API docs still show one; it always waits for ACCEPTED.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import nexusrpc
from nexusrpc import HandlerError, HandlerErrorType, OperationError, OperationErrorState
from temporalio import nexus
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy

from security.generated import ReviewReleaseInput, ReviewReleaseOutput, SecurityReview
from security.review_desk_workflow import DeskConfig, DeskState, ReviewDeskWorkflow, ReviewRequest, desk_id


def requesting_principal(ctx: nexus.TemporalStartOperationContext) -> str:
    """Who the calling agent acts for, from the gateway's Nexus headers.

    Trust assumption: these headers are set by the agent gateway, and Security trusts
    them because on Temporal Cloud only the gateway's Namespace is allowed to call
    this Endpoint. Nexus authorizes Namespaces, not people, and the local dev server
    has no Endpoint allowlists at all. In production, sign the principal (for
    example, a short-lived JWT) and verify it here.
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


async def ensure_desk(client: Client, config: DeskConfig) -> str:
    """Start the service's review desk if it isn't running, and refresh its roster either way."""
    handle = await client.start_workflow(
        ReviewDeskWorkflow.run,
        DeskState(config),
        id=desk_id(config.service),
        task_queue=nexus.info().task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="configure",
        start_signal_args=[config],
        static_summary=f"Security review desk for {config.service}",
    )
    return handle.id


@contextmanager
def update_failures_keep_their_message() -> Iterator[None]:
    """Fail the Operation with the desk's own words when it refuses or fails a review.

    The SDK turns a refused or failed Update into an Operation error that only says
    "Workflow update failed". The desk's message (for example, that a version isn't
    on the review list) is what the caller needs, so it's passed through.
    """
    try:
        yield
    except OperationError as err:
        failure = err.__cause__
        if isinstance(failure, WorkflowUpdateFailedError) and failure.cause is not None:
            message = getattr(failure.cause, "message", str(failure.cause))
            raise OperationError(message, state=OperationErrorState.FAILED) from err
        raise


@nexusrpc.handler.service_handler(service=SecurityReview)
class SecurityReviewHandler:
    def __init__(self, desk_config: Callable[[str], DeskConfig]) -> None:
        self._desk_config = desk_config

    # region snippet:handler-update-backed
    @nexus.temporal_operation
    async def review_release(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: ReviewReleaseInput,
    ) -> nexus.TemporalOperationResult[ReviewReleaseOutput]:
        request = ReviewRequest(input.service, input.version, requesting_principal(ctx))
        desk = await ensure_desk(client.client, self._desk_config(input.service))  # Signal-with-Start
        # The Update backs the Operation, which completes when Security decides.
        with update_failures_keep_their_message():
            return await client.start_workflow_update(
                desk, ReviewDeskWorkflow.review_release, request, update_id=f"review-{ctx.request_id}"
            )
        # endregion
