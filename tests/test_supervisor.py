"""The demo supervisor, as `make run` starts it: a real dev server and real processes on free ports.

These tests need the Temporal CLI on your PATH (`make setup` checks it). They call no
model: the reviewer is the mock, and deploys and review steps are short.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest

from demo.scripted_agent import Options, run
from gateway.principals import demo_tokens
from tests.console_stack import free_port

REPO_ROOT = Path(__file__).resolve().parent.parent
NOAH_TOKEN = next(t for t, e in demo_tokens().items() if e == "noah@pinecrest.example")
pytestmark = pytest.mark.skipif(shutil.which("temporal") is None, reason="needs the Temporal CLI on PATH")


@dataclass
class RunningDemo:
    env: dict[str, str]
    state_dir: Path
    console: str
    mcp: str
    output: list[str]
    proc: asyncio.subprocess.Process

    async def snapshot(self) -> dict[str, Any]:
        async with httpx2.AsyncClient(timeout=5) as http:
            return (await http.get(f"{self.console}/api/snapshot")).json()

    async def post(self, path: str, body: dict[str, Any] | None = None) -> httpx2.Response:
        async with httpx2.AsyncClient(timeout=30) as http:
            return await http.post(f"{self.console}{path}", json=body or {})

    async def command(self, *args: str) -> tuple[int, str]:
        """`python -m demo.supervisor ...`, as make reset, make failure, and make recovery run it."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "demo.supervisor", *args, cwd=REPO_ROOT, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        return proc.returncode or 0, out.decode()

    def pids(self) -> dict[str, int]:
        return json.loads((self.state_dir / "pids.json").read_text())["children"]


def demo_env(state_dir: Path, **ports: int) -> dict[str, str]:
    return {
        **os.environ,
        "PROFILE": "local",
        "DEMO_STATE_DIR": str(state_dir),
        "TEMPORAL_PORT": str(ports.get("temporal", free_port())),
        "TEMPORAL_UI_PORT": str(ports.get("ui", free_port())),
        "GATEWAY_PORT": str(ports.get("gateway", free_port())),
        "CONSOLE_PORT": str(ports.get("console", free_port())),
        "REVIEWER": "mock",
        "SYNC_BUDGET_SECONDS": "1",
        "DEPLOY_SECONDS_STAGING": "2",
        "DEPLOY_SECONDS_PRODUCTION": "2",
        "REVIEW_STEP_SECONDS": "3",
    }


@contextlib.asynccontextmanager
async def demo_running() -> AsyncIterator[RunningDemo]:
    state_dir = Path("/tmp") / f"pcg-{uuid.uuid4().hex[:8]}"  # short: Unix socket paths are limited
    env = demo_env(state_dir)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "demo.supervisor", "run", cwd=REPO_ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    output: list[str] = []

    async def read() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            output.append(line.decode().rstrip())

    reading = asyncio.create_task(read())
    demo = RunningDemo(
        env, state_dir, f"http://127.0.0.1:{env['CONSOLE_PORT']}", f"http://127.0.0.1:{env['GATEWAY_PORT']}/mcp",
        output, proc,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 120
        while not any("Press Ctrl+C" in line for line in output):
            assert proc.returncode is None, "\n".join(output)
            assert time.monotonic() < deadline, "make run never finished starting:\n" + "\n".join(output)
            await asyncio.sleep(0.2)
        yield demo
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)  # Ctrl+C
            await asyncio.wait_for(proc.wait(), timeout=60)
        await reading
        shutil.rmtree(state_dir, ignore_errors=True)


async def until(check: Any, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = await check()
        if result:
            return result
        assert time.monotonic() < deadline, "the expected state never arrived"
        await asyncio.sleep(0.2)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------- the start state (06)


def assert_start_state(snap: dict[str, Any]) -> None:
    """The start state in the demo beats: what the console shows right after make reset."""
    groups = {g["team"]: [t["name"] for t in g["tools"]] for g in snap["registry"]["groups"]}
    assert groups == {"release-eng": ["get_release_status", "deploy"]}  # Security hasn't joined
    assert "secur" not in json.dumps(snap).lower()
    assert snap["registry"]["version"] == 1  # Release Engineering's one registration, from release-eng
    assert snap["policy"]["version"] == 1 and snap["policy"]["blocked"] == []
    stages = {s["id"]: s for s in snap["pipeline"]["stages"]}
    assert list(stages) == ["candidates", "staging", "production"]
    assert stages["staging"]["deployed"]["version"] == "2.3.0"
    assert stages["staging"]["deployed"]["by"] == "Noah Proovle's agent"
    assert stages["production"]["deployed"]["version"] == "2.2.0"
    assert [(c["version"], c["summary"], c["blocked"]) for c in snap["pipeline"]["candidates"]] == [
        ("2.3.0", "Tax rounding", False),
        ("2.3.1", "Tax rounding fix", False),
        ("2.4.0", "Trail pricing", False),
    ]
    assert {w["name"]: w["status"] for w in snap["topbar"]["workers"]} == {
        "gateway": "up",
        "release-eng": "up",
    }
    assert snap["topbar"]["signed_in"]["name"] == "Noah Proovle"
    assert snap["focus"] == {"state": "now", "in_flight": [], "last_outcome": "", "stale_notes": []}
    assert (snap["audit"]["rows"], snap["calls"]["line"]) == ([], "No calls yet")
    assert snap["cost"]["text"] == "Model cost · $0.00 · 0 tokens"  # no reviewer exists yet
    assert snap["problems"] == [], "the smoke test passed, so there's no banner"


async def is_start_state(demo: RunningDemo) -> bool:
    try:
        assert_start_state(await demo.snapshot())
    except (AssertionError, httpx2.HTTPError, ValueError, KeyError):
        return False
    return True


async def change_everything(demo: RunningDemo) -> None:
    """Move the demo well away from its start state: Act 1, both scenario controls, Abe signed in."""
    options = Options(act="1", auto_decide=True, check_every_seconds=0.3)
    await asyncio.wait_for(run(demo.mcp, NOAH_TOKEN, demo.console, options, lambda _: None), timeout=120)
    assert (await demo.post("/api/scenario/publish")).status_code == 200  # Security joins first
    assert (await demo.post("/api/scenario/mandate")).status_code == 200
    assert (await demo.post("/api/signin", {"email": "abe@pinecrest.example"})).status_code == 200
    snap = await demo.snapshot()
    assert snap["policy"]["version"] == 2 and snap["policy"]["blocked"]


# ---------------------------------------------------------------- tests


async def test_make_run_plays_both_acts_with_b12_then_reset_returns_to_the_start_state() -> None:
    async with demo_running() as demo:
        assert await until(lambda: is_start_state(demo)), "make run starts in the start state"
        lines: list[str] = []
        options = Options(act="all", auto_decide=True, kill_during_review=True, check_every_seconds=0.3)
        beats = await asyncio.wait_for(
            run(demo.mcp, NOAH_TOKEN, demo.console, options, lines.append), timeout=240
        )
        assert [b.name for b in beats] == ["B2", "B3", "B3b", *[f"B{n}" for n in range(4, 15)]]
        b12 = next(b for b in beats if b.name == "B12")
        assert b12.outcome.startswith("step 2 resumed"), "\n".join(lines)
        assert "Reading the change set wasn't run again." in "\n".join(lines)

        for _ in range(2):  # every time
            code, out = await demo.command("reset")
            assert code == 0, out
            assert out.strip().endswith(
                "Reset: a fresh dev server, policy v1, Release Engineering's tools, no sessions."
            )
            assert await until(lambda: is_start_state(demo), timeout=60)
            await change_everything(demo)

        second, out = await demo.command("run")
        assert second == 1 and "The demo is already running (another make run)." in out


async def test_no_process_is_left_behind_after_kill_restart_reset_or_shutdown() -> None:
    async with demo_running() as demo:
        assert set(demo.pids()) == {"dev-server", "gateway", "release-eng", "front-door", "console"}
        code, out = await demo.command("join", "security")
        assert code == 0, out
        before = demo.pids()
        assert set(before) == {"dev-server", "gateway", "release-eng", "security", "front-door", "console"}
        assert all(alive(pid) for pid in before.values())

        code, out = await demo.command("kill", "security")
        assert code == 0 and out.startswith(
            f"Killed the security Worker (SIGKILL, pid {before['security']})."
        )
        assert not alive(before["security"]), "kill stops the Python Worker process itself"
        code, out = await demo.command("restart", "security")
        assert code == 0, out
        restarted = demo.pids()["security"]
        assert restarted != before["security"] and alive(restarted)

        code, out = await demo.command("reset")
        assert code == 0, out
        after_reset = demo.pids()
        assert "security" not in after_reset, "reset: Security hasn't joined"
        old = {**before, "security": restarted}
        assert not any(alive(pid) for pid in old.values()), "reset stops every process it started"
        assert all(alive(pid) for pid in after_reset.values())

        demo.proc.send_signal(signal.SIGINT)  # Ctrl+C
        assert await asyncio.wait_for(demo.proc.wait(), timeout=60) == 0
        assert not any(alive(pid) for pid in after_reset.values()), "Ctrl+C stops every process"
        assert not (demo.state_dir / "supervisor.sock").exists()

        code, out = await demo.command("kill", "security")
        assert code == 1
        assert (
            out.strip()
            == "The demo isn't running, so there's no Worker to stop or start. Start it with make run."
        )


async def test_a_taken_port_stops_make_run_before_anything_starts() -> None:
    state_dir = Path("/tmp") / f"pcg-{uuid.uuid4().hex[:8]}"
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        env = demo_env(state_dir, gateway=port)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "demo.supervisor", "run", cwd=REPO_ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    shutil.rmtree(state_dir, ignore_errors=True)
    assert proc.returncode == 1
    assert out.decode().strip() == (
        f"Port {port} is already in use, and GATEWAY_PORT asks for it (the MCP front door). "
        f"Stop whatever is using port {port}, or set GATEWAY_PORT to a free port in .env."
    )


async def test_an_unopenable_control_socket_stops_make_run_with_a_plain_error() -> None:
    """A DEMO_STATE_DIR too deep for a Unix socket path, even relative to the repo."""
    from demo.supervisor import SOCKET_PATH_LIMIT

    state_dir = Path("/tmp") / f"pcg-{uuid.uuid4().hex[:8]}" / ("deep-" * 24)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "demo.supervisor", "run", cwd=REPO_ROOT, env=demo_env(state_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    text = out.decode().strip()
    assert proc.returncode == 1
    assert text.startswith(
        f"The supervisor can't open its control socket at {state_dir.resolve()}/supervisor.sock: "
    )
    assert f"a Unix socket path must be shorter than {SOCKET_PATH_LIMIT} bytes" in text
    assert text.endswith("Set DEMO_STATE_DIR in .env to a shorter folder, or remove it to use .demo-state/.")
    assert not state_dir.exists(), "nothing started and nothing was written"


async def namespaces_and_endpoints(demo: RunningDemo) -> tuple[set[str], set[str]]:
    from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest
    from temporalio.api.workflowservice.v1 import ListNamespacesRequest
    from temporalio.client import Client

    client = await Client.connect(f"localhost:{demo.env['TEMPORAL_PORT']}", namespace="platform")
    namespaces = await client.workflow_service.list_namespaces(ListNamespacesRequest())
    endpoints = await client.operator_service.list_nexus_endpoints(ListNexusEndpointsRequest())
    return {n.namespace_info.name for n in namespaces.namespaces}, {e.spec.name for e in endpoints.endpoints}


async def console_text(demo: RunningDemo) -> str:
    async with httpx2.AsyncClient(timeout=5) as http:
        return (await http.get(f"{demo.console}/")).text + (
            await http.get(f"{demo.console}/api/snapshot")
        ).text


SECURITY_ENDPOINTS = {"security-tools", "platform-registry-security"}


async def test_security_doesnt_exist_until_it_joins_and_reset_removes_it_again() -> None:
    async with demo_running() as demo:
        # The start state: no Namespace, Endpoints, Worker, registry entries, or console text.
        namespaces, endpoints = await namespaces_and_endpoints(demo)
        assert "security" not in namespaces and not SECURITY_ENDPOINTS & endpoints
        assert {"platform", "release-eng"} <= namespaces and {"release-eng-tools"} <= endpoints
        assert "security" not in demo.pids()
        snap = await demo.snapshot()
        assert sorted(t["name"] for g in snap["registry"]["groups"] for t in g["tools"]) == [
            "deploy",
            "get_release_status",
        ]
        assert "secur" not in (await console_text(demo)).lower()

        # Publishing first brings Security in: Namespace, both Endpoints, Worker, then the tool.
        published = await demo.post("/api/scenario/publish")
        assert published.status_code == 200, published.text
        namespaces, endpoints = await namespaces_and_endpoints(demo)
        assert "security" in namespaces and SECURITY_ENDPOINTS <= endpoints
        assert alive(demo.pids()["security"])
        snap = await until(lambda: snapshot_with_security(demo))
        assert "review_release" in [t["name"] for g in snap["registry"]["groups"] for t in g["tools"]]

        # make reset: the Worker, both Endpoints, and the Namespace are gone again.
        security_pid = demo.pids()["security"]
        code, out = await demo.command("reset")
        assert code == 0, out
        namespaces, endpoints = await namespaces_and_endpoints(demo)
        assert "security" not in namespaces and not SECURITY_ENDPOINTS & endpoints
        assert not alive(security_pid) and "security" not in demo.pids()
        assert await until(lambda: is_start_state(demo), timeout=60)

        # "Security joins" on its own: the same three things, and Security shows in the console.
        code, out = await demo.command("join", "security")
        assert code == 0 and out.strip() == (
            "Security joined: the security Namespace, the security-tools and platform-registry-security "
            "Endpoints, and Security's Worker."
        ), out
        namespaces, endpoints = await namespaces_and_endpoints(demo)
        assert "security" in namespaces and SECURITY_ENDPOINTS <= endpoints
        snap = await until(lambda: snapshot_with_security(demo))
        assert [g["team_name"] for g in snap["registry"]["groups"]] == ["Release Engineering", "Security"]
        assert "Abe Roover" in [p["name"] for p in snap["topbar"]["people"]]
        code, out = await demo.command("join", "security")
        assert (code, out.strip()) == (0, "Security has already joined.")


async def snapshot_with_security(demo: RunningDemo) -> dict[str, Any] | None:
    snap = await demo.snapshot()
    chips = {w["name"]: w["status"] for w in snap["topbar"]["workers"]}
    return snap if chips.get("security") == "up" else None
