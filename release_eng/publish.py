"""Release Engineering publishes its tools to the agent gateway, from its own Namespace.

Each team owns a copy of this file. In a real company these folders would be separate repos.

PublishToolsWorkflow runs in this team's Namespace and calls Platform's registration
Service over Nexus, through this team's own Endpoint, platform-registry-release-eng.
Platform reads the team from that Endpoint, so this team can register only its own
tools. Nothing here connects to Platform's Namespace. The Operation is called by name
with a plain dict, so this team needs none of Platform's code.

Run: uv run python -m release_eng.publish contracts/release_engineering.registration.yaml [--by NAME]
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError, NexusOperationError

with workflow.unsafe.imports_passed_through():
    import yaml

TEAM_NAMESPACE = "release-eng"
TASK_QUEUE = "release-eng"
REGISTRATION_ENDPOINT = "platform-registry-release-eng"


@dataclass
class PublishTools:
    registration_file: str  # its path in this repo, such as contracts/<team>.registration.yaml
    registration: str  # the registration file's contents
    contract: str  # the contents of the contract it names
    published_by: str  # who published, for the gateway's audit


@workflow.defn
class PublishToolsWorkflow:
    @workflow.run
    async def run(self, request: PublishTools) -> dict[str, Any]:
        registry = workflow.create_nexus_client(service="PlatformRegistry", endpoint=REGISTRATION_ENDPOINT)
        try:
            return await registry.execute_operation(
                "register_tool",
                asdict(request),
                schedule_to_close_timeout=timedelta(minutes=1),
                summary=f"publish {request.registration_file}",
            )
        except NexusOperationError as err:  # Platform refused: keep its words
            raise ApplicationError(refusal(err), non_retryable=True) from err


def refusal(err: BaseException) -> str:
    """Platform's own message, from under the Nexus error that wraps it."""
    cause: BaseException | None = err
    while cause is not None:
        message = getattr(cause, "message", "")
        if message and "nexus operation completed unsuccessfully" not in message:
            return message
        cause = cause.__cause__ or getattr(cause, "cause", None)
    return str(err)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def publish_request(registration_file: Path, published_by: str) -> PublishTools:
    path = registration_file.resolve()
    text = path.read_bytes().decode()
    contract = path.parent / yaml.safe_load(text)["contract"]
    return PublishTools(
        str(path.relative_to(repo_root())), text, contract.read_bytes().decode(), published_by
    )


async def publish(client: Client, registration_file: Path, published_by: str) -> dict[str, Any]:
    """Run PublishToolsWorkflow in this team's Namespace and return Platform's answer."""
    return await client.execute_workflow(
        PublishToolsWorkflow.run,
        publish_request(registration_file, published_by),
        id=f"publish-{registration_file.stem.split('.')[0]}-{uuid.uuid4().hex[:6]}",
        task_queue=TASK_QUEUE,
    )


async def main(args: list[str]) -> int:
    from dotenv import load_dotenv

    from release_eng.connection import connect

    load_dotenv(repo_root() / ".env")
    if not args or args[0].startswith("-"):
        print(
            "usage: uv run python -m release_eng.publish contracts/release_engineering.registration.yaml [--by NAME]"
        )
        return 2
    published_by = args[args.index("--by") + 1] if "--by" in args else "command line"
    try:
        answer = await publish(await connect(TEAM_NAMESPACE), Path(args[0]), published_by)
    except WorkflowFailureError as err:
        print(f"Platform refused the registration: {refusal(err)}")
        return 1
    print(
        f"Registered {', '.join(answer['tools'])} with the gateway (registry v{answer['registry_version']}), "
        f"through {REGISTRATION_ENDPOINT}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
