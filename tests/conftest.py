"""Shared test fixtures: one local Temporal dev server for the whole test session.

The dev server is the Temporal CLI's, started by the SDK's test environment with
the dynamic-config flags that Update-backed Nexus Operations need. It runs on a
free port with its UI off, so it never collides with a dev server you already
have running. No test calls a model or any network service beyond localhost
(the first run downloads the pinned CLI if it isn't cached).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio.api.nexus.v1 import EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import CreateNexusEndpointRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

TEMPORAL_CLI_VERSION = "v1.9.1"
NAMESPACES = ["platform", "release-eng", "security"]
DEV_SERVER_ARGS = [
    "--dynamic-config-value", "history.enableUpdateCallbacks=true",
    "--dynamic-config-value", "history.enableCHASMSignalBacklinks=true",
    "--dynamic-config-value", "history.enableSignalWithStartFromWorkflow=true",
    *[arg for ns in NAMESPACES for arg in ("--namespace", ns)],
]  # fmt: skip


@pytest.fixture(scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        dev_server_download_version=TEMPORAL_CLI_VERSION,
        dev_server_extra_args=DEV_SERVER_ARGS,
    )
    async with env:
        yield env


async def namespace_client(env: WorkflowEnvironment, namespace: str) -> Client:
    return await env.connect_client(namespace=namespace)


async def create_endpoint(env: WorkflowEnvironment, name: str, namespace: str, task_queue: str) -> None:
    target = EndpointTarget(worker=EndpointTarget.Worker(namespace=namespace, task_queue=task_queue))
    await env.client.operator_service.create_nexus_endpoint(
        CreateNexusEndpointRequest(spec=EndpointSpec(name=name, target=target))
    )


async def ensure_endpoint(env: WorkflowEnvironment, name: str, namespace: str, task_queue: str) -> None:
    try:
        await create_endpoint(env, name, namespace, task_queue)
    except RPCError as err:
        if err.status != RPCStatusCode.ALREADY_EXISTS:
            raise


@pytest.fixture(scope="session")
async def gateway_stack(
    temporal_env: WorkflowEnvironment, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Any]:
    """Gateway, Release Engineering, and Security Workers on their real queues and Endpoints."""
    from gateway.worker import build_worker as build_gateway
    from release_eng.cd_backend import CDBackend
    from release_eng.worker import build_worker as build_release_eng
    from security.reviewer import MockReviewer
    from security.worker import build_worker as build_security
    from tests.gateway_stack import GatewayStack

    saved = {k: os.environ.get(k) for k in ("DEPLOY_SECONDS_STAGING", "DEPLOY_SECONDS_PRODUCTION")}
    os.environ["DEPLOY_SECONDS_STAGING"] = "1.5"
    os.environ["DEPLOY_SECONDS_PRODUCTION"] = "1.5"
    try:
        await ensure_endpoint(temporal_env, "release-eng-tools", "release-eng", "release-eng")
        await ensure_endpoint(temporal_env, "security-tools", "security", "security")
        for team in ("release-eng", "security"):
            await ensure_endpoint(temporal_env, f"platform-registry-{team}", "platform", "gateway")
        platform = await namespace_client(temporal_env, "platform")
        release_eng = await namespace_client(temporal_env, "release-eng")
        security = await namespace_client(temporal_env, "security")
        backend = CDBackend(tmp_path_factory.mktemp("cd") / "cd_backend.sqlite")
        async with (
            build_gateway(platform),
            build_release_eng(release_eng, backend),
            build_security(security, MockReviewer(step_seconds=0.2)),
        ):
            yield GatewayStack(platform, release_eng, security)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def after_continue_as_new(query: Any, attempts: int = 5) -> Any:
    """Run a Query against a Workflow that just continued as new.

    The first Query on the new run takes about a second on the dev server, so a
    1-second Query can time out once. Ask again, briefly.
    """
    import asyncio

    for attempt in range(attempts):
        try:
            return await query()
        except RPCError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.2)
