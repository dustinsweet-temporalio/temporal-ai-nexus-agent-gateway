"""The release console's server in the test process, reading the test stack's Namespaces."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn

from console.server import Console, Settings
from console.sources import Clients, SupervisorClient
from tests.gateway_stack import GatewayStack


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class RunningConsole:
    url: str
    console: Console


@asynccontextmanager
async def running_console(
    stack: GatewayStack, supervisor: SupervisorClient | None = None
) -> AsyncIterator[RunningConsole]:
    settings = Settings(port=free_port())
    console = Console(settings, supervisor)
    reader = console.attach(Clients(stack.platform, stack.release_eng, stack.security))
    reader.read_every = 0.1
    server = uvicorn.Server(
        uvicorn.Config(
            console.app(),
            host="127.0.0.1",
            port=settings.port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    tasks = [asyncio.create_task(reader.run()), asyncio.create_task(console.publish_forever())]
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield RunningConsole(f"http://127.0.0.1:{settings.port}", console)
    finally:
        server.should_exit = True
        await serving
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
