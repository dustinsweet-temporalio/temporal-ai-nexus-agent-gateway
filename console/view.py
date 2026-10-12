"""What the release console shows, built from what it observed in Temporal.

Pure functions, no I/O: `console/sources.py` reads the Workflows and fills an
`Observed`; `build_view` turns it into the JSON the browser renders. The console
is a presenter's view of one service (checkout-api), so unlike the gateway it may
read release-specific argument names such as `version` and `environment`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from gateway.models import PolicyDocument
from gateway.policy import gate_not_registered, unregistered_gate

PENDING = ("running", "awaiting_approval", "awaiting_gate")
DECIDED_SHOWN_SECONDS = 8  # the focus panel's decided state fades to idle after this
# A team added later has no accent of its own (each accent means one thing), so it draws neutral.
TEAM_TONES = {"platform": "violet", "checkout": "sky", "release-eng": "orange", "security": "pink"}
TOOL_LABELS = {
    "get_release_status": "Release status",
    "deploy": "Deploy",
    "review_release": "Security review",
}
WORKER_NAMESPACES = {"gateway": "platform", "release-eng": "release-eng", "security": "security"}
WORKER_TEAMS = {"gateway": "platform", "release-eng": "release-eng", "security": "security"}
# How a refusal by a Temporal Cloud Endpoint's access policy reads. It isn't yet confirmed
# against Temporal Cloud, so any permission-style failure matches.
ENDPOINT_REFUSAL_MARKERS = ("permission denied", "not allowed", "permissiondenied", "access denied")

STATUS_WORDS = {
    "running": ("Running", "violet", "loader-circle"),
    "awaiting_approval": ("Waiting for approval", "yellow", "hand"),
    "awaiting_gate": ("Waiting for review", "pink", "shield-check"),
    "succeeded": ("Succeeded", "green", "circle-check"),
    "denied": ("Denied", "coral", "ban"),
    "rejected": ("Rejected", "coral", "circle-x"),
    "failed": ("Failed", "coral", "triangle-alert"),
}


# ---------------------------------------------------------------- inputs


@dataclass
class Source:
    """One thing the console reads, with the last state it read successfully."""

    data: Any = None
    stale: bool = False  # the latest read failed; `data` is the last known state
    error: str = ""
    read_at: datetime | None = None


@dataclass
class WorkerStatus:
    status: str = "unknown"  # up | down | unknown
    detail: str = ""


@dataclass
class Observed:
    registry: Source = field(default_factory=Source)  # the registry's snapshot Query
    sessions: Source = field(default_factory=Source)  # list of session views
    session_ops: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # pending Nexus Operations
    ledger: Source = field(default_factory=Source)  # the ledger's status Query
    deploys: Source = field(default_factory=Source)  # list of {workflow_id, progress}
    desk: Source = field(default_factory=Source)  # the review desk's desk_view Query
    desk_activities: list[dict[str, Any]] = field(default_factory=list)  # pending Activities on the desk
    workers: dict[str, WorkerStatus] = field(default_factory=dict)
    supervisor_available: bool = False
    problems: list[dict[str, str]] = field(default_factory=list)
    absent_teams: set[str] = field(default_factory=set)  # teams with no Namespace yet: Security, at first


@dataclass
class Registration:
    """A team's registration file: who owns which tools, behind which Endpoint."""

    owner_team: str
    endpoint: str
    contract: str
    tools: list[str]


@dataclass
class ConsoleConfig:
    service: str
    profile: str  # local | cloud
    reviewer: str  # mock | claude
    reviewer_model: str
    namespaces: dict[str, str]  # team or profile name -> Namespace
    temporal_ui: str  # base URL, e.g. http://localhost:8233
    registrations: list[Registration]
    prices: dict[str, tuple[float, float]]  # model -> (input, output) dollars per million tokens
    mode_default: str = "normal"  # normal | record
    cloud: bool = False
    # config/principals.yaml, until the registry is read
    directory: dict[str, Any] = field(default_factory=dict)
    mandate: PolicyDocument | None = None  # the policy "Apply Security mandate" loads


# ---------------------------------------------------------------- small helpers


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = parse_time(start), parse_time(end)
    return (b - a).total_seconds() if a and b else None


def first_name(name: str) -> str:
    return name.split(" ")[0] if name else "Someone"


def person(directory: dict[str, Any], email: str) -> dict[str, Any] | None:
    return next((p for p in directory.get("people", []) if p["email"] == email), None)


def name_of(directory: dict[str, Any], email: str) -> str:
    found = person(directory, email)
    return found["name"] if found else email


def team_name(directory: dict[str, Any], team: str) -> str:
    return directory.get("teams", {}).get(team, team)


def release_arrow(arguments: dict[str, Any]) -> str:
    """'checkout-api 2.3.0 → production' when the arguments name a release."""
    service, version, env = (arguments.get(k) for k in ("service", "version", "environment"))
    if service and version and env:
        return f"{service} {version} → {env}"
    if service and version:
        return f"{service} {version}"
    return " ".join(str(v) for v in arguments.values())


def workflow_link(cfg: ConsoleConfig, namespace_key: str, workflow_id: str) -> str:
    namespace = cfg.namespaces.get(namespace_key, namespace_key)
    return f"{cfg.temporal_ui}/namespaces/{namespace}/workflows/{workflow_id}"


def all_calls(o: Observed) -> list[dict[str, Any]]:
    """Every call in every running session, newest first."""
    calls = [c for view in (o.sessions.data or []) for c in view.get("calls", [])]
    return sorted(calls, key=lambda c: c.get("submitted_at") or "", reverse=True)


def gate_owner(cfg: ConsoleConfig, tool: str) -> Registration | None:
    return next((r for r in cfg.registrations if tool in r.tools), None)


# ---------------------------------------------------------------- policy


def effect_line(rule: dict[str, Any], directory: dict[str, Any], cfg: ConsoleConfig) -> dict[str, str]:
    effect = rule["effect"]
    if effect == "allow":
        return {"text": "allowed", "tone": "green", "icon": "circle-check"}
    if effect == "require_approval":
        role = rule.get("approver_role") or ""
        titles = [p["title"] for p in directory.get("people", []) if role in p.get("roles", [])]
        who = titles[0] if titles else role
        return {"text": f"{who} approves", "tone": "yellow", "icon": "hand"}
    if effect == "require_gate":
        owner = gate_owner(cfg, rule.get("gate") or "")
        team = owner.owner_team if owner else ""
        return {
            "text": f"{team_name(directory, team) or rule.get('gate')} reviews first",
            "tone": TEAM_TONES.get(team, "neutral"),
            "icon": "shield-check",
        }
    return {"text": "refused", "tone": "coral", "icon": "ban"}


def rule_subject(rule: dict[str, Any]) -> str:
    label = TOOL_LABELS.get(rule["tool"]) or str(rule["tool"])
    when = rule.get("when") or {}
    if set(when) == {"environment"}:
        return f"{label} to {when['environment']}"
    extra = ", ".join(f"{k} {v}" for k, v in when.items())
    return f"{label} ({extra})" if extra else label


def policy_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    snap = o.registry.data
    if not snap:
        return {
            "version": None,
            "rules": [],
            "blocked": [],
            "stale": stale_chip(o, "gateway", o.registry.stale),
        }
    directory = snap["directory"]
    registered = set(snap["tools"])
    rules = []
    for rule in snap["policy"]["rules"]:
        if rule["tool"] not in registered:
            continue  # a rule for a tool nobody has published yet has nothing to govern
        rules.append({"id": rule["id"], "subject": rule_subject(rule), **effect_line(rule, directory, cfg)})
    blocked = [
        {
            "text": f"{release_arrow(b['fields'])} · rejected by {b['blocked_by_name']}",
            "reason": b["reason"],
            "at": b["at"],
        }
        for b in snap["blocked_releases"]
    ]
    return {
        "version": snap["policy"]["version"],
        "rules": rules,
        "blocked": blocked,
        "stale": stale_chip(o, "gateway", o.registry.stale),
    }


def production_rule(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snap:
        return None
    return next(
        (
            r
            for r in snap["policy"]["rules"]
            if r["tool"] == "deploy" and (r.get("when") or {}).get("environment") == "production"
        ),
        None,
    )


# ---------------------------------------------------------------- registry


def registry_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    snap = o.registry.data
    tools = snap["tools"] if snap else {}
    directory = snap["directory"] if snap else {}
    policy_rules = snap["policy"]["rules"] if snap else []
    groups = []
    for reg in cfg.registrations:
        rows = []
        for name in reg.tools:
            entry = tools.get(name)
            if entry is None:
                continue
            gated = next(
                (r["effect"] for r in policy_rules if r["tool"] == name and r["effect"] != "allow"), None
            )
            rows.append(
                {
                    "name": name,
                    "mode": entry["mode_hint"],
                    "gate_icon": {"require_approval": "hand", "require_gate": "shield-check"}.get(
                        gated or ""
                    ),
                    "description": entry["description"],
                    "input_schema": entry["input_schema"],
                    "contract": entry["contract"],
                    "registered_by": entry.get("registered_by", ""),
                }
            )
        groups.append(
            {
                "team": reg.owner_team,
                "team_name": team_name(directory, reg.owner_team),
                "tone": TEAM_TONES.get(reg.owner_team, "neutral"),
                "endpoint": reg.endpoint,
                "tools": rows,
            }
        )
    return {
        "version": snap["registry_version"] if snap else None,
        "groups": groups,
        "stale": stale_chip(o, "gateway", o.registry.stale),
        "missing": snap is None and not o.registry.stale,
    }


# ---------------------------------------------------------------- Security's review


def pending_activity(o: Observed, step: str) -> dict[str, Any] | None:
    return next((a for a in o.desk_activities if a.get("activity_type") == step), None)


def review_steps(o: Observed, review: dict[str, Any]) -> list[dict[str, Any]]:
    worker_down = o.workers.get("security", WorkerStatus()).status == "down"
    steps = []
    for s in review["steps"]:
        status = s["status"]
        # A running step's attempt count lives on the pending Activity, which the
        # server reports even while no Worker is there to answer a Query.
        pending = pending_activity(o, s["step"]) if status == "running" else None
        attempt = (pending or {}).get("attempt") or s.get("attempt") or 1
        if status == "running" and worker_down:
            status = "waiting_worker"
        steps.append(
            {
                "step": s["step"],
                "label": s["label"],
                "status": status,
                "attempt": attempt,
                # scheduled (waiting for a Worker to take it) or started (a Worker has it)
                "activity_state": (pending or {}).get("state"),
                "seconds": seconds_between(s.get("started_at"), s.get("completed_at")),
                # The mock reviewer spends no tokens, so it shows none; a real model's count shows.
                "tokens": None
                if s.get("model") in ("", "mock")
                else (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0),
                "model": s.get("model", ""),
                "started_at": s.get("started_at"),
            }
        )
    return steps


def review_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings so far: those of the latest finished step, most severe first."""
    done = [s for s in review["steps"] if s["status"] == "done"]
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings = done[-1]["findings"] if done else []
    return sorted(findings, key=lambda f: order.get(f["severity"], 9))


def latest_review(o: Observed) -> dict[str, Any] | None:
    reviews = (o.desk.data or {}).get("reviews") or []
    return reviews[0] if reviews else None


def stale_note(o: Observed, worker: str, team: str) -> str:
    """The full sentence, for the focus panel."""
    if o.workers.get(worker, WorkerStatus()).status == "down":
        return f"{team} Worker is down. Showing the last known state."
    return f"Can't read {team}'s state right now. Showing the last known state."


def stale_chip(o: Observed, worker: str, stale: bool) -> str | None:
    """The short marker a card shows when it is showing the last known state."""
    if not stale:
        return None
    return "Worker down" if o.workers.get(worker, WorkerStatus()).status == "down" else "Last known state"


def stale_notes(o: Observed) -> list[str]:
    """Every source showing its last known state, as full sentences (one per Worker)."""
    notes = []
    for worker, team, sources in [
        ("gateway", "The gateway", (o.registry, o.sessions)),
        ("release-eng", "Release Engineering", (o.ledger, o.deploys)),
        ("security", "Security", (o.desk,)),
    ]:
        if any(src.stale for src in sources):
            notes.append(stale_note(o, worker, team))
    return notes


def review_card(o: Observed, cfg: ConsoleConfig, review: dict[str, Any]) -> dict[str, Any]:
    directory = (o.registry.data or {}).get("directory", {})
    recommendation = None
    if review.get("recommendation"):
        rec = review["recommendation"].capitalize()
        recommendation = f"{rec} · {review['risk']} risk" if review.get("risk") else rec
    deciding = (o.desk.data or {}).get("deciding_team", "security")
    card = {
        "service": review["service"],
        "version": review["version"],
        "subject": f"{review['service']} {review['version']}",
        "status": review["status"],
        "steps": review_steps(o, review),
        "findings": review_findings(review),
        "recommendation": recommendation,
        "summary": review.get("summary") or "",
        "failure": review.get("failure"),
        "stale": o.desk.stale,
        "link": workflow_link(cfg, "security", f"review-desk-{review['service']}"),
        "who": f"{team_name(directory, deciding)} team",
    }
    return card


# ---------------------------------------------------------------- focus panel


def retrying_focus(o: Observed, cfg: ConsoleConfig, directory: dict[str, Any]) -> dict[str, Any] | None:
    """A pending Nexus call that is failing to start: refused by an Endpoint, or retrying."""
    for session_id, ops in o.session_ops.items():
        for op in ops:
            failure = op.get("last_failure") or ""
            attempt = op.get("attempt") or 1
            state = op.get("state", "")
            if state == "started":
                continue  # it started: now it waits on the team, and earlier failures are history
            if attempt <= 1 and state not in ("backing_off", "blocked") and not failure:
                continue
            reg = next((r for r in cfg.registrations if r.endpoint == op.get("endpoint")), None)
            team = team_name(directory, reg.owner_team) if reg else op.get("endpoint", "")
            # The server counts finished attempts. While one is in flight (scheduled),
            # the attempt being made is the next one; while backing off, it shows when.
            in_flight = state == "scheduled"
            base = {
                "team": team,
                "endpoint": op.get("endpoint", ""),
                "attempt": attempt + 1 if in_flight else attempt,
                "in_flight": in_flight,
                "next_attempt_at": None if in_flight else op.get("next_attempt_at"),
                "failure": failure,
                "session_id": session_id,
            }
            if any(m in failure.lower() for m in ENDPOINT_REFUSAL_MARKERS):
                return {"state": "blocked_endpoint", **base, "retrying": state in ("backing_off", "blocked")}
            breaker = state == "blocked" and "circuit" in (op.get("blocked_reason") or "").lower()
            return {"state": "retrying", **base, "circuit_breaker": breaker}
    return None


def approval_focus(o: Observed, cfg: ConsoleConfig, directory: dict[str, Any]) -> dict[str, Any] | None:
    waiting = [c for c in all_calls(o) if c["status"] == "awaiting_approval"]
    if not waiting:
        return None
    call = waiting[-1]  # the oldest waiting call first
    args = call["arguments"]
    env = args.get("environment")
    role = call.get("approver_role") or ""
    titles = [p["title"] for p in directory.get("people", []) if role in p.get("roles", [])]
    who = " or ".join(titles) or role
    what = f"{env.capitalize()} {call['tool']}s" if env else f"{call['tool']} calls"
    return {
        "state": "approval",
        "call_id": call["call_id"],
        "title": f"Approve {env} {call['tool']}?" if env else f"Approve {call['tool']}?",
        "subject": release_arrow(args),
        "asked_by": f"Asked by {first_name(call['principal_name'])}'s agent, for {call['principal_name']}",
        "why": f"{what} need the {who}'s approval.",
        "rule": f"rule {call['rule_id']} · policy v{call['policy_version']}",
        "who": who,
        "session_link": workflow_link(cfg, "platform", call["session_id"]),
    }


def latest_decision(o: Observed) -> dict[str, Any] | None:
    """The most recent decision by a person: an approval or a Security review decision."""
    directory = (o.registry.data or {}).get("directory", {})
    found = []
    for c in all_calls(o):
        a = c.get("approval")
        if a and a.get("decided_at"):
            found.append(
                {
                    "kind": "approval",
                    "decision": a["decision"],
                    "subject": release_arrow(c["arguments"]),
                    "decider": a["decided_by_name"],
                    "decider_title": (person(directory, a["decided_by"]) or {}).get("title", ""),
                    "at": a["decided_at"],
                    "reason": a.get("reason", ""),
                }
            )
    for r in (o.desk.data or {}).get("reviews") or []:
        if r.get("decided_at"):
            who = person(directory, r.get("decided_by") or "") or {}
            found.append(
                {
                    "kind": "review",
                    "decision": r["decision"],
                    "subject": f"{r['service']} {r['version']} · Security review",
                    "decider": who.get("name", r.get("decided_by")),
                    "decider_title": who.get("title", ""),
                    "at": r["decided_at"],
                    "reason": r.get("note", ""),
                }
            )
    return max(found, key=lambda d: d["at"]) if found else None


def in_flight_item(o: Observed, call: dict[str, Any]) -> dict[str, Any]:
    """One call still in flight, with its progress as the owning team reports it."""
    item: dict[str, Any] = {"call_id": call["call_id"], "text": call.get("description") or call["tool"]}
    args = call["arguments"]
    if call["status"] == "running":
        deploy = next(
            (
                d["progress"]
                for d in o.deploys.data or []
                if (d.get("progress") or {}).get("environment") == args.get("environment")
                and (d.get("progress") or {}).get("version") == args.get("version")
            ),
            None,
        )
        if deploy:
            return {
                **item,
                "status": deploy.get("label", "Running"),
                "percent": deploy.get("percent", 0),
                "tone": "orange",
            }
        return {**item, "status": "Running", "percent": None, "tone": "violet"}
    if call["status"] == "awaiting_approval":
        return {**item, "status": "Waiting for approval", "percent": None, "tone": "yellow"}
    team = (call.get("gate") or {}).get("team") or "Security"
    review = latest_review(o)
    if review and review["version"] == args.get("version") and review["status"] == "reviewing":
        done = sum(1 for s in review["steps"] if s["status"] == "done")
        total = len(review["steps"])
        return {
            **item,
            "status": f"{team} review, step {min(done + 1, total)} of {total}",
            "percent": round(100 * done / total),
            "tone": "pink",
        }
    return {**item, "status": f"Waiting for {team}'s review", "percent": None, "tone": "pink"}


def now_focus(o: Observed) -> dict[str, Any]:
    """Every call in flight, then the latest outcome, unless it is older than a call still in flight."""
    calls = all_calls(o)  # newest first
    pending = [c for c in calls if c["status"] in PENDING]
    finished = [c for c in calls if c["status"] not in PENDING]
    latest = finished[0] if finished else None
    if latest and pending:
        newest_pending = max(c["submitted_at"] for c in pending)
        if (latest.get("finished_at") or latest["submitted_at"]) < newest_pending:
            latest = None
    return {
        "state": "now",
        "in_flight": [in_flight_item(o, c) for c in pending],
        "last_outcome": latest["message"] if latest else "",
    }


def focus_view(o: Observed, cfg: ConsoleConfig, now: datetime) -> dict[str, Any]:
    """The one thing that needs attention, with every worker-down sentence the panel must show."""
    return {**choose_focus(o, cfg, now), "stale_notes": stale_notes(o)}


def choose_focus(o: Observed, cfg: ConsoleConfig, now: datetime) -> dict[str, Any]:
    directory = (o.registry.data or {}).get("directory", {})
    blocked = retrying_focus(o, cfg, directory)
    if blocked:
        return blocked
    approval = approval_focus(o, cfg, directory)
    if approval:
        return approval
    review = latest_review(o)
    if review and review["status"] in ("reviewing", "awaiting_decision"):
        state = "decision" if review["status"] == "awaiting_decision" else "review"
        return {"state": state, **review_card(o, cfg, review)}
    decision = latest_decision(o)
    decided_at = parse_time(decision["at"]) if decision else None
    if decision and decided_at and (now - decided_at).total_seconds() < DECIDED_SHOWN_SECONDS:
        return {"state": "decided", **decision}
    return now_focus(o)


# ---------------------------------------------------------------- pipeline


def deployed_line(env: dict[str, Any], directory: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": env["version"],
        "by": f"{name_of(directory, env['deployed_by'])}'s agent",
        "at": env["deployed_at"],
    }


def incoming_for(o: Observed, cfg: ConsoleConfig, environment: str) -> dict[str, Any] | None:
    live = ((o.ledger.data or {}).get("environments") or {}).get(environment, {}).get("version")
    for d in o.deploys.data or []:
        p = d.get("progress") or {}
        if p.get("version") == live:
            continue  # recorded on the ledger: it succeeded, and the card's value already shows it
        if p.get("environment") == environment and p.get("step") not in ("done", "failed"):
            return {
                "state": "deploying",
                "version": p.get("version"),
                "percent": p.get("percent", 0),
                "label": p.get("label", ""),
                "link": workflow_link(cfg, "release-eng", d["workflow_id"]),
            }
    for c in all_calls(o):
        if c["tool"] != "deploy" or c["arguments"].get("environment") != environment:
            continue
        version = c["arguments"].get("version")
        if c["status"] == "awaiting_approval":
            return {"state": "waiting_approval", "version": version, "label": "waiting for approval"}
        if c["status"] == "awaiting_gate":
            team = (c.get("gate") or {}).get("team") or "Security"
            return {"state": "waiting_review", "version": version, "label": f"waiting for {team}'s review"}
        if c["status"] == "running" and version != live:
            return {"state": "deploying", "version": version, "percent": 0, "label": "Starting"}
    return None


def security_stage(o: Observed, cfg: ConsoleConfig, gate_rule: dict[str, Any]) -> dict[str, Any]:
    directory = (o.registry.data or {}).get("directory", {})
    owner = gate_owner(cfg, gate_rule.get("gate") or "")
    team_key = owner.owner_team if owner else "security"
    stage: dict[str, Any] = {
        "id": team_key,
        "title": f"{team_name(directory, team_key)} review",
        "tone": TEAM_TONES.get(team_key, "neutral"),
        "link": workflow_link(cfg, team_key, f"review-desk-{cfg.service}"),
        "stale": stale_chip(o, team_key, o.desk.stale),
        "version": None,
        "line": "No review in flight",
    }
    review = latest_review(o)
    if review is None:
        return stage
    steps = review["steps"]
    done = sum(1 for s in steps if s["status"] == "done")
    stage["version"], stage["status"] = review["version"], review["status"]
    if review["status"] == "reviewing":
        current = next((s for s in steps if s["status"] != "done"), steps[-1])
        stage["line"] = f"Step {min(done + 1, len(steps))} of {len(steps)} · {current['label']}"
        stage["progress"] = round(100 * done / len(steps))
    elif review["status"] == "awaiting_decision":
        stage["line"] = f"Waiting for {team_name(directory, team_key)}'s decision"
        stage["progress"] = 100
    elif review["status"] == "decided":
        stage["line"] = (
            f"{(review['decision'] or '').capitalize()} by {name_of(directory, review['decided_by'])}"
        )
        stage["decision"] = review["decision"]
    else:
        stage["line"] = "The review failed"
        stage["decision"] = "failed"
    return stage


def pipeline_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    snap = o.registry.data
    directory = snap["directory"] if snap else {}
    ledger = o.ledger.data
    ledger_link = workflow_link(cfg, "release-eng", f"ledger-{cfg.service}")
    blocked_versions = {
        b["fields"].get("version")
        for b in (snap or {}).get("blocked_releases", [])
        if b["fields"].get("service") == cfg.service and b["fields"].get("environment") == "production"
    }
    candidates = [
        {"version": c["version"], "summary": c["summary"], "blocked": c["version"] in blocked_versions}
        for c in (ledger or {}).get("candidates", [])
    ]
    envs = (ledger or {}).get("environments", {})
    rule = production_rule(snap)
    stages: list[dict[str, Any]] = [{"id": "candidates", "title": "Candidates", "link": ledger_link}]
    for env in ("staging", "production"):
        if env == "production" and rule and rule["effect"] == "require_gate":
            stages.append({"kind": "gate", **security_stage(o, cfg, rule)})
        stage: dict[str, Any] = {
            "id": env,
            "title": env.capitalize(),
            "link": ledger_link,
            "deployed": deployed_line(envs[env], directory) if env in envs else None,
            "incoming": incoming_for(o, cfg, env),
        }
        if env == "production" and rule and rule["effect"] != "allow":
            stage["badge"] = effect_line(rule, directory, cfg)
        stages.append(stage)
    return {
        "service": cfg.service,
        "candidates": candidates,
        "stages": stages,
        "stale": stale_chip(o, "release-eng", o.ledger.stale or o.deploys.stale),
        "missing": ledger is None and not o.ledger.stale,
    }


# ---------------------------------------------------------------- live calls


def client_label(client_name: str) -> str:
    lowered = client_name.lower()
    if "claude" in lowered:
        return "Claude Code"
    if lowered == "scripted-agent":
        return "Scripted agent"
    return client_name or "Unknown client"


def call_line(call: dict[str, Any]) -> str:
    desc = call.get("description") or call["tool"]
    status = call["status"]
    took = seconds_between(call.get("submitted_at"), call.get("finished_at"))
    handed = call.get("handed_back_after_seconds")
    handed_text = f" · handed back to the agent after {handed:g}s" if handed else ""
    if status == "succeeded":
        if took is None:
            return f"{desc} · succeeded{handed_text}"
        return f"{desc} · succeeded in {'under 0.1s' if took < 0.1 else f'{took:.1f}s'}{handed_text}"
    if status == "running":
        return f"{desc} · running{handed_text}"
    if status == "awaiting_approval":
        return f"{desc} · held for approval"
    if status == "awaiting_gate":
        team = (call.get("gate") or {}).get("team") or "Security"
        return f"{desc} · held for {team} review"
    if status == "rejected":
        decider = (call.get("approval") or {}).get("decided_by_name") or (call.get("gate") or {}).get("team")
        return f"{desc} · rejected by {decider}" if decider else f"{desc} · rejected"
    if status == "denied":
        return f"{desc} · denied by the gateway"
    return f"{desc} · failed"


def team_of_endpoint(cfg: ConsoleConfig, endpoint: str | None) -> str | None:
    reg = next((r for r in cfg.registrations if r.endpoint == endpoint), None)
    return reg.owner_team if reg else None


def calls_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    snap = o.registry.data or {}
    directory = snap.get("directory", {})
    tools = snap.get("tools", {})
    views = sorted(
        o.sessions.data or [],
        key=lambda v: max((c.get("submitted_at") or "" for c in v.get("calls", [])), default=""),
        reverse=True,
    )
    active = views[0] if views else None
    calls = all_calls(o)
    pending = [c for c in calls if c["status"] in PENDING]
    busy: set[str] = set()
    for c in pending:
        gate = c.get("gate")
        if c["status"] == "awaiting_gate" and gate and gate.get("status") == "running":
            busy.add(team_of_endpoint(cfg, gate.get("endpoint")) or "")
        elif c["status"] == "running" and c.get("endpoint"):
            busy.add(team_of_endpoint(cfg, c.get("endpoint")) or "")
    default_person = (directory.get("people") or [{}])[0]
    principal_name = active["principal_name"] if active else default_person.get("name", "")
    teams = []
    for reg in cfg.registrations:
        teams.append(
            {
                "team": reg.owner_team,
                "name": team_name(directory, reg.owner_team),
                "tone": TEAM_TONES.get(reg.owner_team, "neutral"),
                "namespace": cfg.namespaces.get(reg.owner_team, reg.owner_team),
                "endpoint": reg.endpoint,
                "tools": [t for t in reg.tools if t in tools],
                "busy": reg.owner_team in busy,
            }
        )
    return {
        "agent": {
            "label": f"{first_name(principal_name)}'s agent",
            "client": client_label(active["client_name"]) if active else "No session yet",
        },
        "gateway": {
            "session_id": active["session_id"] if active else None,
            "link": workflow_link(cfg, "platform", active["session_id"]) if active else None,
            "waiting_on_person": any(c["status"] == "awaiting_approval" for c in pending),
        },
        "teams": teams,
        "line": call_line(calls[0]) if calls else "No calls yet",
        "stale": stale_chip(o, "gateway", o.sessions.stale),
    }


class EventTracker:
    """Turns call state changes into diagram animations: a dot out, a pulse back.

    The first observation only primes it, so opening the console never replays
    calls that already happened.
    """

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str], str] = {}
        self.next_id = 1
        self.primed = False
        self.recent: list[dict[str, Any]] = []

    def legs(self, call: dict[str, Any], cfg: ConsoleConfig) -> dict[tuple[str, str], tuple[str, str | None]]:
        """Each leg of a call -> (phase, target team). Phases: submitted, out, back."""
        cid = call["call_id"]
        legs: dict[tuple[str, str], tuple[str, str | None]] = {(cid, "submit"): ("submitted", "platform")}
        gate = call.get("gate")
        if gate:
            team = team_of_endpoint(cfg, gate.get("endpoint"))
            legs[(cid, "gate")] = ("out" if gate.get("status") == "running" else "back", team)
        gate_failed = bool(gate and gate.get("status") in ("failed", "rejected", "running"))
        if call["status"] == "running" and call.get("endpoint") and not gate_failed:
            legs[(cid, "call")] = ("out", team_of_endpoint(cfg, call.get("endpoint")))
        elif call["status"] in ("succeeded", "failed") and call.get("endpoint") and not gate_failed:
            legs[(cid, "call")] = ("back", team_of_endpoint(cfg, call.get("endpoint")))
        return legs

    def update(self, calls: list[dict[str, Any]], cfg: ConsoleConfig) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        for call in reversed(calls):  # oldest first, so events keep their order
            for key, (phase, team) in self.legs(call, cfg).items():
                before = self.seen.get(key)
                self.seen[key] = phase
                if not self.primed or before == phase:
                    continue
                if key[1] == "submit":
                    new.append({"kind": "submit", "team": "platform"})
                    continue
                if before is None:
                    new.append({"kind": "out", "team": team})
                if phase == "back":
                    ok = call["status"] == "succeeded" or (call.get("gate") or {}).get("status") == "approved"
                    new.append({"kind": "back", "team": team, "ok": ok if key[1] == "call" else True})
        self.primed = True
        for event in new:
            event["id"] = self.next_id
            self.next_id += 1
        self.recent = (self.recent + new)[-20:]
        return self.recent


# ---------------------------------------------------------------- audit and cost


def audit_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    snap = o.registry.data or {}
    rows: list[dict[str, Any]] = []
    calls = all_calls(o)
    for c in calls:
        word, tone, icon = STATUS_WORDS.get(c["status"], (c["status"], "violet", "circle-dashed"))
        if c["status"] == "rejected" and c.get("approval"):
            word = f"Rejected by {c['approval']['decided_by_name']}"
        rows.append(
            {
                "kind": "call",
                "time": c["submitted_at"],
                "who": c["principal_name"],
                "via": "via agent",
                "tool": c["tool"],
                "summary": c.get("description") or c["tool"],
                "outcome": word,
                "tone": tone,
                "icon": icon,
                "details": {
                    "Policy version": f"v{c['policy_version']}" if c.get("policy_version") else "none",
                    "Rule": c.get("rule_id") or "none",
                    "Endpoint": c.get("endpoint") or "not dispatched",
                    "Service": c.get("service") or "",
                    "Operation": c.get("operation") or "",
                    "Session": c["session_id"],
                    "Call": c["call_id"],
                },
                "link": workflow_link(cfg, "platform", c["session_id"]),
            }
        )
        gate = c.get("gate")
        if gate:
            started = next(
                (h["at"] for h in c["history"] if h["status"] == "awaiting_gate"), c["submitted_at"]
            )
            gate_word = {
                "running": ("Reviewing", "pink", "shield-check"),
                "approved": ("Approved", "green", "circle-check"),
                "rejected": ("Rejected", "coral", "circle-x"),
                "failed": ("Failed", "coral", "triangle-alert"),
            }[gate["status"]]
            decided_by = (gate.get("result") or {}).get("decided_by")
            word = gate_word[0]
            if decided_by and gate["status"] in ("approved", "rejected"):
                word = f"{word} by {name_of(snap.get('directory', {}), decided_by)}"
            rows.append(
                {
                    "kind": "gate",
                    "time": started,
                    "who": f"Gateway, for {c['principal_name']}",
                    "via": "",
                    "tool": gate["tool"],
                    "summary": f"gate for {c.get('description') or c['tool']}",
                    "outcome": word,
                    "tone": gate_word[1],
                    "icon": gate_word[2],
                    "details": {
                        "Rule": c.get("rule_id") or "",
                        "Endpoint": gate["endpoint"],
                        "Service": gate["service"],
                        "Operation": gate["operation"],
                        "Session": c["session_id"],
                        "Call": c["call_id"],
                    },
                    "link": workflow_link(cfg, "platform", c["session_id"]),
                }
            )
    for change in snap.get("policy_history", []):
        rows.append(
            {
                "kind": "scenario",
                "time": change["changed_at"],
                "who": change["changed_by"],
                "via": "",
                "tool": f"policy v{change['version']}",
                "summary": change["summary"],
                "outcome": "Applied",
                "tone": "violet",
                "icon": "scroll-text",
                "details": {"Registry": "tool-registry"},
                "link": workflow_link(cfg, "platform", "tool-registry"),
            }
        )
    for entry in snap.get("tools", {}).values():
        if entry.get("registered_by") in ("", "setup") or entry.get("registered_principal") == "setup":
            continue  # the start state, not a change made during the demo
        # The team comes from the registration Endpoint the call arrived on; the principal is
        # what the team's publish Workflow said.
        through = entry.get("registered_through", "")
        rows.append(
            {
                "kind": "scenario",
                "time": entry["registered_at"],
                "who": entry["registered_by"],
                "via": entry.get("registered_principal", ""),
                "tool": entry["name"],
                "summary": f"registered behind {entry['endpoint']}",
                "outcome": "Published",
                "tone": TEAM_TONES.get(entry["owner_team"], "neutral"),
                "icon": "plug",
                "details": {
                    "Endpoint": entry["endpoint"],
                    **({"Registered through": through} if through else {}),
                    "Published by": entry.get("registered_principal", "") or entry["registered_by"],
                    "Contract": entry["contract"],
                },
                "link": workflow_link(cfg, "platform", "tool-registry"),
            }
        )
    for row in rows:
        row["who"] = row["who"][:1].upper() + row["who"][1:]
    rows.sort(key=lambda r: r["time"] or "", reverse=True)
    call_count = sum(1 for r in rows if r["kind"] in ("call", "gate"))
    return {"rows": rows, "calls": call_count, "stale": o.sessions.stale or o.registry.stale}


def cost_view(o: Observed, cfg: ConsoleConfig) -> dict[str, Any]:
    tokens = 0
    dollars = 0.0
    models: set[str] = set()
    for review in (o.desk.data or {}).get("reviews") or []:
        for step in review["steps"]:
            i, out = step.get("input_tokens") or 0, step.get("output_tokens") or 0
            tokens += i + out
            if step.get("model"):
                models.add(step["model"])
            price = cfg.prices.get(step.get("model") or "")
            if price:
                dollars += i * price[0] / 1_000_000 + out * price[1] / 1_000_000
    mock = (models <= {"mock"}) if models else cfg.reviewer == "mock"
    if "security" in o.absent_teams:  # no reviewer exists yet, so the meter names none
        text = "Model cost · $0.00 · 0 tokens"
    elif mock and tokens == 0:
        text = "Mock reviewer · $0.00 · 0 tokens"
    else:
        text = f"Reviewer tokens: {tokens:,} · ${dollars:.2f}"
    return {"text": text, "tokens": tokens, "dollars": round(dollars, 4), "stale": o.desk.stale}


# ---------------------------------------------------------------- top bar


def reviewer_pill(o: Observed, cfg: ConsoleConfig) -> str:
    """What the Security Worker actually ran, when a step says; else what .env asks for."""
    models = [
        s["model"]
        for r in (o.desk.data or {}).get("reviews") or []
        for s in r["steps"]
        if s.get("model") and s["status"] == "done"
    ]
    model = models[-1] if models else ("mock" if cfg.reviewer == "mock" else cfg.reviewer_model)
    return "Mock reviewer" if model == "mock" else f"Claude reviewer · {model}"


def mandate_blocked(cfg: ConsoleConfig, tools: dict[str, Any]) -> str | None:
    if cfg.mandate is None:
        return None
    gate = unregistered_gate(cfg.mandate, tools)
    return gate_not_registered(cfg.mandate.version, gate) if gate else None


def scenario_steps(
    o: Observed, cfg: ConsoleConfig, tools: dict[str, Any], rule: dict[str, Any] | None
) -> list[Any]:
    """The Scenario menu, in order. A step reads neutrally until it's used, so the menu doesn't tell the story."""
    joined = "security" not in o.absent_teams
    published = any(t.get("owner_team") == "security" for t in tools.values())
    applied = bool(rule and rule["effect"] == "require_gate")
    # Publish before mandate: policy can't require a gate that isn't on the gateway.
    blocked = None if applied else mandate_blocked(cfg, tools)
    steps: list[dict[str, Any]] = [
        {"id": "join", "done": joined, "done_label": "Joined",
         "label": "Security joined" if joined else "A new team joins"},
        {"id": "publish", "done": published, "done_label": "Published",
         "label": "Security tool published" if published else
                  "Publish Security tool" if joined else "The new team publishes a tool"},
        {"id": "mandate", "done": applied, "done_label": "Applied",
         "label": "Security mandate applied" if applied else
                  "Apply Security mandate" if published else "Platform adds a rule",
         "blocked": (blocked if joined else "Comes after the step above it.") if blocked else None,
         "blocked_short": ("publish first" if joined else "after the step above") if blocked else None},
    ]  # fmt: skip
    upcoming = next((step for step in steps if not step["done"]), None)
    if upcoming:
        upcoming["label"] = f"Next: {upcoming['label'][:1].lower()}{upcoming['label'][1:]}"
    return steps


def without_absent_teams(o: Observed, cfg: ConsoleConfig) -> tuple[Observed, ConsoleConfig]:
    """A team that hasn't joined (Security, at first) shows nowhere: no tools, Endpoint, node, or people."""
    gone = o.absent_teams
    if not gone:
        return o, cfg

    def present(directory: dict[str, Any]) -> dict[str, Any]:
        return {
            **directory,
            "people": [p for p in directory.get("people", []) if p.get("team") not in gone],
            "teams": {k: v for k, v in directory.get("teams", {}).items() if k not in gone},
        }

    cfg = replace(
        cfg,
        registrations=[r for r in cfg.registrations if r.owner_team not in gone],
        directory=present(cfg.directory),
    )
    data = o.registry.data
    if data and data.get("directory"):
        o = replace(o, registry=replace(o.registry, data={**data, "directory": present(data["directory"])}))
    return o, cfg


def topbar_view(o: Observed, cfg: ConsoleConfig, signed_in: str) -> dict[str, Any]:
    snap = o.registry.data or {}
    directory = snap.get("directory") or cfg.directory
    people = [
        {"email": p["email"], "name": p["name"], "title": p["title"], "team": p["team"]}
        for p in directory.get("people", [])
    ]
    me = next((p for p in people if p["email"] == signed_in), None)
    rule = production_rule(snap or None)
    tools = snap.get("tools", {})
    workers = []
    for name in ("gateway", "release-eng", "security"):
        if WORKER_TEAMS[name] in o.absent_teams:
            continue
        status = o.workers.get(name, WorkerStatus())
        workers.append(
            {
                "name": name,
                "status": status.status,
                "detail": status.detail,
                "tone": TEAM_TONES[WORKER_TEAMS[name]],
            }
        )
    return {
        "service": cfg.service,
        "profile": "Temporal Cloud" if cfg.cloud else "Local dev server",
        "reviewer": "" if "security" in o.absent_teams else reviewer_pill(o, cfg),
        "workers": workers,
        "supervisor": o.supervisor_available,
        "scenario": {
            "steps": scenario_steps(o, cfg, tools, rule),
            "cloud_endpoint_link": None,  # Temporal Cloud only; not set up yet
        },
        "signed_in": me,
        "people": people,
    }


# ---------------------------------------------------------------- everything


def build_view(
    o: Observed, cfg: ConsoleConfig, signed_in: str, events: EventTracker, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    o, cfg = without_absent_teams(o, cfg)
    return {
        "mode_default": cfg.mode_default,
        "temporal_ui": cfg.temporal_ui,
        "topbar": topbar_view(o, cfg, signed_in),
        "registry": registry_view(o, cfg),
        "policy": policy_view(o, cfg),
        "pipeline": pipeline_view(o, cfg),
        "calls": {**calls_view(o, cfg), "events": events.update(all_calls(o), cfg)},
        "focus": focus_view(o, cfg, now),
        "audit": audit_view(o, cfg),
        "cost": cost_view(o, cfg),
        "problems": o.problems,
    }
