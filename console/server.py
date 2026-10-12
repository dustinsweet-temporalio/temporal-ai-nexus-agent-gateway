"""The release console's server: http://localhost:8090.

It reads every team's state from Temporal (see `console/sources.py`), turns it
into one snapshot (see `console/view.py`), and streams it to the browser over
Server-Sent Events whenever it changes. Every button is a POST that becomes a
Temporal Update with the signed-in person as the decider, so the console holds no
state of its own beyond who is signed in. A refused Update comes back as HTTP 409
with the Workflow's own sentence.

Demo sign-in only: anyone who can open the console can sign in as anyone on the
roster. It listens on 127.0.0.1 for that reason.

Run: uv run python -m console.server
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from temporalio.client import Client, WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from console.sources import (
    Clients,
    NotFound,
    Reader,
    SupervisorClient,
    namespace_exists,
    query_twice,
    running_ids,
)
from console.view import ConsoleConfig, EventTracker, Observed, Registration, build_view
from demo import seed
from gateway.connection import connect, connect_config
from gateway.contracts import parse_registration, registration_files, tool_registrations
from gateway.models import REGISTRY_ID, ApprovalDecision
from gateway.policy import load_policies
from gateway.principals import load_directory
from gateway.session_workflow import AgentSessionWorkflow
from security.publish import publish
from security.publish import refusal as publish_refusal
from security.review_desk_workflow import Decision, ReviewDeskWorkflow, desk_id

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"
POLICY_FILE = REPO_ROOT / "config" / "policy.yaml"
SERVICE = "checkout-api"
# A change found by a read is pushed within this long; an action's own result goes at once.
PUSH_AT_MOST_EVERY_SECONDS = 0.1
UPDATE_TIMEOUT = timedelta(seconds=10)
SECURITY_REGISTRATION = REPO_ROOT / "contracts" / "security_review.registration.yaml"
WORKER_NAMES = ("gateway", "release-eng", "security")
log = logging.getLogger("console.server")


# ---------------------------------------------------------------- configuration


def load_registrations() -> list[Registration]:
    registrations = []
    for path in registration_files():
        name = str(path.relative_to(REPO_ROOT))
        tools = tool_registrations(name, parse_registration(name, path.read_text()))
        first = next(iter(tools.values()))
        registrations.append(
            Registration(first["owner_team"], first["endpoint"], first["contract"], list(tools))
        )
    order = {"release-eng": 0, "security": 1}
    return sorted(registrations, key=lambda r: order.get(r.owner_team, 9))


def load_prices(path: Path = REPO_ROOT / "config" / "pricing.yaml") -> dict[str, tuple[float, float]]:
    with path.open() as f:
        pricing = yaml.safe_load(f)
    return {m: (p["input_per_mtok"], p["output_per_mtok"]) for m, p in pricing["models"].items()}


@dataclass
class Settings:
    port: int = 8090
    profile: str = "local"
    mode: str = "normal"
    reviewer: str = "mock"
    reviewer_model: str = "claude-sonnet-5-5"
    temporal_ui_port: int = 8233
    state_dir: Path = REPO_ROOT / ".demo-state"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            port=int(env("CONSOLE_PORT") or 8090),
            profile=(env("PROFILE") or "local").strip(),
            mode="record" if (env("CONSOLE_MODE") or "").strip() == "record" else "normal",
            reviewer=(env("REVIEWER") or "mock").strip().lower(),
            reviewer_model=(env("REVIEWER_MODEL") or "claude-sonnet-5-5").strip(),
            temporal_ui_port=int(env("TEMPORAL_UI_PORT") or 8233),
            state_dir=Path(env("DEMO_STATE_DIR") or REPO_ROOT / ".demo-state"),  # empty means default
        )

    def console_config(self, namespaces: dict[str, str]) -> ConsoleConfig:
        cloud = self.profile == "cloud"
        return ConsoleConfig(
            service=SERVICE,
            profile=self.profile,
            reviewer=self.reviewer,
            reviewer_model=self.reviewer_model,
            namespaces=namespaces,
            temporal_ui="https://cloud.temporal.io" if cloud else f"http://localhost:{self.temporal_ui_port}",
            registrations=load_registrations(),
            prices=load_prices(),
            mode_default=self.mode,
            cloud=cloud,
            directory=asdict(load_directory()),
            mandate=load_policies(POLICY_FILE)[2],
        )


# ---------------------------------------------------------------- the console


class Console:
    def __init__(self, settings: Settings, supervisor: SupervisorClient | None = None) -> None:
        self.settings = settings
        self.cfg = settings.console_config({})
        self.supervisor = supervisor
        self.observed = Observed()
        self.events = EventTracker()
        self.people = {p.email: p for p in load_directory().people}
        self.signed_in = next(iter(self.people))
        self.clients: Clients | None = None
        self.reader: Reader | None = None
        self.snapshot: dict[str, Any] = {}
        self.snapshot_json = ""
        self.version = 0
        self._last_push = 0.0
        self._changed = asyncio.Event()
        self._published = asyncio.Condition()

    # ------------------------------------------------------------- reading

    def attach(self, clients: Clients) -> Reader:
        self.clients = clients
        self.cfg = self.settings.console_config(
            {
                "platform": clients.platform.namespace,
                "release-eng": clients.release_eng.namespace,
                "security": clients.security.namespace,
            }
        )
        self.observed.problems = []
        # On the local dev server a team may not exist yet (Security, until it joins).
        teams = None if self.cfg.cloud else {r.owner_team: self.cfg.namespaces.get(r.owner_team, r.owner_team)
                                             for r in self.cfg.registrations}  # fmt: skip
        self.reader = Reader(clients, SERVICE, self.observed, self.supervisor, team_namespaces=teams)
        self.reader.changed = self._changed.set
        return self.reader

    async def connect_and_read(self, connect_to: Callable[[str], Awaitable[Client]] = connect) -> None:
        """Connect to every Namespace (retrying until Temporal is up), then read forever."""
        while True:
            try:
                clients = Clients(
                    await connect_to("platform"),
                    await connect_to("release-eng"),
                    await connect_to("security"),
                )
                break
            except (RuntimeError, RPCError, OSError) as err:
                address = connect_config("platform").get("target_host", "the configured address")
                self.observed.problems = [
                    {
                        "text": f"The console can't reach Temporal at {address}. Is the dev server running?",
                        "detail": str(err),
                    }
                ]
                self._changed.set()
                await asyncio.sleep(2)
        await self.attach(clients).run()

    def smoke_problems(self) -> list[dict[str, str]]:
        """A coral banner unless the startup smoke test (make run) has passed."""
        try:
            smoke = json.loads((self.settings.state_dir / "smoke.json").read_text())
        except (OSError, ValueError):
            smoke = {}
        if smoke.get("status") in ("passed", "skipped"):  # skipped: not the local profile
            return []
        if smoke.get("status") == "failed":
            return [{"text": "The startup smoke test failed, so Update-backed Operations may never return.",
                     "detail": smoke.get("message", "")}]  # fmt: skip
        if smoke.get("status") == "running":
            return [{"text": "The startup smoke test is running.", "detail": ""}]
        return [{"text": "The startup smoke test hasn't passed, so Update-backed Operations may never return. "
                 "make run starts everything and runs it.", "detail": ""}]  # fmt: skip

    def refresh_registrations(self) -> None:
        """Re-read the teams' registration files when one is added or changed (no restart needed)."""
        stamp = tuple((p.name, p.stat().st_mtime) for p in registration_files())
        if stamp != getattr(self, "_registrations_stamp", None):
            self._registrations_stamp = stamp
            self.cfg.registrations = load_registrations()

    def rebuild(self) -> bool:
        """Build the snapshot; True if it differs from the last one."""
        self.refresh_registrations()
        self.snapshot = build_view(self.observed, self.cfg, self.signed_in, self.events)
        self.snapshot["problems"] = [*self.snapshot["problems"], *self.smoke_problems()]
        encoded = json.dumps(self.snapshot, sort_keys=True, default=str)
        if encoded == self.snapshot_json:
            return False
        self.snapshot_json = encoded
        return True

    async def publish_forever(self) -> None:
        """Push a new snapshot when something changed, at most every 100 ms."""
        while True:
            with contextlib.suppress(TimeoutError):
                # Time passing changes the view too (a decision fades after 8 seconds).
                await asyncio.wait_for(self._changed.wait(), timeout=1.0)
            self._changed.clear()
            wait = self._last_push + PUSH_AT_MOST_EVERY_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self.push_now()

    async def push_now(self) -> None:
        if self.rebuild():
            self._last_push = time.monotonic()
            async with self._published:
                self.version += 1
                self._published.notify_all()

    async def after_action(self, *sources: str) -> None:
        """Re-read what an action changed and push it at once, so the click shows immediately."""
        if self.reader:
            with contextlib.suppress(Exception):
                await self.reader.refresh(*sources)
        await self.push_now()

    def nudge(self) -> None:
        if self.reader:
            self.reader.nudge()
        self._changed.set()

    # ------------------------------------------------------------- HTTP: reading

    async def index(self, request: Request) -> Response:
        return Response((WEB_DIR / "index.html").read_text(), media_type="text/html")

    async def api_snapshot(self, request: Request) -> Response:
        if not self.snapshot_json:
            self.rebuild()
        return Response(self.snapshot_json, media_type="application/json")

    async def api_stream(self, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            if not self.snapshot_json:
                self.rebuild()
            seen = self.version
            yield f"data: {self.snapshot_json}\n\n"
            while True:
                async with self._published:
                    try:
                        await asyncio.wait_for(
                            self._published.wait_for(lambda seen=seen: self.version != seen), timeout=15
                        )
                        changed = True
                    except TimeoutError:
                        changed = False
                if not changed:
                    yield ": still here\n\n"  # keeps proxies and the browser from closing it
                    continue
                seen = self.version
                yield f"data: {self.snapshot_json}\n\n"

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    # ------------------------------------------------------------- HTTP: actions

    def _no_temporal(self) -> JSONResponse:
        return refusal(503, "The console isn't connected to Temporal yet.")

    async def api_signin(self, request: Request) -> JSONResponse:
        email = str((await body_of(request)).get("email", ""))
        found = self.people.get(email)
        if found is None:
            return refusal(404, f"{email or 'That person'} isn't on the roster in config/principals.yaml.")
        self.signed_in = email
        await self.push_now()
        return JSONResponse({"signed_in": email, "name": found.name, "title": found.title})

    async def api_approval(self, request: Request) -> JSONResponse:
        if self.clients is None:
            return self._no_temporal()
        call_id = request.path_params["call_id"]
        body = await body_of(request)
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            return refusal(400, "A decision is approved or rejected.")
        session = await self._session_of(call_id)
        if session is None:
            return refusal(404, f"There's no call {call_id} in any running session.")
        handle = self.clients.platform.get_workflow_handle_for(AgentSessionWorkflow.run, session)
        reason = str(body.get("reason", "")).strip()

        async def send() -> Any:
            return await handle.execute_update(
                AgentSessionWorkflow.decide_approval,
                ApprovalDecision(call_id, decision, self.signed_in, reason),
                rpc_timeout=UPDATE_TIMEOUT,
            )

        return await self._update(
            send, "gateway", lambda rec: {"status": rec.status, "message": rec.message}, ("sessions",)
        )

    async def _session_of(self, call_id: str) -> str | None:
        """The running session that holds this call: from the last read, else asked afresh."""
        for view in self.observed.sessions.data or []:
            if any(c["call_id"] == call_id for c in view["calls"]):
                return view["session_id"]
        assert self.clients is not None
        platform = self.clients.platform
        for session in await running_ids(platform, "AgentSessionWorkflow"):
            with contextlib.suppress(RPCError, NotFound):
                if await query_twice(platform, session, "get_call", call_id) is not None:
                    return session
        return None

    async def api_review_decision(self, request: Request) -> JSONResponse:
        if self.clients is None:
            return self._no_temporal()
        version = request.path_params["version"]
        body = await body_of(request)
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            return refusal(400, "A decision is approved or rejected.")
        desk = self.clients.security.get_workflow_handle_for(ReviewDeskWorkflow.run, desk_id(SERVICE))
        note = str(body.get("reason", "")).strip()

        async def send() -> Any:
            return await desk.execute_update(
                ReviewDeskWorkflow.record_decision,
                Decision(version, decision, self.signed_in, note),
                rpc_timeout=UPDATE_TIMEOUT,
            )

        return await self._update(
            send, "security", lambda review: {"status": review.status}, ("security", "sessions")
        )

    async def api_mandate(self, request: Request) -> JSONResponse:
        if self.clients is None:
            return self._no_temporal()
        platform = self.clients.platform
        registry = await query_twice(platform, REGISTRY_ID, "snapshot")
        if registry["policy"]["version"] >= 2:
            return refusal(409, "The Security mandate is already in effect: policy v2 is active.")
        return await self._update(
            lambda: seed.apply_mandate(platform),
            "gateway",
            lambda v: {"message": f"Policy v2 is active (registry v{v})."},
            ("registry",),
        )

    async def join_security(self) -> JSONResponse | None:
        """Security joins: its Namespace, its two Endpoints, and its Worker. None once it has."""
        assert self.clients is not None
        if self.supervisor is None:  # no make run: Security has joined if its Namespace exists
            if await namespace_exists(self.clients.platform, self.clients.security.namespace):
                return None
            return refusal(503, "Adding a team needs the demo supervisor, which `make run` starts.")
        status, body = await self.supervisor.join("security")
        if status != 200:
            return refusal(status, body.get("error", "The team couldn't join."))
        if not body.get("joined"):
            return None
        await self.after_action("workers", "registry", "security")
        return JSONResponse({"ok": True, "message": body["message"]})

    async def api_join(self, request: Request) -> JSONResponse:
        if self.clients is None:
            return self._no_temporal()
        joined = await self.join_security()
        return joined or refusal(409, "Security has already joined.")

    async def api_publish(self, request: Request) -> JSONResponse:
        if self.clients is None:
            return self._no_temporal()
        joined = await self.join_security()  # publishing first brings Security in
        if joined is not None and joined.status_code != 200:
            return joined
        platform = self.clients.platform
        registry = await query_twice(platform, REGISTRY_ID, "snapshot")
        if "review_release" in registry["tools"]:
            return refusal(409, "Security's review_release is already on the gateway.")
        # A presenter control acting as Security: it runs Security's own publish Workflow in
        # the security Namespace, which registers through platform-registry-security.
        publishing = publish(self.clients.security, SECURITY_REGISTRATION, "scenario control (Security)")
        try:
            answer = await asyncio.wait_for(publishing, timeout=UPDATE_TIMEOUT.total_seconds())
        except WorkflowFailureError as err:
            self.nudge()
            return refusal(409, publish_refusal(err))
        except TimeoutError:
            self.nudge()
            return refusal(
                503,
                "Security's publish Workflow hasn't finished. Are the Security and gateway Workers running?",
                "It finishes on its own once they are.",
            )
        await self.after_action("registry")
        return JSONResponse(
            {"ok": True, "message": f"review_release is registered (registry v{answer['registry_version']})."}
        )

    async def api_worker(self, request: Request) -> JSONResponse:
        name, action = request.path_params["name"], request.path_params["action"]
        if name not in WORKER_NAMES or action not in ("kill", "restart"):
            return refusal(404, f"There's no Worker called {name}.")
        if self.supervisor is None:
            return refusal(503, "Kill and Restart need the demo supervisor, which `make run` starts.")
        status, body = await self.supervisor.act(name, action)
        await self.after_action("workers")
        return JSONResponse(body, status_code=status)

    async def _update(
        self,
        send: Callable[[], Awaitable[Any]],
        worker: str,
        answer: Callable[[Any], dict[str, Any]],
        changes: tuple[str, ...],
    ) -> JSONResponse:
        """Send an Update; a refusal comes back as 409 with the Workflow's own sentence.

        On success, the sources the Update changed are read again and pushed before
        the response returns, so the browser shows the result right after the click.
        """
        try:
            result = await send()
        except WorkflowUpdateFailedError as err:
            self.nudge()
            cause = err.cause
            message = cause.message if isinstance(cause, ApplicationError) else str(cause)
            return refusal(409, message)
        except RPCError as err:
            self.nudge()
            label = {"gateway": "gateway", "security": "Security"}[worker]
            return refusal(
                503,
                f"The {label} Worker didn't answer within {UPDATE_TIMEOUT.seconds} seconds. Is it running?",
                err.message,
            )
        await self.after_action(*changes)
        return JSONResponse({"ok": True, **answer(result)})

    # ------------------------------------------------------------- the app

    def app(self) -> Starlette:
        routes = [
            Route("/", self.index),
            Route("/api/snapshot", self.api_snapshot),
            Route("/api/stream", self.api_stream),
            Route("/api/signin", self.api_signin, methods=["POST"]),
            Route("/api/approvals/{call_id}", self.api_approval, methods=["POST"]),
            Route("/api/reviews/{version}/decision", self.api_review_decision, methods=["POST"]),
            Route("/api/scenario/mandate", self.api_mandate, methods=["POST"]),
            Route("/api/scenario/join", self.api_join, methods=["POST"]),
            Route("/api/scenario/publish", self.api_publish, methods=["POST"]),
            Route("/api/workers/{name}/{action}", self.api_worker, methods=["POST"]),
            Mount("/", StaticFiles(directory=WEB_DIR)),
        ]
        return Starlette(routes=routes)


def refusal(status: int, message: str, detail: str = "") -> JSONResponse:
    body = {"error": message, **({"detail": detail} if detail else {})}
    return JSONResponse(body, status_code=status)


async def body_of(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def serve(console: Console, host: str = "127.0.0.1", port: int = 8090) -> None:
    server = uvicorn.Server(uvicorn.Config(console.app(), host=host, port=port, log_level="warning"))
    tasks = [asyncio.create_task(console.connect_and_read()), asyncio.create_task(console.publish_forever())]
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s console %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # not one line per supervisor poll
    settings = Settings.from_env()
    supervisor = SupervisorClient(settings.state_dir / "supervisor.sock")
    console = Console(settings, supervisor)
    log.info("Release console at http://localhost:%d", settings.port)
    await serve(console, port=settings.port)


if __name__ == "__main__":
    asyncio.run(main())
