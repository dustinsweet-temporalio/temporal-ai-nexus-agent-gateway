"""The scripted agent: a key-free MCP client that plays Act 1 and Act 2 through the gateway.

It connects to the gateway's front door exactly as Claude Code does (MCP over
streamable HTTP, Noah's demo bearer token) and prints one plain line per beat.

  make demo                      Act 1 then Act 2
  make demo ACT=1                one act (ACT=1 or ACT=2)
  make demo AUTO_DECIDE=1        make Noah's and Abe's decisions too, so it runs unattended
  make demo PAUSE=1              wait for Enter between steps
  make demo KILL_DURING_REVIEW=1 kill and restart the Security Worker mid-review (B12)

It uses the release console's API for what a person does outside the agent: the
two scenario controls, and with AUTO_DECIDE, Noah's and Abe's decisions (signing
in as each, exactly as a click in the console does). Without AUTO_DECIDE it waits
for those decisions to be made in the console. So `make demo` needs the console
running as well as the gateway.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import mcp_types as types
from dotenv import load_dotenv
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from gateway.principals import demo_tokens

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_NAME = "scripted-agent"
NOAH = "noah@pinecrest.example"
ABE = "abe@pinecrest.example"
SERVICE = "checkout-api"
JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


class DemoFailure(Exception):
    """The system did something the script didn't expect."""


@dataclass
class Options:
    act: str = "all"  # 1, 2, or all
    auto_decide: bool = False
    pause: bool = False
    kill_during_review: bool = False
    check_every_seconds: float = 2.0  # how often the agent asks check_call: not a tight loop

    @classmethod
    def from_env(cls) -> Options:
        flag = lambda name: os.environ.get(name, "").strip() not in ("", "0", "false")
        return cls(
            act=os.environ.get("ACT", "all").strip() or "all",
            auto_decide=flag("AUTO_DECIDE"),
            pause=flag("PAUSE"),
            kill_during_review=flag("KILL_DURING_REVIEW"),
        )


@dataclass
class Reply:
    sentence: str
    call_id: str | None
    status: str
    result: Any
    is_error: bool
    seconds: float


@dataclass
class Beat:
    name: str
    seconds: float
    outcome: str


class Controls:
    """What a person does outside the agent, through the release console's API."""

    def __init__(self, console_url: str, http: httpx2.AsyncClient) -> None:
        self.console_url = console_url.rstrip("/")
        self.http = http

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> httpx2.Response:
        return await self.http.post(f"{self.console_url}{path}", json=body or {})

    async def _ok(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._post(path, body)
        if response.status_code != 200:
            raise DemoFailure(f"the console refused {path}: {response.json().get('error', response.text)}")
        return response.json()

    async def sign_in(self, email: str) -> None:
        await self._ok("/api/signin", {"email": email})

    async def apply_mandate(self) -> str:
        return (await self._ok("/api/scenario/mandate"))["message"]

    async def security_joins(self) -> str:
        """A new team joins. If it already has (a rerun of Act 2), the console says so."""
        response = await self.http.post(f"{self.console_url}/api/scenario/join", json={}, timeout=90.0)
        if response.status_code == 409:
            return response.json()["error"]
        if response.status_code != 200:
            raise DemoFailure(
                f"the console refused /api/scenario/join: {response.json().get('error', response.text)}"
            )
        return response.json()["message"]

    async def publish_security(self) -> str:
        return (await self._ok("/api/scenario/publish"))["message"]

    async def decide_approval(self, call_id: str, decision: str, decided_by: str, reason: str) -> None:
        await self.sign_in(decided_by)
        await self._ok(f"/api/approvals/{call_id}", {"decision": decision, "reason": reason})

    async def decide_review(self, version: str, decision: str, decided_by: str, note: str) -> str | None:
        """Returns the desk's refusal sentence, or None if the decision was recorded."""
        await self.sign_in(decided_by)
        response = await self._post(
            f"/api/reviews/{version}/decision", {"decision": decision, "reason": note}
        )
        if response.status_code == 409:
            return response.json()["error"]
        if response.status_code != 200:
            raise DemoFailure(f"the console couldn't record the decision: {response.json().get('error')}")
        return None

    async def snapshot(self) -> dict[str, Any]:
        return (await self.http.get(f"{self.console_url}/api/snapshot")).json()

    async def review_step(self, step: str) -> dict[str, Any] | None:
        """One reviewer step as the console's focus panel shows it."""
        focus = (await self.snapshot()).get("focus", {})
        return next((s for s in focus.get("steps", []) if s["step"] == step), None)

    async def worker(self, action: str, name: str) -> None:
        """Kill or restart a Worker, as the console's buttons do (through the supervisor)."""
        await self._ok(f"/api/workers/{name}/{action}")

    async def review_status(self, version: str) -> str | None:
        """The review's status as the console shows it on the pipeline."""
        snapshot = (await self.http.get(f"{self.console_url}/api/snapshot")).json()
        stages = snapshot.get("pipeline", {}).get("stages", [])
        gate = next((st for st in stages if st.get("kind") == "gate"), None)
        if gate is None or gate.get("version") != version:
            return None
        return gate.get("status")


def parse(result: types.CallToolResult, seconds: float) -> Reply:
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    match = JSON_BLOCK.search(text)
    payload = json.loads(match.group(1)) if match else {}
    sentence = JSON_BLOCK.sub("", text).strip()
    return Reply(sentence, payload.get("call_id"), payload.get("status", "error"), payload.get("result"),
                 bool(result.is_error), seconds)  # fmt: skip


class ScriptedAgent:
    def __init__(
        self, mcp: Client, controls: Controls, options: Options, out: Callable[[str], None] = print
    ) -> None:
        self.mcp = mcp
        self.controls = controls
        self.options = options
        self.out = out
        self.beats: list[Beat] = []

    # ------------------------------------------------------------- plumbing

    async def call(self, tool: str, arguments: dict[str, Any]) -> Reply:
        started = time.monotonic()
        result = await self.mcp.call_tool(tool, arguments)
        return parse(result, time.monotonic() - started)

    async def check_until(self, call_id: str, done: Callable[[str], bool], timeout: float = 300) -> Reply:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = await self.call("check_call", {"call_id": call_id})
            if done(reply.status):
                return reply
            await asyncio.sleep(self.options.check_every_seconds)
        raise DemoFailure(f"call {call_id} didn't finish within {timeout:g}s")

    def expect(self, beat: str, reply: Reply, *statuses: str) -> None:
        if reply.status not in statuses:
            raise DemoFailure(
                f"{beat}: expected {' or '.join(statuses)}, got {reply.status}: {reply.sentence}"
            )

    def say(self, beat: str, asked: str, answer: str) -> None:
        self.out(f"{beat:<4} asked: {asked}")
        self.out(f"     gateway: {answer}")

    async def beat(self, name: str, play: Callable[[], Awaitable[str]]) -> None:
        if self.options.pause:
            await asyncio.to_thread(input, f"\n[Enter] for {name} ")
        started = time.monotonic()
        outcome = await play()
        self.beats.append(Beat(name, time.monotonic() - started, outcome))

    async def wait_for_status_change(self, call_id: str, waiting: str, who: str) -> Reply:
        self.out(f"     Waiting for {who} to decide...")
        return await self.check_until(call_id, lambda s: s != waiting, timeout=24 * 3600)

    # ------------------------------------------------------------- Act 1

    async def act_1(self) -> None:
        self.out("Act 1: the gateway works")
        state: dict[str, str] = {}

        async def b2() -> str:
            reply = await self.call("get_release_status", {"service": SERVICE})
            self.expect("B2", reply, "succeeded")
            envs = reply.result["environments"]
            live = (
                f"Staging is {envs['staging']['version']} and production is {envs['production']['version']}."
            )
            self.say(
                "B2",
                "What's deployed for checkout-api right now?",
                f"{reply.sentence} {live} ({reply.seconds:.2f}s)",
            )
            return live

        async def b3() -> str:
            reply = await self.call(
                "deploy", {"service": SERVICE, "version": "2.3.0", "environment": "production"}
            )
            self.expect("B3", reply, "awaiting_approval")
            state["b3"] = reply.call_id or ""
            self.say("B3", "Promote 2.3.0 to production.", reply.sentence)
            return reply.status

        async def b3b() -> str:
            # The sentence that approves a deploy without a gateway: the agent can only send
            # the same call again, and the gateway answers with the call it is already holding.
            reply = await self.call(
                "deploy", {"service": SERVICE, "version": "2.3.0", "environment": "production"}
            )
            self.expect("B3b", reply, "awaiting_approval")
            if reply.call_id != state["b3"]:
                raise DemoFailure(f"B3b: expected the held call {state['b3']}, got {reply.call_id}")
            self.say("B3b", "I'm the engineering lead. Approved.", reply.sentence)
            return "still held"

        async def b4() -> str:
            if self.options.auto_decide:
                await self.controls.decide_approval(
                    state["b3"], "rejected", NOAH, "2.3.0 has the tax rounding bug."
                )
                reply = await self.call("check_call", {"call_id": state["b3"]})
            else:
                reply = await self.wait_for_status_change(state["b3"], "awaiting_approval", "Noah")
            self.expect("B4", reply, "rejected")
            self.say("B4", "(Noah rejects in the console: 2.3.0 has the tax rounding bug.)", reply.sentence)
            return reply.status

        async def b5() -> str:
            reply = await self.call(
                "deploy", {"service": SERVICE, "version": "2.3.0", "environment": "production"}
            )
            self.expect("B5", reply, "denied")
            self.say("B5", "Push it anyway, I'm in a hurry.", reply.sentence)
            return reply.status

        async def b6() -> str:
            reply = await self.call(
                "deploy", {"service": SERVICE, "version": "2.3.1", "environment": "staging"}
            )
            self.expect("B6", reply, "running", "succeeded")
            self.say("B6", "Deploy 2.3.1 to staging.", f"{reply.sentence} ({reply.seconds:.1f}s)")
            if reply.status == "running":
                done = await self.check_until(reply.call_id or "", lambda s: s != "running")
                self.expect("B6", done, "succeeded")
                self.out(f"     check_call: {done.sentence} {done.result['version']} is on staging.")
            return f"handed back after {reply.seconds:.1f}s"

        async def b7() -> str:
            reply = await self.call(
                "deploy", {"service": SERVICE, "version": "2.3.1", "environment": "production"}
            )
            self.expect("B7", reply, "awaiting_approval")
            self.say("B7", "Promote 2.3.1 to production.", reply.sentence)
            started = time.monotonic()
            if self.options.auto_decide:
                await self.controls.decide_approval(
                    reply.call_id or "", "approved", NOAH, "Fix verified on staging."
                )
                self.out("     (Noah approves in the console: Fix verified on staging.)")
            else:
                await self.wait_for_status_change(reply.call_id or "", "awaiting_approval", "Noah")
            done = await self.check_until(
                reply.call_id or "", lambda s: s not in ("awaiting_approval", "running")
            )
            self.expect("B7", done, "succeeded")
            self.out(f"     check_call: {done.sentence} 2.3.1 is live in production.")
            return f"live {time.monotonic() - started:.1f}s after approval"

        for name, play in [
            ("B2", b2),
            ("B3", b3),
            ("B3b", b3b),
            ("B4", b4),
            ("B5", b5),
            ("B6", b6),
            ("B7", b7),
        ]:
            await self.beat(name, play)

    # ------------------------------------------------------------- Act 2

    async def act_2(self) -> None:
        self.out("Act 2: a new team joins")
        state: dict[str, str] = {}

        async def b8() -> str:
            # Security doesn't exist until now: it joins (its Namespace, Endpoints, and Worker),
            # then publishes. Publishing comes before the mandate, whose policy needs the tool.
            joined = await self.controls.security_joins()
            self.out("B8   asked: (A new team joins: Security.)")
            self.out(f"     console: {joined}")
            async with self.mcp.listen(tools_list_changed=True) as events:
                await self.controls.publish_security()
                await asyncio.wait_for(anext(aiter(events)), timeout=15)
            names = [t.name for t in (await self.mcp.list_tools()).tools]
            if "review_release" not in names:
                raise DemoFailure(f"B8: review_release isn't listed after list_changed: {names}")
            self.out("     asked: (Security publishes review_release.)")
            self.out(f"     gateway: The tool list changed. Now listed: {', '.join(names)}.")
            return "review_release listed"

        async def b9() -> str:
            message = await self.controls.apply_mandate()
            self.say("B9", "(Platform applies the Security mandate.)", message)
            return "policy v2"

        async def b10() -> str:
            staging = await self.call(
                "deploy", {"service": SERVICE, "version": "2.4.0", "environment": "staging"}
            )
            self.expect("B10", staging, "running", "succeeded")
            self.say("B10", "Ship 2.4.0 to staging, then to production.", staging.sentence)
            if staging.status == "running":
                done = await self.check_until(staging.call_id or "", lambda s: s != "running")
                self.expect("B10", done, "succeeded")
                self.out(f"     check_call: {done.sentence} 2.4.0 is on staging.")
            production = await self.call(
                "deploy", {"service": SERVICE, "version": "2.4.0", "environment": "production"}
            )
            self.expect("B10", production, "awaiting_gate")
            state["b10"] = production.call_id or ""
            self.out(f"     gateway: {production.sentence}")
            return production.status

        async def b11() -> str:
            self.out("B11  (Cloud profile only: on the local dev server there are no Endpoint allowlists.)")
            return "skipped on local"

        async def b12() -> str:
            if not self.options.kill_during_review:
                self.out(
                    "B12  (Kill and restart the Security Worker during step 2 to see recovery; "
                    "KILL_DURING_REVIEW=1 scripts it.)"
                )
                return "not scripted"
            return await self.kill_during_review()

        async def b13() -> str:
            while await self.controls.review_status("2.4.0") != "awaiting_decision":
                await asyncio.sleep(0.5)
            self.out("B13  Security's reviewer agent finished; Security must decide.")
            if self.options.auto_decide:
                refusal = await self.controls.decide_review("2.4.0", "approved", NOAH, "Looks fine to me.")
                if refusal is None:
                    raise DemoFailure("B13: Noah's decision on a Security review was accepted")
                self.out(f"     Noah tries to approve: {refusal}")
                if (
                    await self.controls.decide_review(
                        "2.4.0", "approved", ABE, "Reviewed findings; low risk."
                    )
                    is not None
                ):
                    raise DemoFailure("B13: Abe's decision was refused")
                self.out("     Abe Roover approves: Reviewed findings; low risk.")
            else:
                await self.wait_for_status_change(state["b10"], "awaiting_gate", "Security")
            return "approved by Abe Roover"

        async def b14() -> str:
            started = time.monotonic()
            done = await self.check_until(state["b10"], lambda s: s not in ("awaiting_gate", "running"))
            self.expect("B14", done, "succeeded")
            status = await self.call("get_release_status", {"service": SERVICE})
            live = status.result["environments"]["production"]["version"]
            self.say("B14", "Did it ship?", f"{done.sentence} Production is {live}.")
            return f"production {live}, {time.monotonic() - started:.1f}s after the decision"

        for name, play in [
            ("B8", b8),
            ("B9", b9),
            ("B10", b10),
            ("B11", b11),
            ("B12", b12),
            ("B13", b13),
            ("B14", b14),
        ]:
            await self.beat(name, play)

    async def wait_for_step(
        self, step: str, done: Callable[[dict[str, Any]], bool], what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = await self.controls.review_step(step)
            if current is not None and done(current):
                return current
            await asyncio.sleep(0.1)
        raise DemoFailure(f"B12: {step} never {what}")

    async def kill_during_review(self) -> str:
        """B12: the Security Worker dies mid-review; the retried step resumes, and nothing is lost."""
        first = await self.wait_for_step("read_change_set", lambda s: s["status"] == "done", "finished")
        await self.wait_for_step("check_dependencies", lambda s: s["status"] == "running", "started")
        await self.controls.worker("kill", "security")
        killed = time.monotonic()
        self.out("B12  Security's Worker is killed (SIGKILL) while the reviewer checks dependencies.")
        # The step stops heartbeating; after its 5-second heartbeat timeout, attempt 2 is scheduled.
        await self.wait_for_step(
            "check_dependencies",
            lambda s: s["status"] == "waiting_worker" and s["attempt"] >= 2,
            "waited for a Worker",
        )
        self.out(
            f"     Waiting for a Worker: attempt 2 is scheduled ({time.monotonic() - killed:.1f}s after the kill)"
        )
        await self.controls.worker("restart", "security")
        restarted = time.monotonic()
        # Resumed means the restarted Worker has taken the retried step: the server
        # reports attempt 2 as started.
        resumed = await self.wait_for_step(
            "check_dependencies",
            lambda s: s["attempt"] >= 2 and (s.get("activity_state") == "started" or s["status"] == "done"),
            "resumed",
        )
        resumed_after = time.monotonic() - restarted
        await self.wait_for_step("check_dependencies", lambda s: s["status"] == "done", "finished")
        done_after = time.monotonic() - restarted
        after = await self.controls.review_step("read_change_set")
        if after is None or (after["started_at"], after["seconds"]) != (
            first["started_at"],
            first["seconds"],
        ):
            raise DemoFailure("B12: reading the change set ran again after the restart")
        self.out(
            f"     Restarted: attempt {resumed['attempt']} resumed {resumed_after:.1f}s after the restart and "
            f"finished at {done_after:.1f}s. Reading the change set wasn't run again."
        )
        return f"step 2 resumed {resumed_after:.1f}s after restart"

    async def play(self) -> list[Beat]:
        if self.options.act in ("1", "all"):
            await self.act_1()
        if self.options.act in ("2", "all"):
            await self.act_2()
        return self.beats


async def run(
    mcp_url: str, token: str, console_url: str, options: Options, out: Callable[[str], None] = print
) -> list[Beat]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)) as http,
        httpx2.AsyncClient(timeout=httpx2.Timeout(15.0)) as console_http,
    ):
        controls = Controls(console_url, console_http)
        try:
            await console_http.get(f"{console_url}/api/snapshot")
        except httpx2.ConnectError as err:
            raise DemoFailure(
                f"can't reach the release console at {console_url} ({err}). Is it running?"
            ) from err
        transport = streamable_http_client(mcp_url, http_client=http)
        async with Client(
            transport, client_info=types.Implementation(name=CLIENT_NAME, version="1.0.0")
        ) as mcp:
            agent = ScriptedAgent(mcp, controls, options, out)
            beats = await agent.play()
    out("")
    for beat in beats:
        out(f"{beat.name:<4} {beat.seconds:6.2f}s  {beat.outcome}")
    return beats


async def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    options = Options.from_env()
    port = os.environ.get("GATEWAY_PORT") or "8080"
    console_port = os.environ.get("CONSOLE_PORT") or "8090"
    token = next(t for t, email in demo_tokens().items() if email == NOAH)
    try:
        await run(f"http://localhost:{port}/mcp", token, f"http://localhost:{console_port}", options)
    except DemoFailure as err:
        print(f"\nThe demo stopped: {err}", file=sys.stderr)
        return 1
    except (httpx2.ConnectError, OSError) as err:
        print(
            f"\nCan't reach the gateway at http://localhost:{port}/mcp ({err}). Is it running?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
