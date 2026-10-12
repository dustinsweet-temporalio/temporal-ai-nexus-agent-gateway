"""The release console's server against the real stack: actions are Updates, refusals are 409s."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from console.sources import NotFound, Reader, SupervisorClient, query_twice
from console.view import Observed, Source
from demo import seed
from tests.console_stack import running_console
from tests.gateway_stack import GatewayStack

PRODUCTION = {"service": "checkout-api", "version": "2.3.0", "environment": "production"}


async def snapshot_until(http: httpx2.AsyncClient, check: Any, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = (await http.get("/api/snapshot")).json()
        if snap and check(snap):
            return snap
        await asyncio.sleep(0.1)
    raise TimeoutError("the console never showed the expected state")


async def test_an_approval_is_an_update_and_a_refusal_comes_back_verbatim(
    gateway_stack: GatewayStack,
) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    client = gateway_stack.new_client_name()
    first = await gateway_stack.submit(client, "deploy", PRODUCTION)
    second = await gateway_stack.submit(client, "deploy", {**PRODUCTION, "version": "2.3.1"})

    async with running_console(gateway_stack) as console, httpx2.AsyncClient(base_url=console.url) as http:
        assert (await http.post("/api/signin", json={"email": "abe@pinecrest.example"})).status_code == 200
        wrong = await http.post(f"/api/approvals/{second.call_id}", json={"decision": "approved"})
        assert wrong.status_code == 409
        assert wrong.json()["error"] == (
            "Only checkout-lead can approve this. Signed in as abe@pinecrest.example (security)."
        )

        await http.post("/api/signin", json={"email": "noah@pinecrest.example"})
        rejected = await http.post(
            f"/api/approvals/{first.call_id}",
            json={"decision": "rejected", "reason": "2.3.0 has the tax rounding bug."},
        )
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        record = await gateway_stack.call(client, first.call_id)
        assert record.approval is not None and record.approval.decided_by == "noah@pinecrest.example"
        assert record.approval.reason == "2.3.0 has the tax rounding bug."

        again = await http.post(f"/api/approvals/{first.call_id}", json={"decision": "approved"})
        assert again.status_code == 409
        assert again.json()["error"] == f"Call {first.call_id} isn't waiting for approval. It is rejected."

        assert (
            await http.post(f"/api/approvals/{first.call_id}", json={"decision": "maybe"})
        ).status_code == 400
        missing = await http.post("/api/approvals/c-nope", json={"decision": "approved"})
        assert (missing.status_code, missing.json()["error"]) == (
            404,
            "There's no call c-nope in any running session.",
        )


def step(snapshot: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in snapshot["topbar"]["scenario"]["steps"] if s["id"] == step_id)


async def test_sign_in_and_the_scenario_controls(gateway_stack: GatewayStack) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with running_console(gateway_stack) as console, httpx2.AsyncClient(base_url=console.url) as http:
        unknown = await http.post("/api/signin", json={"email": "mallory@pinecrest.example"})
        assert (unknown.status_code, unknown.json()["error"]) == (
            404,
            "mallory@pinecrest.example isn't on the roster in config/principals.yaml.",
        )
        abe = await http.post("/api/signin", json={"email": "abe@pinecrest.example"})
        assert abe.json() == {
            "signed_in": "abe@pinecrest.example",
            "name": "Abe Roover",
            "title": "Security engineer",
        }
        await snapshot_until(http, lambda s: s["topbar"]["signed_in"]["name"] == "Abe Roover")

        # Publish before mandate: the registry refuses a policy whose gate isn't registered.
        early = await http.post("/api/scenario/mandate")
        assert (early.status_code, early.json()["error"]) == (
            409,
            "Policy v2 requires review_release, which isn't registered yet. Publish it first.",
        )
        snap = await snapshot_until(http, lambda s: step(s, "mandate")["blocked"] is not None)
        assert step(snap, "mandate")["blocked"] == early.json()["error"]

        # Without make run there's no supervisor to add a team, but this Namespace exists already.
        joined = await http.post("/api/scenario/join")
        assert (joined.status_code, joined.json()["error"]) == (409, "Security has already joined.")
        published = await http.post("/api/scenario/publish")
        assert published.status_code == 200
        await snapshot_until(http, lambda s: step(s, "publish")["done"])
        assert (await http.post("/api/scenario/publish")).status_code == 409

        mandate = await http.post("/api/scenario/mandate")
        assert mandate.status_code == 200 and mandate.json()["message"].startswith("Policy v2 is active")
        snap = await snapshot_until(http, lambda s: step(s, "mandate")["done"])
        assert step(snap, "mandate")["blocked"] is None
        assert (await http.post("/api/scenario/mandate")).status_code == 409
        assert [(r["who"], r["via"]) for r in snap["audit"]["rows"]] == [
            ("Scenario control (Platform)", ""),
            ("Security team", "scenario control (Security)"),  # the team, from its registration Endpoint
        ]
        published = snap["audit"]["rows"][1]["details"]
        assert published["Registered through"] == "platform-registry-security"


async def test_the_stream_pushes_a_change_within_a_second(gateway_stack: GatewayStack) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    async with (
        running_console(gateway_stack) as console,
        httpx2.AsyncClient(base_url=console.url, timeout=httpx2.Timeout(10.0)) as http,
    ):
        await snapshot_until(http, lambda s: s["policy"]["version"] == 1)
        async with http.stream("GET", "/api/stream") as stream:
            lines = stream.aiter_lines()
            first = json.loads((await anext(lines)).removeprefix("data: "))
            assert first["policy"]["version"] == 1
            changed = time.monotonic()
            await gateway_stack.publish_security()
            await seed.apply_mandate(gateway_stack.platform)
            async for line in lines:
                if line.startswith("data: ") and json.loads(line[6:])["policy"]["version"] == 2:
                    break
            assert time.monotonic() - changed < 0.6  # a read every 100 ms, a push within 100 ms


async def test_an_action_is_on_the_stream_before_its_response_returns(gateway_stack: GatewayStack) -> None:
    """After a successful action, the server re-reads what changed and pushes at once."""
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    client = gateway_stack.new_client_name()
    held = await gateway_stack.submit(client, "deploy", PRODUCTION)
    async with (
        running_console(gateway_stack) as console,
        httpx2.AsyncClient(base_url=console.url, timeout=httpx2.Timeout(10.0)) as http,
    ):
        await snapshot_until(http, lambda s: s["focus"]["state"] == "approval")
        reader = console.console.reader
        assert reader is not None
        reader.read_every = 60  # only the action's own re-read can show the result
        await asyncio.sleep(0.3)
        async with http.stream("GET", "/api/stream") as stream:
            lines = stream.aiter_lines()
            assert json.loads((await anext(lines))[6:])["focus"]["state"] == "approval"
            clicked = time.monotonic()
            answer = await http.post(f"/api/approvals/{held.call_id}", json={"decision": "rejected"})
            assert answer.status_code == 200
            async for line in lines:
                if line.startswith("data: ") and json.loads(line[6:])["focus"]["state"] == "decided":
                    break
            assert time.monotonic() - clicked < 0.5


@asynccontextmanager
async def fake_supervisor(socket_path: Path) -> AsyncIterator[list[str]]:
    """The supervisor's HTTP interface faked: it records what it was asked."""
    asked: list[str] = []
    state = {"gateway": "running", "release-eng": "running", "security": "running"}

    async def workers(request: Request) -> JSONResponse:
        return JSONResponse({"workers": [{"name": n, "state": s, "pid": 1} for n, s in state.items()]})

    async def act(request: Request) -> JSONResponse:
        name, action = request.path_params["name"], request.path_params["action"]
        asked.append(f"{action} {name}")
        state[name] = "stopped" if action == "kill" else "running"
        return JSONResponse({"name": name, "state": state[name]})

    app = Starlette(
        routes=[Route("/workers", workers), Route("/workers/{name}/{action}", act, methods=["POST"])]
    )
    server = uvicorn.Server(uvicorn.Config(app, uds=str(socket_path), log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield asked
    finally:
        server.should_exit = True
        await serving


async def test_kill_and_restart_go_to_the_supervisor(gateway_stack: GatewayStack, tmp_path: Path) -> None:
    await seed.reset(gateway_stack.platform, gateway_stack.release_eng, gateway_stack.security)
    socket_path = (
        Path("/tmp") / f"console-test-{uuid.uuid4().hex[:8]}.sock"
    )  # short: Unix socket paths are limited
    async with (
        fake_supervisor(socket_path) as asked,
        running_console(gateway_stack, SupervisorClient(socket_path)) as console,
        httpx2.AsyncClient(base_url=console.url) as http,
    ):
        killed = await http.post("/api/workers/security/kill")
        assert (killed.status_code, killed.json()) == (200, {"name": "security", "state": "stopped"})
        snap = await snapshot_until(
            http,
            lambda s: next(w for w in s["topbar"]["workers"] if w["name"] == "security")["status"] == "down",
        )
        assert snap["topbar"]["supervisor"] is True
        assert (await http.post("/api/workers/security/restart")).status_code == 200
        assert asked == ["kill security", "restart security"]
        assert (await http.post("/api/workers/nobody/kill")).status_code == 404

    async with (
        running_console(gateway_stack, SupervisorClient(tmp_path / "missing.sock")) as console,
        httpx2.AsyncClient(base_url=console.url) as http,
    ):
        refused = await http.post("/api/workers/security/kill")
        assert refused.status_code == 503
        assert refused.json()["error"].startswith(
            "Kill and Restart need the demo supervisor, which `make run` starts."
        )


async def test_a_query_nobody_answers_keeps_the_last_known_state(gateway_stack: GatewayStack) -> None:
    platform = gateway_stack.platform
    workflow_id = f"unserved-{uuid.uuid4().hex[:8]}"
    await platform.start_workflow("NobodyRunsThis", id=workflow_id, task_queue="nobody-polls-this")
    reader = Reader.__new__(Reader)  # only _read_into is under test
    source = Source(data={"version": 7})

    started = time.monotonic()
    await Reader._read_into(reader, source, lambda: query_twice(platform, workflow_id, "snapshot"))
    took = time.monotonic() - started

    assert source.stale and source.data == {"version": 7}, "the last known state stays, marked stale"
    assert 1.8 < took < 4, f"two 1-second attempts, then give up (took {took:.1f}s)"
    await platform.get_workflow_handle(workflow_id).terminate("test done")

    try:
        await query_twice(platform, f"never-started-{uuid.uuid4().hex[:6]}", "snapshot")
        raise AssertionError("expected NotFound")
    except NotFound:
        pass
    assert Observed().registry.data is None
