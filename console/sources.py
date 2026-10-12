"""How the release console reads Temporal: Queries, visibility listings, and descriptions.

Each source has its own background loop, so a slow or failing read never holds up
the others or the stream to the browser. Every Query has a 1-second timeout and is
asked once more if it times out (the first Query after Continue-As-New can take
about a second). When both attempts fail, the source keeps its last good state and
is marked stale: a team's Queries fail while its Worker is down, and the console
shows the last known state instead of going blank.

Descriptions (pending Activities and Nexus Operations) and the Worker list come
from the server itself, so they keep working while a Worker is down. That is how
the console shows a review step "waiting for a Worker" with its attempt count.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from temporalio.api.enums.v1 import PendingActivityState, PendingNexusOperationState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    DescribeNamespaceRequest,
    DescribeTaskQueueRequest,
    ListWorkersRequest,
)
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from console.view import WORKER_NAMESPACES, WORKER_TEAMS, Observed, Source, WorkerStatus

QUERY_TIMEOUT = timedelta(seconds=1)  # every Query fails fast
READ_EVERY_SECONDS = 0.1  # with the 100 ms push cap, a change reaches the browser within about 200 ms
WORKERS_EVERY_SECONDS = 1.0
# Workers heartbeat every second. Four seconds without one means the Worker is gone.
WORKER_SILENT_SECONDS = 4.0
# Pollers are the fallback when the server doesn't list Workers. They linger for
# a minute or more after a Worker dies, so they are slow to show one down.
POLLER_SILENT_SECONDS = 70.0
log = logging.getLogger("console.sources")


class NotFound(Exception):
    """The Workflow doesn't exist (yet): not an error, nothing to show."""


@dataclass
class Clients:
    platform: Client
    release_eng: Client
    security: Client

    def for_worker(self, name: str) -> Client:
        return {"gateway": self.platform, "release-eng": self.release_eng, "security": self.security}[name]


async def query_twice(client: Client, workflow_id: str, query: str, *args: Any) -> Any:
    """A fail-fast Query, asked once more if it times out. Raises NotFound or RPCError."""
    handle = client.get_workflow_handle(workflow_id)
    for attempt in (1, 2):
        try:
            return await handle.query(query, args=args, rpc_timeout=QUERY_TIMEOUT)
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise NotFound(workflow_id) from err
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def running_ids(client: Client, workflow_type: str) -> list[str]:
    query = f"WorkflowType = '{workflow_type}' AND ExecutionStatus = 'Running'"
    return [w.id async for w in client.list_workflows(query, rpc_timeout=QUERY_TIMEOUT)]


def short_name(enum_name: str, prefix: str) -> str:
    return enum_name.removeprefix(prefix).lower()


def iso(ts: Any) -> str | None:
    return ts.ToDatetime(tzinfo=UTC).isoformat() if ts.seconds or ts.nanos else None


async def pending_nexus_operations(client: Client, workflow_id: str) -> list[dict[str, Any]]:
    desc = await client.get_workflow_handle(workflow_id).describe(rpc_timeout=QUERY_TIMEOUT)
    return [
        {
            "endpoint": op.endpoint,
            "service": op.service,
            "operation": op.operation,
            "state": short_name(PendingNexusOperationState.Name(op.state), "PENDING_NEXUS_OPERATION_STATE_"),
            "attempt": op.attempt,
            "next_attempt_at": iso(op.next_attempt_schedule_time),
            "last_failure": op.last_attempt_failure.message if op.HasField("last_attempt_failure") else "",
            "blocked_reason": op.blocked_reason,
        }
        for op in desc.raw_description.pending_nexus_operations
    ]


async def desk_activities(client: Client, workflow_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """Whether the Workflow is running, and its pending Activities. Raises NotFound."""
    try:
        desc = await client.get_workflow_handle(workflow_id).describe(rpc_timeout=QUERY_TIMEOUT)
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            raise NotFound(workflow_id) from err
        raise
    running = desc.status == WorkflowExecutionStatus.RUNNING
    return running, [
        {
            "activity_type": a.activity_type.name,
            "attempt": a.attempt,
            "state": short_name(PendingActivityState.Name(a.state), "PENDING_ACTIVITY_STATE_"),
            "last_failure": a.last_failure.message if a.HasField("last_failure") else "",
        }
        for a in desc.raw_description.pending_activities
    ]


async def worker_status(client: Client, task_queue: str, now: datetime) -> WorkerStatus:
    """Up if a Worker on this Task Queue heartbeat in the last few seconds."""
    try:
        listing = await client.workflow_service.list_workers(
            ListWorkersRequest(namespace=client.namespace, page_size=200), timeout=QUERY_TIMEOUT
        )
    except RPCError as err:
        if err.status not in (RPCStatusCode.UNIMPLEMENTED, RPCStatusCode.PERMISSION_DENIED):
            raise
        return await poller_status(client, task_queue, now)
    beats = [w.worker_heartbeat for w in listing.workers_info if w.worker_heartbeat.task_queue == task_queue]
    for beat in beats:
        age = (now - beat.heartbeat_time.ToDatetime(tzinfo=UTC)).total_seconds()
        if beat.status == 1 and age <= WORKER_SILENT_SECONDS:  # WORKER_STATUS_RUNNING
            return WorkerStatus("up")
    if not beats:
        return WorkerStatus("down", "No Worker has reported in.")
    return WorkerStatus("down", "No heartbeat from its Worker.")


async def poller_status(client: Client, task_queue: str, now: datetime) -> WorkerStatus:
    described = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=task_queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        ),
        timeout=QUERY_TIMEOUT,
    )
    for poller in described.pollers:
        age = (now - poller.last_access_time.ToDatetime(tzinfo=UTC)).total_seconds()
        if age <= POLLER_SILENT_SECONDS:
            return WorkerStatus("up")
    return WorkerStatus("down", "No Worker is polling its Task Queue.")


# ---------------------------------------------------------------- the supervisor

# macOS limits a Unix socket's path to 104 bytes; a repo in a deep folder passes that.
UNIX_SOCKET_PATH_LIMIT = 100


def unix_socket_address(path: Path) -> str:
    """The socket's path, relative to the current folder when the absolute one is too long.

    The supervisor, the console, and the make targets all run from the repo root, so
    a relative path names the same socket for each of them.
    """
    absolute = str(path.resolve())
    return absolute if len(absolute) < UNIX_SOCKET_PATH_LIMIT else os.path.relpath(absolute)


class SupervisorClient:
    """The console's side of the demo supervisor's HTTP interface.

    The supervisor (`demo/supervisor.py`, which `make run` starts) owns the Worker
    processes. It serves HTTP on a Unix socket, `<DEMO_STATE_DIR>/supervisor.sock`
    (default `.demo-state/supervisor.sock`), so it needs no port and only local
    processes can reach it:

      GET  /workers                -> 200 {"workers": [{"name": "security", "state": "running", "pid": 4242}]}
      POST /workers/{name}/kill    -> SIGKILL the Worker's Python process (never the `uv run` wrapper)
                                      200 {"name": ..., "state": "stopped"}
      POST /workers/{name}/restart -> start it again (stopping it first if it is running)
                                      200 {"name": ..., "state": "running", "pid": ...}

    `name` is gateway, release-eng, or security. An unknown name is 404, and any
    refusal is 409, each with {"error": "<a plain sentence>"}.
    """

    def __init__(self, socket_path: Path, http: httpx2.AsyncClient | None = None) -> None:
        self.socket_path = socket_path
        self.http = http or httpx2.AsyncClient(
            transport=httpx2.AsyncHTTPTransport(uds=unix_socket_address(socket_path)),
            base_url="http://supervisor",
            timeout=httpx2.Timeout(5.0),
        )

    async def workers(self) -> dict[str, str] | None:
        """Each Worker's state, or None if the supervisor isn't running."""
        try:
            response = await self.http.get("/workers", timeout=1.0)
            response.raise_for_status()
        except (httpx2.HTTPError, OSError):
            return None
        return {w["name"]: w["state"] for w in response.json().get("workers", [])}

    async def join(self, team: str) -> tuple[int, dict[str, Any]]:
        """A new team joins the demo: its Namespace, its Endpoints, and its Worker."""
        try:
            response = await self.http.post(f"/teams/{team}/join", timeout=90.0)
        except (httpx2.HTTPError, OSError):
            return 503, {"error": "Adding a team needs the demo supervisor, which `make run` starts."}
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text}
        return response.status_code, body

    async def act(self, name: str, action: str) -> tuple[int, dict[str, Any]]:
        try:
            response = await self.http.post(f"/workers/{name}/{action}")
        except (httpx2.HTTPError, OSError):
            return 503, {
                "error": "Kill and Restart need the demo supervisor, which `make run` starts. "
                "It isn't running, so the console can't stop or start Workers."
            }
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text}
        return response.status_code, body


async def namespace_exists(client: Client, namespace: str) -> bool:
    try:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=namespace), timeout=QUERY_TIMEOUT
        )
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


# ---------------------------------------------------------------- the reader


class Reader:
    """Keeps an `Observed` up to date, one background loop per source."""

    def __init__(
        self,
        clients: Clients,
        service: str,
        observed: Observed,
        supervisor: SupervisorClient | None = None,
        read_every: float = READ_EVERY_SECONDS,
        workers_every: float = WORKERS_EVERY_SECONDS,
        team_namespaces: dict[str, str] | None = None,
    ) -> None:
        self.clients = clients
        # Teams whose Namespace may not exist yet (Security, until it joins). None: all exist.
        self.team_namespaces = team_namespaces
        self.service = service
        self.o = observed
        self.supervisor = supervisor
        self.read_every = read_every
        self.workers_every = workers_every
        self.nudged = asyncio.Event()
        self._session_views: dict[str, Any] = {}
        self.changed: Callable[[], None] = lambda: None
        # One read of a source at a time, so a read that started before an action can't
        # land after the fresh read the action asked for and put older state back.
        self._locks = {
            n: asyncio.Lock() for n in ("registry", "sessions", "release_eng", "security", "workers")
        }

    def nudge(self) -> None:
        """Read everything again now (after an action), instead of on the next tick."""
        self.nudged.set()

    async def refresh(self, *names: str) -> None:
        """Read these sources now, after any read already under way (an action just changed them)."""
        for name in names:
            async with self._locks[name]:
                await getattr(self, f"read_{name}")()

    async def run(self) -> None:
        loops = [
            self._loop("registry", self.read_every),
            self._loop("sessions", self.read_every),
            self._loop("release_eng", self.read_every),
            self._loop("security", self.read_every),
            self._loop("workers", self.workers_every, nudgeable=False),
        ]
        await asyncio.gather(*loops)

    async def _loop(self, name: str, every: float, nudgeable: bool = True) -> None:
        while True:
            try:
                await self.refresh(name)
            except Exception as err:  # noqa: BLE001 - a read loop must never die
                log.warning("reading %s failed: %s", name, err)
            self.changed()
            if nudgeable:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.nudged.wait(), timeout=every)
                # Let every loop see the nudge before it is cleared.
                await asyncio.sleep(0)
                self.nudged.clear()
            else:
                await asyncio.sleep(every)

    async def _read_into(self, source: Source, read: Callable[[], Awaitable[Any]]) -> None:
        try:
            source.data = await read()
            source.stale, source.error = False, ""
            source.read_at = datetime.now(UTC)
        except NotFound:
            source.data, source.stale, source.error = None, False, ""
        except (RPCError, WorkflowQueryFailedError, TimeoutError) as err:
            source.stale, source.error = True, getattr(err, "message", None) or str(err)

    # ------------------------------------------------------------- sources

    async def read_registry(self) -> None:
        await self._read_into(
            self.o.registry, lambda: query_twice(self.clients.platform, "tool-registry", "snapshot")
        )

    async def read_sessions(self) -> None:
        platform = self.clients.platform

        async def read() -> list[Any]:
            ids = await running_ids(platform, "AgentSessionWorkflow")
            results = await asyncio.gather(
                *(query_twice(platform, sid, "view") for sid in ids), return_exceptions=True
            )
            failed = None
            views = []
            for sid, result in zip(ids, results, strict=True):
                if isinstance(result, BaseException):
                    if not isinstance(result, NotFound):
                        failed = result
                    if sid in self._session_views and not isinstance(result, NotFound):
                        views.append(self._session_views[sid])
                    continue
                self._session_views[sid] = result
                views.append(result)
            self._session_views = {k: v for k, v in self._session_views.items() if k in ids}
            await self._read_session_ops(views)
            if failed is not None:
                self.o.sessions.data = views
                raise failed
            return views

        await self._read_into(self.o.sessions, read)

    async def _read_session_ops(self, views: list[Any]) -> None:
        """Pending Nexus Operations of sessions with a call out to a team, from the server."""
        out = [
            v["session_id"]
            for v in views
            if any(c["status"] in ("running", "awaiting_gate") and c.get("endpoint") for c in v["calls"])
        ]
        results = await asyncio.gather(
            *(pending_nexus_operations(self.clients.platform, sid) for sid in out), return_exceptions=True
        )
        self.o.session_ops = {
            sid: r for sid, r in zip(out, results, strict=True) if not isinstance(r, BaseException) and r
        }

    async def read_release_eng(self) -> None:
        client = self.clients.release_eng
        await self._read_into(self.o.ledger, lambda: query_twice(client, f"ledger-{self.service}", "status"))

        async def deploys() -> list[dict[str, Any]]:
            ids = await running_ids(client, "DeployWorkflow")
            results = await asyncio.gather(
                *(query_twice(client, wid, "progress") for wid in ids), return_exceptions=True
            )
            for r in results:
                if isinstance(r, BaseException) and not isinstance(r, NotFound):
                    raise r
            return [
                {"workflow_id": wid, "progress": r}
                for wid, r in zip(ids, results, strict=True)
                if not isinstance(r, BaseException)
            ]

        await self._read_into(self.o.deploys, deploys)

    async def read_security(self) -> None:
        if "security" in self.o.absent_teams:  # Security hasn't joined: there's nothing to read
            self.o.desk.data, self.o.desk.stale, self.o.desk.error = None, False, ""
            self.o.desk_activities = []
            return
        client = self.clients.security
        desk = f"review-desk-{self.service}"
        try:
            # A description needs no Worker, so it tells "not running" (a reset ended
            # the desk; Security starts a fresh one on the next review) from "Worker down".
            running, self.o.desk_activities = await desk_activities(client, desk)
        except NotFound:
            running, self.o.desk_activities = False, []
        except RPCError:
            running, self.o.desk_activities = True, []
        if not running:
            self.o.desk.data, self.o.desk.stale, self.o.desk.error = None, False, ""
            return
        await self._read_into(self.o.desk, lambda: query_twice(client, desk, "desk_view"))

    async def read_presence(self) -> None:
        """Which teams have no Namespace yet. A team that hasn't joined shows nowhere."""
        if self.team_namespaces is None:
            return
        absent = set()
        for team, namespace in self.team_namespaces.items():
            if not await namespace_exists(self.clients.platform, namespace):
                absent.add(team)
        self.o.absent_teams = absent

    async def read_workers(self) -> None:
        await self.read_presence()
        now = datetime.now(UTC)
        names = [n for n in WORKER_NAMESPACES if WORKER_TEAMS[n] not in self.o.absent_teams]
        for gone in set(self.o.workers) - set(names):
            del self.o.workers[gone]
        results = await asyncio.gather(
            *(worker_status(self.clients.for_worker(n), n, now) for n in names), return_exceptions=True
        )
        supervised = await self.supervisor.workers() if self.supervisor else None
        self.o.supervisor_available = supervised is not None
        for name, result in zip(names, results, strict=True):
            status = result if isinstance(result, WorkerStatus) else WorkerStatus("unknown", str(result))
            if supervised and supervised.get(name) == "stopped":
                status = WorkerStatus(
                    "down", "Stopped by the supervisor."
                )  # known at once, before heartbeats lapse
            self.o.workers[name] = status
