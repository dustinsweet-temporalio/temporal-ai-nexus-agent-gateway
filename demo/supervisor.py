"""The demo supervisor: starts every process, and stops, kills, and restarts them on request.

  uv run python -m demo.supervisor run              start everything (make run); Ctrl+C stops it all
  uv run python -m demo.supervisor join security    Security joins the demo (make security-joins)
  uv run python -m demo.supervisor kill security    SIGKILL a Worker (make failure)
  uv run python -m demo.supervisor restart security start it again (make recovery)
  uv run python -m demo.supervisor reset            back to the demo's start state (make reset)

On the local profile it starts the Temporal dev server (with the dynamic-config
flags that Update-backed Operations need) and the start state's Namespaces, platform
and release-eng, then the gateway and Release Engineering Workers, seeds the start
state, runs the startup smoke test, and starts the MCP front door and the release
console. Security isn't part of the start state. It joins during the demo: joining
creates its Namespace and its two Endpoints and starts its Worker, and make reset
(a fresh dev server) removes them again. Each runs as its own Python process, started directly (never
through a `uv run` wrapper), so Kill stops exactly the Worker and nothing else.

Kill sends SIGKILL at once. Stopping sends SIGTERM, then SIGKILL after 3 seconds.
Each child runs in its own process group, so Ctrl+C reaches only the supervisor,
which then stops every child itself. The supervisor records its children's PIDs in
.demo-state/pids.json and cleans up after a previous run that didn't exit cleanly.

The console and these commands reach the supervisor over HTTP on a Unix socket,
<DEMO_STATE_DIR>/supervisor.sock (see `console.sources.SupervisorClient`).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from temporalio.service import RPCError, RPCStatusCode

from console.sources import Clients, unix_socket_address, worker_status

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKERS = {"gateway": "gateway.worker", "release-eng": "release_eng.worker", "security": "security.worker"}
START_WORKERS = ("gateway", "release-eng")
JOINS_LATER = {"security": ("security", "Security")}  # team -> (its Worker, its name); Namespace = team
SERVICES = {"front-door": "gateway.mcp_server", "console": "console.server"}
SHUTDOWN_GRACE_SECONDS = 3.0
SOCKET_PATH_LIMIT = 104 if sys.platform == "darwin" else 108  # bytes, the OS's limit for a Unix socket path
DEV_SERVER_FLAGS = [
    "--dynamic-config-value", "history.enableUpdateCallbacks=true",
    "--dynamic-config-value", "history.enableCHASMSignalBacklinks=true",
    "--dynamic-config-value", "history.enableSignalWithStartFromWorkflow=true",
    *[arg for ns in ("platform", "release-eng") for arg in ("--namespace", ns)],
]  # fmt: skip


class StartupError(Exception):
    """Something kept the demo from starting; the message says what and what to do."""


# ---------------------------------------------------------------- settings and ports


@dataclass
class Settings:
    profile: str = "local"
    temporal_port: int = 7233
    temporal_ui_port: int = 8233
    gateway_port: int = 8080
    console_port: int = 8090
    state_dir: Path = REPO_ROOT / ".demo-state"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            profile=(env("PROFILE") or "local").strip(),
            temporal_port=int(env("TEMPORAL_PORT") or 7233),
            temporal_ui_port=int(env("TEMPORAL_UI_PORT") or 8233),
            gateway_port=int(env("GATEWAY_PORT") or 8080),
            console_port=int(env("CONSOLE_PORT") or 8090),
            state_dir=Path(env("DEMO_STATE_DIR") or REPO_ROOT / ".demo-state").resolve(),
        )

    @property
    def local(self) -> bool:
        return self.profile == "local"

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "supervisor.sock"

    def ports(self) -> list[tuple[int, str, str]]:
        """(port, setting, what uses it), for the ports this run needs."""
        ports = [(self.gateway_port, "GATEWAY_PORT", "the MCP front door"),
                 (self.console_port, "CONSOLE_PORT", "the release console")]  # fmt: skip
        if self.local:
            ports = [(self.temporal_port, "TEMPORAL_PORT", "the Temporal dev server"),
                     (self.temporal_ui_port, "TEMPORAL_UI_PORT", "the Temporal UI"), *ports]  # fmt: skip
        return ports


def port_taken(port: int) -> bool:
    """True if something already listens on the port, or it can't be bound."""
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.3):
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def port_conflicts(settings: Settings) -> list[str]:
    """One plain sentence per port that's already taken."""
    return [
        f"Port {port} is already in use, and {setting} asks for it ({what}). "
        f"Stop whatever is using port {port}, or set {setting} to a free port in .env."
        for port, setting, what in settings.ports()
        if port_taken(port)
    ]


# ---------------------------------------------------------------- one child process


@dataclass
class Child:
    name: str
    argv: list[str]
    log_path: Path
    marker: str  # in the command line, to recognize this child's process later
    proc: subprocess.Popen[bytes] | None = None
    log: Any = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.running and self.proc else None

    def start(self, env: dict[str, str]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            self.argv,
            cwd=REPO_ROOT,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own process group: Ctrl+C reaches only the supervisor
        )

    def kill(self) -> None:
        """SIGKILL, at once: the Worker dies mid-work, as a crashed machine would."""
        if self.running and self.proc:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
        self._close()

    async def stop(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """SIGTERM, then SIGKILL if it hasn't exited after the grace period."""
        if self.running and self.proc:
            self.proc.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + grace
            while self.proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait()
        self._close()

    def _close(self) -> None:
        if self.log:
            self.log.close()
            self.log = None

    def tail(self, lines: int = 12) -> str:
        with contextlib.suppress(OSError):
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        return ""


def process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


# ---------------------------------------------------------------- the supervisor


@dataclass
class Supervisor:
    settings: Settings
    children: dict[str, Child] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    smoke: dict[str, Any] = field(default_factory=dict)
    joined: set[str] = field(default_factory=set)  # teams that joined during the demo
    out: Any = print

    def __post_init__(self) -> None:
        logs = self.settings.state_dir / "logs"
        python = sys.executable
        if self.settings.local:
            temporal = shutil.which("temporal") or "temporal"
            self.children["dev-server"] = Child(
                "dev-server",
                [temporal, "server", "start-dev", "--port", str(self.settings.temporal_port),
                 "--ui-port", str(self.settings.temporal_ui_port), "--log-level", "warn", *DEV_SERVER_FLAGS],
                logs / "dev-server.log",
                "server start-dev",
            )  # fmt: skip
        for name, module in {**WORKERS, **SERVICES}.items():
            self.children[name] = Child(name, [python, "-m", module], logs / f"{name}.log", f"-m {module}")

    @property
    def env(self) -> dict[str, str]:
        return {**os.environ, "DEMO_STATE_DIR": str(self.settings.state_dir), "PYTHONUNBUFFERED": "1"}

    # ------------------------------------------------------------- bookkeeping

    @property
    def pids_file(self) -> Path:
        return self.settings.state_dir / "pids.json"

    def record_pids(self) -> None:
        pids = {name: c.pid for name, c in self.children.items() if c.pid}
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.pids_file.write_text(json.dumps({"supervisor": os.getpid(), "children": pids}))

    def clean_up_after_a_previous_run(self) -> list[str]:
        """Stop any child a previous supervisor left behind (it was killed before it could)."""
        stopped: list[str] = []
        with contextlib.suppress(OSError, ValueError):
            recorded = json.loads(self.pids_file.read_text()).get("children", {})
            for name, pid in recorded.items():
                child = self.children.get(name)
                if child and child.marker in process_command(pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                        stopped.append(f"{name} (pid {pid})")
        return stopped

    def clear_state(self) -> None:
        """Clear .demo-state/: the CD system's records, logs, and the smoke result. Keeps the socket."""
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        for item in self.settings.state_dir.iterdir():
            if item.name == "supervisor.sock":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    # ------------------------------------------------------------- starting

    async def start_all(self) -> None:
        if self.settings.local:
            if not shutil.which("temporal"):
                raise StartupError(
                    "The Temporal CLI isn't on your PATH. Install it (on macOS: brew install temporal), "
                    "then run make setup."
                )
            self.out(f"Starting the Temporal dev server on port {self.settings.temporal_port}...")
            self.children["dev-server"].start(self.env)
        clients = await self.connect()
        from demo.setup_local import ensure_endpoints

        if self.settings.local:
            await ensure_endpoints(clients.platform)
        self.out("Starting the gateway and Release Engineering Workers...")
        for name in START_WORKERS:
            self.children[name].start(self.env)
        await self.wait_for_workers(clients, START_WORKERS)
        self.record_pids()

        from demo import seed

        await seed.reset(clients.platform, clients.release_eng, clients.security)
        self.out("Seeded the start state: policy v1, Release Engineering's tools, the release ledger.")
        await self.run_smoke(clients)
        self.out("Starting the MCP front door and the release console...")
        for name in SERVICES:
            self.children[name].start(self.env)
        await self.wait_for_port("front-door", self.settings.gateway_port)
        await self.wait_for_port("console", self.settings.console_port)
        self.record_pids()

    async def connect(self, timeout: float = 30) -> Clients:
        from gateway.connection import connect

        deadline = time.monotonic() + timeout
        while True:
            dev_server = self.children.get("dev-server")
            if dev_server and dev_server.proc and not dev_server.running:
                raise StartupError(f"The Temporal dev server stopped while starting:\n{dev_server.tail()}")
            try:
                clients = Clients(
                    await connect("platform"), await connect("release-eng"), await connect("security")
                )
                for client in (clients.platform, clients.release_eng):
                    await client.count_workflows()  # the Namespace is ready (security's may not exist yet)
                return clients
            except Exception as err:
                if time.monotonic() > deadline:
                    raise StartupError(f"Couldn't reach Temporal within {timeout:g} seconds: {err}") from err
                await asyncio.sleep(0.5)

    async def wait_for_workers(self, clients: Clients, names: Sequence[str], timeout: float = 30) -> None:
        from datetime import UTC, datetime

        deadline = time.monotonic() + timeout
        waiting = set(names)
        while waiting:
            for name in list(waiting):
                child = self.children[name]
                if not child.running:
                    raise StartupError(
                        f"The {name} Worker stopped while starting. Its log ends:\n{child.tail()}"
                    )
                status = await worker_status(clients.for_worker(name), name, datetime.now(UTC))
                if status.status == "up":
                    waiting.discard(name)
            if waiting and time.monotonic() > deadline:
                raise StartupError(
                    f"These Workers didn't report in within {timeout:g} seconds: {', '.join(sorted(waiting))}."
                )
            await asyncio.sleep(0.3)

    async def wait_for_port(self, name: str, port: int, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        child = self.children[name]
        while not port_taken(port):
            if not child.running:
                raise StartupError(f"The {name} stopped while starting. Its log ends:\n{child.tail()}")
            if time.monotonic() > deadline:
                raise StartupError(f"The {name} didn't open port {port} within {timeout:g} seconds.")
            await asyncio.sleep(0.2)

    @property
    def smoke_applies(self) -> bool:
        """The smoke test checks dev-server flags, so it runs on the local profile only."""
        return self.settings.local

    async def run_smoke(self, clients: Clients) -> None:
        from demo.smoke import run_smoke, write_result, write_skipped

        if not self.smoke_applies:
            write_skipped(self.settings.state_dir, self.settings.profile)
            self.smoke = {"passed": True, "message": "skipped: not the local profile"}
            return

        write_result(self.settings.state_dir, None)
        result = await run_smoke(clients.platform)
        write_result(self.settings.state_dir, result)
        self.smoke = {"passed": result.passed, "message": result.message}
        if not result.passed:
            raise StartupError(result.message)
        self.out(result.message)

    # ------------------------------------------------------------- stopping, killing, resetting

    async def stop_all(self) -> None:
        """Front door and console first, then the Workers, then the dev server."""
        for name in [*SERVICES, *reversed(list(WORKERS)), "dev-server"]:
            if name in self.children:
                await self.children[name].stop()
        self.record_pids()

    def active_workers(self) -> list[str]:
        """The start state's Workers, plus those of teams that have joined."""
        return [n for n in WORKERS if n in START_WORKERS or n in self.joined]

    def not_joined(self, name: str) -> dict[str, Any] | None:
        if name in self.active_workers():
            return None
        team_name = next((n for w, n in JOINS_LATER.values() if w == name), name)
        return {"error": f"The {name} Worker isn't part of the demo until {team_name} joins.", "status": 409}

    async def join(self, team: str) -> dict[str, Any]:
        """A new team joins: its Namespace, its two Endpoints, and its Worker."""
        if team not in JOINS_LATER:
            return {"error": f"There's no team called {team} waiting to join.", "status": 404}
        async with self.lock:
            worker, team_name = JOINS_LATER[team]
            if team in self.joined:
                return {"message": f"{team_name} has already joined.", "joined": False}
            from datetime import timedelta

            from google.protobuf.duration_pb2 import Duration
            from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest

            from demo import seed
            from demo.setup_local import ensure_endpoints

            clients = await self.connect()
            if self.settings.local:
                retention = Duration()
                retention.FromTimedelta(timedelta(days=1))
                try:
                    await clients.platform.workflow_service.register_namespace(
                        RegisterNamespaceRequest(
                            namespace=team, workflow_execution_retention_period=retention
                        )
                    )
                except RPCError as err:
                    if err.status != RPCStatusCode.ALREADY_EXISTS:
                        raise
                # A new Namespace takes a moment to reach every part of the dev server: until
                # it does, creating an Endpoint that targets it, or starting its Worker, fails.
                deadline = time.monotonic() + 30
                while True:
                    try:
                        await clients.for_worker(worker).count_workflows()
                        await ensure_endpoints(clients.platform, (team,))
                        break
                    except RPCError:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(0.5)
            await seed.refresh_directory(clients.platform)  # Platform adds the team's people
            self.children[worker].start(self.env)
            self.joined.add(team)
            self.record_pids()
            try:
                await self.wait_for_workers(clients, (worker,), timeout=60)
            except StartupError as err:
                return {"error": str(err), "status": 500}
            message = (
                f"{team_name} joined: the {team} Namespace, the {team}-tools and platform-registry-{team} "
                f"Endpoints, and {team_name}'s Worker."
            )
            self.out(message)
            return {"message": message, "joined": True}

    async def kill(self, name: str) -> dict[str, Any]:
        if refused := self.not_joined(name):
            return refused
        child = self.children[name]
        if not child.running:
            return {"error": f"The {name} Worker isn't running.", "status": 409}
        pid = child.pid
        child.kill()
        self.record_pids()
        self.out(f"Killed the {name} Worker (SIGKILL, pid {pid}).")
        return {"name": name, "state": "stopped", "pid": pid}

    async def restart(self, name: str) -> dict[str, Any]:
        if refused := self.not_joined(name):
            return refused
        child = self.children[name]
        await child.stop()
        child.start(self.env)
        self.record_pids()
        self.out(f"Restarted the {name} Worker (pid {child.pid}).")
        return {"name": name, "state": "running", "pid": child.pid}

    async def reset(self) -> str:
        """Stop everything, clear .demo-state/, start a fresh dev server, and reseed."""
        async with self.lock:
            self.out("Resetting: stopping every process...")
            await self.stop_all()
            self.joined.clear()  # a fresh dev server: no joined team's Namespace or Endpoints
            self.clear_state()
            await self.start_all()
            message = "Reset: a fresh dev server, policy v1, Release Engineering's tools, no sessions."
            self.out(message)
            return message

    # ------------------------------------------------------------- the HTTP interface

    def app(self) -> Starlette:
        async def workers(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "workers": [
                        {
                            "name": n,
                            "state": "running" if self.children[n].running else "stopped",
                            "pid": self.children[n].pid,
                        }
                        for n in self.active_workers()
                    ],
                    "joined": sorted(self.joined),
                }
            )

        async def status(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "supervisor": os.getpid(),
                    "children": {n: {"running": c.running, "pid": c.pid} for n, c in self.children.items()},
                    "smoke": self.smoke,
                }
            )

        async def act(request: Request) -> JSONResponse:
            name, action = request.path_params["name"], request.path_params["action"]
            if name not in WORKERS or action not in ("kill", "restart"):
                return JSONResponse({"error": f"There's no Worker called {name}."}, status_code=404)
            async with self.lock:
                body = await (self.kill(name) if action == "kill" else self.restart(name))
            return JSONResponse(body, status_code=body.pop("status", 200))

        async def join(request: Request) -> JSONResponse:
            try:
                body = await self.join(request.path_params["team"])
            except (StartupError, RPCError) as err:
                return JSONResponse({"error": str(err)}, status_code=500)
            return JSONResponse(body, status_code=body.pop("status", 200))

        async def reset(request: Request) -> JSONResponse:
            try:
                return JSONResponse({"ok": True, "message": await self.reset()})
            except StartupError as err:
                return JSONResponse({"error": str(err)}, status_code=500)

        return Starlette(
            routes=[
                Route("/workers", workers),
                Route("/status", status),
                Route("/workers/{name}/{action}", act, methods=["POST"]),
                Route("/reset", reset, methods=["POST"]),
                Route("/teams/{team}/join", join, methods=["POST"]),
            ]
        )


class SocketServer(uvicorn.Server):
    """uvicorn on the supervisor's socket, leaving Ctrl+C to the supervisor."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def serve_or_report(self) -> None:
        """uvicorn exits the process when it can't bind; turn that into a check run() makes."""
        with contextlib.suppress(SystemExit):
            await self.serve()


def socket_problem(settings: Settings, reason: str = "") -> str | None:
    """Why the supervisor can't open its control socket, in a plain sentence; None if it can."""
    path = settings.socket_path.resolve()
    size = len(unix_socket_address(path).encode())
    if size < SOCKET_PATH_LIMIT and not reason:
        return None
    relative = len(os.path.relpath(path).encode())
    why = reason or (
        f"its path is {len(str(path).encode())} bytes ({relative} relative to the repo), and a Unix "
        f"socket path must be shorter than {SOCKET_PATH_LIMIT} bytes"
    )
    return (
        f"The supervisor can't open its control socket at {path}: {why}. Set DEMO_STATE_DIR in .env "
        "to a shorter folder, or remove it to use .demo-state/."
    )


# ---------------------------------------------------------------- commands


def banner(settings: Settings) -> str:
    mcp = f"http://localhost:{settings.gateway_port}/mcp"
    lines = [
        "",
        "The Pinecrest agent gateway is running.",
        f"  Release console   http://localhost:{settings.console_port}",
    ]
    if settings.local:
        lines.append(f"  Temporal UI       http://localhost:{settings.temporal_ui_port}")
    lines += [
        f"  MCP front door    {mcp}",
        "Connect Claude Code as Noah:",
        f'  claude mcp add --transport http pinecrest-gateway {mcp} --header "Authorization: Bearer demo-token-noah"',
        f"Logs are in {settings.state_dir.name}/logs/. Press Ctrl+C to stop everything.",
    ]
    return "\n".join(lines)


async def supervisor_answers(settings: Settings) -> bool:
    if not settings.socket_path.exists():
        return False
    transport = httpx2.AsyncHTTPTransport(uds=unix_socket_address(settings.socket_path))
    async with httpx2.AsyncClient(transport=transport, base_url="http://supervisor", timeout=1.0) as http:
        try:
            await http.get("/status")
            return True
        except (httpx2.HTTPError, OSError):
            return False


async def run(settings: Settings) -> int:
    if await supervisor_answers(settings):
        print("The demo is already running (another make run). Stop it with Ctrl+C in its terminal first.")
        return 1
    supervisor = Supervisor(settings)
    for leftover in supervisor.clean_up_after_a_previous_run():
        print(f"Stopped {leftover}, left over from a run that didn't exit cleanly.")
    problem = socket_problem(settings)
    if problem:
        print(problem)
        return 1
    conflicts = port_conflicts(settings)
    if conflicts:
        print("\n".join(conflicts))
        return 1
    supervisor.clear_state()
    settings.socket_path.unlink(missing_ok=True)
    server = SocketServer(
        uvicorn.Config(supervisor.app(), uds=unix_socket_address(settings.socket_path), log_level="warning")
    )
    serving = asyncio.create_task(server.serve_or_report())
    while not server.started and not serving.done():
        await asyncio.sleep(0.05)
    if not server.started:
        print(socket_problem(settings, reason="the operating system refused it (uvicorn's error is above)"))
        return 1
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    code = 0
    try:
        starting = asyncio.create_task(supervisor.start_all())
        stopped = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({starting, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if starting in done:
            starting.result()  # raises StartupError
            print(banner(settings))
            await stop.wait()
        else:
            starting.cancel()
        print("\nStopping everything...")
    except StartupError as err:
        print(f"\n{err}")
        code = 1
    finally:
        await supervisor.stop_all()
        server.should_exit = True
        await serving
        settings.socket_path.unlink(missing_ok=True)
        supervisor.pids_file.unlink(missing_ok=True)
        print("Stopped.")
    return code


async def ask(
    settings: Settings, method: str, path: str, timeout: float
) -> tuple[int, dict[str, Any]] | None:
    """Send one request to a running supervisor; None if it isn't running."""
    if not settings.socket_path.exists():
        return None
    transport = httpx2.AsyncHTTPTransport(uds=unix_socket_address(settings.socket_path))
    async with httpx2.AsyncClient(transport=transport, base_url="http://supervisor", timeout=timeout) as http:
        try:
            response = await http.request(method, path)
        except (httpx2.ConnectError, OSError):
            return None
        return response.status_code, response.json()


async def control(settings: Settings, command: str, name: str) -> int:
    if command in ("kill", "restart") and name not in WORKERS:
        print(f"WORKER must be one of {', '.join(WORKERS)}, not {name!r}.")
        return 1
    if command == "join":
        answer = await ask(settings, "POST", f"/teams/{name}/join", timeout=90)
        if answer is None:
            print("The demo isn't running, so no team can join. Start it with make run.")
            return 1
        status, body = answer
        print(body.get("message") or body.get("error", body))
        return 0 if status == 200 else 1
    if command == "reset":
        answer = await ask(settings, "POST", "/reset", timeout=180)
        if answer is None:
            supervisor = Supervisor(settings)
            leftovers = supervisor.clean_up_after_a_previous_run()
            supervisor.clear_state()
            settings.socket_path.unlink(missing_ok=True)
            for leftover in leftovers:
                print(f"Stopped {leftover}, left over from a run that didn't exit cleanly.")
            print("Nothing is running. Cleared .demo-state/; make run starts from the start state.")
            return 0
    else:
        answer = await ask(settings, "POST", f"/workers/{name}/{command}", timeout=30)
        if answer is None:
            print("The demo isn't running, so there's no Worker to stop or start. Start it with make run.")
            return 1
    status, body = answer
    if status != 200:
        print(body.get("error", body))
        return 1
    if command == "reset":
        print(body["message"])
    elif command == "kill":
        print(
            f"Killed the {name} Worker (SIGKILL, pid {body['pid']}). Restart it with make recovery WORKER={name}."
        )
    else:
        print(f"Restarted the {name} Worker (pid {body['pid']}).")
    return 0


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # progress shows as it happens, even through a pipe
    os.chdir(REPO_ROOT)  # the socket path may be relative to the repo (see unix_socket_address)
    load_dotenv(REPO_ROOT / ".env")
    settings = Settings.from_env()
    args = sys.argv[1:] or ["run"]
    if args[0] == "run":
        return asyncio.run(run(settings))
    if args[0] == "join" and len(args) == 2:
        return asyncio.run(control(settings, "join", args[1]))
    if args[0] in ("kill", "restart") and len(args) == 2:
        return asyncio.run(control(settings, args[0], args[1]))
    if args[0] == "reset":
        return asyncio.run(control(settings, "reset", ""))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
