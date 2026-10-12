"""The release console's view: what each region shows for a given state. Pure, no Temporal."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from console.server import load_prices, load_registrations
from console.view import (
    ConsoleConfig,
    EventTracker,
    Observed,
    Source,
    WorkerStatus,
    build_view,
    calls_view,
    focus_view,
)
from gateway.contracts import CONTRACTS_DIR, team_registry_entries
from gateway.models import Approval, CallRecord, GateRecord, RegistryEntry, RegistrySnapshot, StateChange
from gateway.policy import load_policies
from gateway.principals import load_directory
from release_eng.workflows import seed_ledger_state
from tests.gateway_stack import POLICIES, ROOT

NOAH, ABE = "noah@pinecrest.example", "abe@pinecrest.example"
NOW = datetime(2026, 10, 3, 17, 0, tzinfo=UTC)
CFG = ConsoleConfig(
    service="checkout-api",
    profile="local",
    reviewer="mock",
    reviewer_model="claude-sonnet-5-5",
    namespaces={"platform": "platform", "release-eng": "release-eng", "security": "security"},
    temporal_ui="http://localhost:8233",
    registrations=load_registrations(),
    prices=load_prices(),
    mandate=POLICIES[2],
)
MANDATE_BLOCKED = "Policy v2 requires review_release, which isn't registered yet. Publish it first."


def scenario_step(view: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in view["topbar"]["scenario"]["steps"] if s["id"] == step_id)


def entries(registration: str) -> dict[str, RegistryEntry]:
    found = [RegistryEntry(**e) for e in team_registry_entries(CONTRACTS_DIR / registration)]
    for e in found:
        e.registered_by = "setup"
    return {e.name: e for e in found}


def registry(policy: int = 1, security: bool = False, blocked: list[Any] | None = None) -> dict[str, Any]:
    tools = entries("release_engineering.registration.yaml")
    if security:
        for e in entries("security_review.registration.yaml").values():
            e.registered_by, e.registered_at = "Security team", NOW.isoformat()
            e.registered_principal, e.registered_through = (
                "scenario control (Security)",
                "platform-registry-security",
            )
            tools[e.name] = e
    snap = RegistrySnapshot(tools, POLICIES[policy], [], blocked or [], load_directory(), len(tools))
    return asdict(snap)


def call(
    status: str, environment: str = "production", version: str = "2.3.0", minutes_ago: float = 1, **extra: Any
) -> dict[str, Any]:
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    args = {"service": "checkout-api", "version": version, "environment": environment}
    rec = CallRecord(
        call_id=f"c-{status}-{version}-{environment}",
        tool="deploy",
        arguments=args,
        summary=f"deploy checkout-api {version} to {environment} for noah",
        description=f"deploy checkout-api {version} to {environment}",
        principal=NOAH,
        principal_name="Noah Proovle",
        client_name="claude-code",
        session_id="session-0123456789ab",
        status=status,  # type: ignore[arg-type]
        policy_version=1,
        rule_id="deploy-production-approval" if environment == "production" else "deploy-staging",
        endpoint="release-eng-tools",
        service="ReleaseEngineering",
        operation="deploy",
        approver_role="checkout-lead" if environment == "production" else None,
        message=f"{status.capitalize()}: deploy checkout-api {version} to {environment}.",
        submitted_at=at,
        history=[StateChange(status, at)],
    )
    for key, value in extra.items():
        setattr(rec, key, value)
    return asdict(rec)


def sessions(*calls: dict[str, Any]) -> Source:
    view = {
        "session_id": "session-0123456789ab",
        "principal": NOAH,
        "principal_name": "Noah Proovle",
        "client_name": "claude-code",
        "calls": list(calls),
        "pending": [],
    }
    return Source(data=[view])


def step(name: str, label: str, status: str, **extra: Any) -> dict[str, Any]:
    base = {
        "step": name,
        "label": label,
        "status": status,
        "attempt": 1,
        "summary": "",
        "findings": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "model": "mock" if status == "done" else "",
        "started_at": (NOW - timedelta(seconds=10)).isoformat(),
        "completed_at": (NOW - timedelta(seconds=5)).isoformat() if status == "done" else None,
    }
    return {**base, **extra}


def desk(status: str, steps: list[dict[str, Any]], **extra: Any) -> Source:
    review = {
        "service": "checkout-api",
        "version": "2.4.0",
        "requested_by": NOAH,
        "requested_at": (NOW - timedelta(seconds=20)).isoformat(),
        "status": status,
        "steps": steps,
        "risk": None,
        "recommendation": None,
        "summary": "",
        "decision": None,
        "decided_by": None,
        "decided_at": None,
        "note": "",
        "failure": None,
    }
    review.update(extra)
    return Source(
        data={"service": "checkout-api", "deciding_team": "security", "open_requests": 1, "reviews": [review]}
    )


def observed(**fields: Any) -> Observed:
    o = Observed(
        registry=Source(data=registry()),
        sessions=Source(data=[]),
        ledger=Source(data=asdict(seed_ledger_state(NOW))),
        deploys=Source(data=[]),
        workers={n: WorkerStatus("up") for n in ("gateway", "release-eng", "security")},
    )
    for key, value in fields.items():
        setattr(o, key, value)
    return o


# ---------------------------------------------------------------- the start state


def test_the_start_state() -> None:
    view = build_view(observed(), CFG, NOAH, EventTracker(), NOW)

    assert view["registry"]["version"] == 2
    groups = {g["team_name"]: [t["name"] for t in g["tools"]] for g in view["registry"]["groups"]}
    assert {k: groups[k] for k in ("Release Engineering", "Security")} == {
        "Release Engineering": ["get_release_status", "deploy"],
        "Security": [],
    }
    assert [f"{r['subject']}: {r['text']}" for r in view["policy"]["rules"]] == [
        "Release status: allowed",
        "Deploy to staging: allowed",
        "Deploy to production: Checkout lead approves",
    ]
    stages = {s["id"]: s for s in view["pipeline"]["stages"]}
    assert list(stages) == ["candidates", "staging", "production"]
    assert stages["staging"]["deployed"]["version"] == "2.3.0"
    assert stages["staging"]["deployed"]["by"] == "Noah Proovle's agent"
    assert stages["production"]["deployed"]["version"] == "2.2.0"
    assert stages["production"]["badge"]["text"] == "Checkout lead approves"
    assert [(c["version"], c["summary"]) for c in view["pipeline"]["candidates"]] == [
        ("2.3.0", "Tax rounding"),
        ("2.3.1", "Tax rounding fix"),
        ("2.4.0", "Trail pricing"),
    ]
    assert view["focus"] == {"state": "now", "in_flight": [], "last_outcome": "", "stale_notes": []}
    assert (scenario_step(view, "mandate")["blocked"], scenario_step(view, "mandate")["blocked_short"]) == (
        MANDATE_BLOCKED,
        "publish first",
    )
    assert view["calls"]["line"] == "No calls yet"
    assert [t["tools"] for t in view["calls"]["teams"]][:2] == [["get_release_status", "deploy"], []]
    assert view["cost"]["text"] == "Mock reviewer · $0.00 · 0 tokens"
    assert (view["audit"]["calls"], view["audit"]["rows"]) == (0, [])
    assert view["topbar"]["profile"] == "Local dev server"
    assert view["topbar"]["reviewer"] == "Mock reviewer"
    assert view["topbar"]["signed_in"]["name"] == "Noah Proovle"


def test_the_mandate_changes_only_the_production_line_and_adds_the_security_stage() -> None:
    before = build_view(observed(), CFG, NOAH, EventTracker(), NOW)
    after = build_view(observed(registry=Source(data=registry(policy=2))), CFG, NOAH, EventTracker(), NOW)

    def lines(v: dict[str, Any]) -> dict[str, str]:
        return {r["subject"]: r["text"] for r in v["policy"]["rules"]}

    assert lines(after) == {**lines(before), "Deploy to production": "Security reviews first"}
    production = next(r for r in after["policy"]["rules"] if r["subject"] == "Deploy to production")
    assert (production["id"], production["tone"], production["icon"]) == (
        "deploy-production-security-gate",
        "pink",
        "shield-check",
    )
    assert [s["id"] for s in after["pipeline"]["stages"]] == [
        "candidates",
        "staging",
        "security",
        "production",
    ]
    assert after["pipeline"]["stages"][-1]["badge"]["text"] == "Security reviews first"
    assert scenario_step(after, "mandate")["done"] is True


def test_a_published_tool_shows_on_the_registry_and_the_diagram() -> None:
    view = build_view(
        observed(registry=Source(data=registry(policy=2, security=True))), CFG, NOAH, EventTracker(), NOW
    )
    security = next(g for g in view["registry"]["groups"] if g["team"] == "security")
    (row,) = security["tools"]
    assert (row["name"], row["mode"], row["contract"]) == (
        "review_release",
        "async",
        "contracts/security_review.nexusrpc.yaml",
    )
    assert row["input_schema"]["required"] == ["service", "version"]
    assert view["calls"]["teams"][1]["tools"] == ["review_release"]
    assert "Security review: allowed" in [f"{r['subject']}: {r['text']}" for r in view["policy"]["rules"]]
    (published,) = [r for r in view["audit"]["rows"] if r["kind"] == "scenario"]
    assert scenario_step(view, "mandate")["blocked"] is None
    assert (published["who"], published["via"], published["tool"], published["outcome"]) == (
        "Security team",
        "scenario control (Security)",
        "review_release",
        "Published",
    )
    assert published["details"]["Registered through"] == "platform-registry-security"


# ---------------------------------------------------------------- the focus panel


def test_a_held_call_needs_noahs_approval() -> None:
    o = observed(sessions=sessions(call("awaiting_approval")))
    focus = focus_view(o, CFG, NOW)
    assert focus == {
        "state": "approval",
        "call_id": "c-awaiting_approval-2.3.0-production",
        "title": "Approve production deploy?",
        "subject": "checkout-api 2.3.0 → production",
        "asked_by": "Asked by Noah's agent, for Noah Proovle",
        "why": "Production deploys need the Checkout lead's approval.",
        "rule": "rule deploy-production-approval · policy v1",
        "who": "Checkout lead",
        "session_link": "http://localhost:8233/namespaces/platform/workflows/session-0123456789ab",
        "stale_notes": [],
    }
    stages = {s["id"]: s for s in build_view(o, CFG, NOAH, EventTracker(), NOW)["pipeline"]["stages"]}
    assert stages["production"]["incoming"] == {
        "state": "waiting_approval",
        "version": "2.3.0",
        "label": "waiting for approval",
    }
    assert calls_view(o, CFG)["gateway"]["waiting_on_person"] is True


def test_a_decision_shows_for_eight_seconds_then_the_panel_goes_idle() -> None:
    decided_at = NOW - timedelta(seconds=3)
    rejected = call(
        "rejected",
        approval=Approval(
            "rejected", NOAH, "Noah Proovle", decided_at.isoformat(), "2.3.0 has the tax rounding bug."
        ),
        message="Rejected: deploy checkout-api 2.3.0 to production. Noah Proovle rejected it.",
    )
    o = observed(sessions=sessions(rejected))
    focus = focus_view(o, CFG, NOW)
    assert (focus["state"], focus["decision"], focus["decider"], focus["decider_title"], focus["reason"]) == (
        "decided",
        "rejected",
        "Noah Proovle",
        "Checkout lead",
        "2.3.0 has the tax rounding bug.",
    )
    assert focus_view(o, CFG, NOW + timedelta(seconds=6)) == {
        "state": "now",
        "in_flight": [],
        "last_outcome": "Rejected: deploy checkout-api 2.3.0 to production. Noah Proovle rejected it.",
        "stale_notes": [],
    }


def test_a_security_review_in_progress_then_waiting_for_a_decision() -> None:
    reviewing = desk(
        "reviewing",
        [
            step(
                "read_change_set",
                "Reading the change set",
                "done",
                input_tokens=1200,
                output_tokens=300,
                model="claude-sonnet-5-5",
            ),
            step("check_dependencies", "Checking dependencies", "running"),
            step("assess_risk", "Assessing risk", "pending"),
        ],
    )
    focus = focus_view(observed(registry=Source(data=registry(2, True)), desk=reviewing), CFG, NOW)
    assert (focus["state"], focus["subject"]) == ("review", "checkout-api 2.4.0")
    assert [(s["status"], s["tokens"]) for s in focus["steps"]] == [
        ("done", 1500),  # a real model's tokens show
        ("running", None),
        ("pending", None),
    ]
    assert focus["steps"][0]["seconds"] == 5.0

    finding = {"severity": "info", "title": "New outbound call on the checkout page", "detail": "..."}
    waiting = desk(
        "awaiting_decision",
        [
            step("read_change_set", "Reading the change set", "done"),
            step("check_dependencies", "Checking dependencies", "done"),
            step("assess_risk", "Assessing risk", "done", findings=[finding]),
        ],
        recommendation="approve",
        risk="low",
        summary="Low risk.",
    )
    focus = focus_view(observed(registry=Source(data=registry(2, True)), desk=waiting), CFG, NOW)
    assert (focus["state"], focus["recommendation"], focus["who"]) == (
        "decision",
        "Approve · low risk",
        "Security team",
    )
    assert focus["findings"] == [finding]
    assert [s["tokens"] for s in focus["steps"]] == [None, None, None], (
        "the mock reviewer shows durations only"
    )


def test_a_review_step_waits_for_a_worker_and_the_last_known_state_is_marked() -> None:
    review = desk(
        "reviewing",
        [
            step("read_change_set", "Reading the change set", "done"),
            step("check_dependencies", "Checking dependencies", "running"),
            step("assess_risk", "Assessing risk", "pending"),
        ],
    )
    review.stale = True
    o = observed(
        registry=Source(data=registry(2, True)),
        desk=review,
        desk_activities=[
            {"activity_type": "check_dependencies", "attempt": 2, "state": "scheduled", "last_failure": ""}
        ],
        workers={
            "gateway": WorkerStatus("up"),
            "release-eng": WorkerStatus("up"),
            "security": WorkerStatus("down"),
        },
    )
    focus = focus_view(o, CFG, NOW)
    assert [(s["status"], s["attempt"]) for s in focus["steps"]] == [
        ("done", 1),
        ("waiting_worker", 2),
        ("pending", 1),
    ]
    # The full sentence is in the focus panel only; the pipeline card shows a short chip.
    assert focus["stale_notes"] == ["Security Worker is down. Showing the last known state."]
    gate = next(
        s
        for s in build_view(o, CFG, NOAH, EventTracker(), NOW)["pipeline"]["stages"]
        if s["id"] == "security"
    )
    assert gate["stale"] == "Worker down"


def test_a_gate_that_cannot_start_shows_retrying_or_blocked_by_the_endpoint() -> None:
    gate = GateRecord(
        "review_release", {}, "security-tools", "SecurityReview", "review_release", team="Security"
    )
    held = call("awaiting_gate", version="2.4.0", gate=gate)
    op = {
        "endpoint": "security-tools",
        "service": "SecurityReview",
        "operation": "review_release",
        "state": "backing_off",
        "attempt": 4,
        "next_attempt_at": (NOW + timedelta(seconds=12)).isoformat(),
        "last_failure": "upstream timeout",
        "blocked_reason": "",
    }
    o = observed(registry=Source(data=registry(2, True)), sessions=sessions(held))
    o.session_ops = {"session-0123456789ab": [op]}
    focus = focus_view(o, CFG, NOW)
    assert (focus["state"], focus["team"], focus["attempt"], focus["failure"], focus["circuit_breaker"]) == (
        "retrying",
        "Security",
        4,
        "upstream timeout",
        False,
    )
    assert focus["next_attempt_at"] == op["next_attempt_at"] and focus["in_flight"] is False
    # The server counts finished attempts: with one in flight, the fifth attempt is under way.
    o.session_ops = {"session-0123456789ab": [{**op, "state": "scheduled", "next_attempt_at": None}]}
    in_flight = focus_view(o, CFG, NOW)
    assert (in_flight["state"], in_flight["attempt"], in_flight["in_flight"]) == ("retrying", 5, True)
    o.session_ops = {
        "session-0123456789ab": [{**op, "last_failure": "Permission denied: caller not allowed"}]
    }
    assert focus_view(o, CFG, NOW)["state"] == "blocked_endpoint"
    o.session_ops = {"session-0123456789ab": [{**op, "attempt": 1, "state": "started", "last_failure": ""}]}
    assert focus_view(o, CFG, NOW)["state"] != "retrying", "a gate that started is not retrying"
    # After a retry succeeds, the started Operation still reports its attempt count and last failure.
    o.session_ops = {"session-0123456789ab": [{**op, "attempt": 2, "state": "started"}]}
    assert focus_view(o, CFG, NOW)["state"] != "retrying", "a gate that started after a retry is not retrying"


# ---------------------------------------------------------------- pipeline, calls, audit, cost


def test_a_rejected_release_locks_its_candidate_and_is_listed_as_blocked() -> None:
    blocked = {
        "tool": "deploy",
        "fields": {"service": "checkout-api", "version": "2.3.0", "environment": "production"},
        "label": "checkout-api 2.3.0 to production",
        "blocked_by": NOAH,
        "blocked_by_name": "Noah Proovle",
        "reason": "Rejected.",
        "at": NOW.isoformat(),
    }
    snap = registry()
    snap["blocked_releases"] = [blocked]
    view = build_view(observed(registry=Source(data=snap)), CFG, NOAH, EventTracker(), NOW)
    assert [c["blocked"] for c in view["pipeline"]["candidates"]] == [True, False, False]
    assert [b["text"] for b in view["policy"]["blocked"]] == [
        "checkout-api 2.3.0 → production · rejected by Noah Proovle"
    ]


def test_a_running_deploy_shows_on_its_environment() -> None:
    progress = {
        "step": "wave-2",
        "label": "Rolling out (wave 2 of 4)",
        "percent": 27,
        "service": "checkout-api",
        "version": "2.3.1",
        "environment": "staging",
    }
    o = observed(deploys=Source(data=[{"workflow_id": "deploy-abc", "progress": progress}]))
    stages = {s["id"]: s for s in build_view(o, CFG, NOAH, EventTracker(), NOW)["pipeline"]["stages"]}
    assert stages["staging"]["incoming"] == {
        "state": "deploying",
        "version": "2.3.1",
        "percent": 27,
        "label": "Rolling out (wave 2 of 4)",
        "link": "http://localhost:8233/namespaces/release-eng/workflows/deploy-abc",
    }
    assert stages["production"]["incoming"] is None


def test_the_line_under_the_diagram_describes_the_latest_call() -> None:
    handed = call(
        "running", environment="staging", version="2.3.1", minutes_ago=0.1, handed_back_after_seconds=5.0
    )
    assert calls_view(observed(sessions=sessions(handed)), CFG)["line"] == (
        "deploy checkout-api 2.3.1 to staging · running · handed back to the agent after 5s"
    )
    quick = call("succeeded", environment="staging", minutes_ago=0)
    quick["finished_at"] = (NOW + timedelta(seconds=0.3)).isoformat()
    assert calls_view(observed(sessions=sessions(quick)), CFG)["line"] == (
        "deploy checkout-api 2.3.0 to staging · succeeded in 0.3s"
    )


def test_the_audit_lists_calls_gates_and_scenario_controls() -> None:
    gate = GateRecord(
        "review_release",
        {},
        "security-tools",
        "SecurityReview",
        "review_release",
        status="approved",
        team="Security",
        result={"decision": "approved", "decided_by": ABE},
    )
    gated = call("succeeded", version="2.4.0", gate=gate, minutes_ago=1)
    gated["history"].insert(0, {"status": "awaiting_gate", "at": gated["submitted_at"], "note": ""})
    denied = call("denied", minutes_ago=2, rule_id="blocked-release", endpoint=None)
    snap = registry(policy=2, security=True)
    snap["policy_history"] = [
        {
            "version": 2,
            "changed_at": NOW.isoformat(),
            "changed_by": "scenario control (Platform)",
            "summary": "Mandate",
        }
    ]
    view = build_view(
        observed(registry=Source(data=snap), sessions=sessions(gated, denied)), CFG, NOAH, EventTracker(), NOW
    )
    rows = [(r["kind"], r["who"], r["tool"], r["outcome"]) for r in view["audit"]["rows"]]
    assert ("call", "Noah Proovle", "deploy", "Succeeded") in rows
    assert ("gate", "Gateway, for Noah Proovle", "review_release", "Approved by Abe Roover") in rows
    assert ("call", "Noah Proovle", "deploy", "Denied") in rows
    assert ("scenario", "Scenario control (Platform)", "policy v2", "Applied") in rows
    assert ("scenario", "Security team", "review_release", "Published") in rows
    assert view["audit"]["calls"] == 3
    denied_row = next(r for r in view["audit"]["rows"] if r["outcome"] == "Denied")
    assert denied_row["details"]["Rule"] == "blocked-release"
    assert denied_row["details"]["Endpoint"] == "not dispatched"


def test_the_cost_meter_prices_real_tokens() -> None:
    review = desk(
        "reviewing",
        [
            step(
                "read_change_set",
                "Reading the change set",
                "done",
                input_tokens=1000,
                output_tokens=500,
                model="claude-sonnet-5-5",
            ),
            step("check_dependencies", "Checking dependencies", "running"),
            step("assess_risk", "Assessing risk", "pending"),
        ],
    )
    view = build_view(observed(desk=review), CFG, NOAH, EventTracker(), NOW)
    # 1,000 input at $2.00 and 500 output at $10.00 per million tokens: $0.007.
    assert view["cost"]["text"] == "Reviewer tokens: 1,500 · $0.01"
    assert view["cost"]["dollars"] == 0.007
    assert view["topbar"]["reviewer"] == "Claude reviewer · claude-sonnet-5-5"


# ---------------------------------------------------------------- animations


def test_animations_follow_a_call_and_never_replay_history() -> None:
    tracker = EventTracker()
    earlier = call("succeeded", environment="staging", minutes_ago=5)
    assert tracker.update([earlier], CFG) == [], "opening the console replays nothing"

    running = call("running", environment="staging", version="2.3.1", minutes_ago=0)
    events = tracker.update([running, earlier], CFG)
    assert [(e["kind"], e["team"]) for e in events] == [("submit", "platform"), ("out", "release-eng")]

    done = {**running, "status": "succeeded"}
    events = tracker.update([done, earlier], CFG)
    assert [(e["kind"], e["team"], e.get("ok")) for e in events[-1:]] == [("back", "release-eng", True)]

    denied = call("denied", minutes_ago=0, version="2.3.0")
    events = tracker.update([denied, done, earlier], CFG)
    assert [e["kind"] for e in events[-1:]] == ["submit"], "a denied call never crosses the boundary"


def test_policy_versions_load_from_config() -> None:
    assert sorted(load_policies(ROOT / "config" / "policy.yaml")) == [1, 2]


# ---------------------------------------------------------------- now, incoming deploys, chips


def test_now_lists_calls_in_flight_and_never_an_older_outcome() -> None:
    progress = {
        "step": "wave-2",
        "label": "Rolling out (wave 2 of 4)",
        "percent": 27,
        "service": "checkout-api",
        "version": "2.3.1",
        "environment": "staging",
    }
    deploying = call("running", environment="staging", version="2.3.1", minutes_ago=0.1)
    older = call("succeeded", environment="staging", version="2.3.0", minutes_ago=2)
    older["finished_at"] = (NOW - timedelta(minutes=1.9)).isoformat()
    o = observed(
        sessions=sessions(deploying, older),
        deploys=Source(data=[{"workflow_id": "deploy-abc", "progress": progress}]),
    )
    focus = focus_view(o, CFG, NOW)
    assert focus == {
        "state": "now",
        "in_flight": [
            {
                "call_id": deploying["call_id"],
                "text": "deploy checkout-api 2.3.1 to staging",
                "status": "Rolling out (wave 2 of 4)",
                "percent": 27,
                "tone": "orange",
            }
        ],
        "last_outcome": "",  # older than the deploy still in flight
        "stale_notes": [],
    }
    newer = call("denied", version="2.3.0", minutes_ago=0.05, message="Denied: deploy checkout-api 2.3.0.")
    newer["finished_at"] = newer["submitted_at"]
    o.sessions = sessions(newer, deploying, older)
    assert focus_view(o, CFG, NOW)["last_outcome"] == "Denied: deploy checkout-api 2.3.0."


def test_a_recorded_deploy_is_no_longer_incoming() -> None:
    ledger = asdict(seed_ledger_state(NOW))
    ledger["environments"]["staging"]["version"] = "2.3.1"
    progress = {"step": "record", "label": "Recording the release", "percent": 100, "version": "2.3.1"}
    o = observed(
        ledger=Source(data=ledger),
        deploys=Source(
            data=[{"workflow_id": "deploy-abc", "progress": {**progress, "environment": "staging"}}]
        ),
    )
    staging = next(
        s for s in build_view(o, CFG, NOAH, EventTracker(), NOW)["pipeline"]["stages"] if s["id"] == "staging"
    )
    assert (staging["deployed"]["version"], staging["incoming"]) == ("2.3.1", None)


def test_a_gate_waiting_on_security_says_security() -> None:
    gate = GateRecord(
        "review_release", {}, "security-tools", "SecurityReview", "review_release", team="Security"
    )
    o = observed(
        registry=Source(data=registry(2, True)),
        sessions=sessions(call("awaiting_gate", version="2.4.0", gate=gate)),
    )
    production = next(
        s
        for s in build_view(o, CFG, NOAH, EventTracker(), NOW)["pipeline"]["stages"]
        if s["id"] == "production"
    )
    assert production["incoming"]["label"] == "waiting for Security's review"


def test_cards_show_a_short_chip_and_the_focus_panel_the_sentence() -> None:
    o = observed(
        workers={
            "gateway": WorkerStatus("down"),
            "release-eng": WorkerStatus("up"),
            "security": WorkerStatus("up"),
        }
    )
    o.registry.stale = True
    o.sessions.stale = True
    view = build_view(o, CFG, NOAH, EventTracker(), NOW)
    assert (view["registry"]["stale"], view["policy"]["stale"], view["calls"]["stale"]) == (
        "Worker down",
    ) * 3
    assert view["focus"]["stale_notes"] == ["The gateway Worker is down. Showing the last known state."]
    o.workers["gateway"] = WorkerStatus("up")
    assert build_view(o, CFG, NOAH, EventTracker(), NOW)["registry"]["stale"] == "Last known state"


def test_before_security_joins_the_view_has_no_trace_of_it() -> None:
    """The start state: no Security chip, group, node, Endpoint, person, reviewer, or menu wording."""
    absent = observed(registry=Source(data=registry()))
    absent.absent_teams = {"security"}
    view = build_view(absent, CFG, NOAH, EventTracker(), NOW)
    assert "secur" not in json.dumps(view).lower()
    assert [w["name"] for w in view["topbar"]["workers"]] == ["gateway", "release-eng"]
    assert [g["team"] for g in view["registry"]["groups"]] == ["release-eng"]
    assert [t["team"] for t in view["calls"]["teams"]] == ["release-eng"]
    assert [p["name"] for p in view["topbar"]["people"]] == ["Noah Proovle"]
    assert view["topbar"]["reviewer"] == ""
    assert [(s["label"], s["done"]) for s in view["topbar"]["scenario"]["steps"]] == [
        ("Next: a new team joins", False),
        ("The new team publishes a tool", False),
        ("Platform adds a rule", False),
    ]
    assert scenario_step(view, "mandate")["blocked"] == "Comes after the step above it."

    joined = build_view(observed(registry=Source(data=registry())), CFG, NOAH, EventTracker(), NOW)
    assert [w["name"] for w in joined["topbar"]["workers"]] == ["gateway", "release-eng", "security"]
    assert [g["team_name"] for g in joined["registry"]["groups"]] == ["Release Engineering", "Security"]
    assert [(s["label"], s["done"]) for s in joined["topbar"]["scenario"]["steps"]][:2] == [
        ("Security joined", True),
        ("Next: publish Security tool", False),
    ]
