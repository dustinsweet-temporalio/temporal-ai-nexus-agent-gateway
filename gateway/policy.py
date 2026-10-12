"""Policy evaluation: given a tool call, decide allow, deny, require_approval, or require_gate.

Pure functions with no Temporal imports, so the session Workflow can call them
deterministically and tests can cover every rule directly. The policy document
itself lives in config/policy.yaml and reaches the gateway through the registry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from gateway.models import BlockedRelease, BlockSpec, Effect, PolicyDocument, PolicyRule

BLOCKED_RELEASE_RULE = "blocked-release"
DEFAULT_RULE = "default"
EFFECTS: tuple[str, ...] = ("allow", "deny", "require_approval", "require_gate")


@dataclass
class PolicyDecision:
    effect: Effect
    rule_id: str
    policy_version: int
    approver_role: str | None = None
    gate: str | None = None
    blocks_release: BlockSpec | None = None
    reason: str = ""  # why a call is denied, in a sentence the agent can relay


def evaluate(
    policy: PolicyDocument,
    blocked: list[BlockedRelease],
    tool: str,
    arguments: dict[str, Any],
    display_utc_offset_minutes: int = 0,
) -> PolicyDecision:
    rule = next((r for r in policy.rules if _matches(r, tool, arguments)), None)
    if rule is None:
        return PolicyDecision(
            effect=policy.default,
            rule_id=DEFAULT_RULE,
            policy_version=policy.version,
            reason=f"Policy v{policy.version} has no rule that allows {tool} with these arguments."
            if policy.default == "deny"
            else "",
        )
    if rule.effect != "deny":
        block = _blocking(blocked, tool, arguments)
        if block is not None:
            return PolicyDecision(
                effect="deny",
                rule_id=BLOCKED_RELEASE_RULE,
                policy_version=policy.version,
                reason=blocked_release_message(block, display_utc_offset_minutes),
            )
    return PolicyDecision(
        effect=rule.effect,
        rule_id=rule.id,
        policy_version=policy.version,
        approver_role=rule.approver_role,
        gate=rule.gate,
        blocks_release=rule.blocks_release,
        reason=f"Policy v{policy.version} denies {tool} (rule {rule.id})." if rule.effect == "deny" else "",
    )


def blocked_release_message(block: BlockedRelease, display_utc_offset_minutes: int = 0) -> str:
    local = datetime.fromisoformat(block.at) + timedelta(minutes=display_utc_offset_minutes)
    return (
        f"{block.label} was rejected by {block.blocked_by_name} at {local:%H:%M}. "
        "A rejected release stays blocked; ship a new version instead."
    )


def _matches(rule: PolicyRule, tool: str, arguments: dict[str, Any]) -> bool:
    return rule.tool == tool and all(arguments.get(k) == v for k, v in rule.when.items())


def _blocking(blocked: list[BlockedRelease], tool: str, arguments: dict[str, Any]) -> BlockedRelease | None:
    """A recorded block this call matches: same tool, and every field the block recorded."""
    return next(
        (
            b
            for b in blocked
            if b.tool == tool and all(str(arguments.get(k)) == v for k, v in b.fields.items())
        ),
        None,
    )


def render(template: str, arguments: dict[str, Any]) -> str | None:
    """Fill a team's or a rule's template from the call's arguments; None if it doesn't fit."""
    try:
        return template.format_map({k: str(v) for k, v in arguments.items()})
    except (KeyError, IndexError, ValueError):
        return None


def blocked_release_for(
    spec: BlockSpec | None, tool: str, arguments: dict[str, Any]
) -> tuple[dict[str, str], str] | None:
    """What a rejected call blocks: the declared fields' values and the release's label.

    A rule without a `blocks_release` declaration blocks nothing. The gateway never
    guesses from argument names; the policy says which fields identify a release.
    """
    if spec is None or not all(f in arguments for f in spec.fields):
        return None
    fields = {f: str(arguments[f]) for f in spec.fields}
    return fields, render(spec.label, arguments) or f"{tool} {' '.join(fields.values())}"


# ---------------------------------------------------------------- loading


class PolicyError(Exception):
    """A policy document that can't be used."""


def parse_policy(raw: dict[str, Any]) -> PolicyDocument:
    rules = [
        PolicyRule(
            id=r["id"],
            tool=r["tool"],
            effect=r["effect"],
            when=dict(r.get("when") or {}),
            approver_role=r.get("approver_role"),
            gate=r.get("gate"),
            blocks_release=_block_spec(r["id"], r.get("blocks_release")),
        )
        for r in raw["rules"]
    ]
    document = PolicyDocument(version=int(raw["version"]), default=raw.get("default", "deny"), rules=rules)
    problems = policy_problems(document)
    if problems:
        raise PolicyError("; ".join(problems))
    return document


def _block_spec(rule_id: str, raw: Any) -> BlockSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("fields") or not raw.get("label"):
        raise PolicyError(f"rule {rule_id}: blocks_release needs fields and a label")
    return BlockSpec(fields=[str(f) for f in raw["fields"]], label=str(raw["label"]))


def policy_problems(document: PolicyDocument) -> list[str]:
    problems = []
    if document.default not in EFFECTS:
        problems.append(f"default must be one of {EFFECTS}")
    ids = [r.id for r in document.rules]
    if len(ids) != len(set(ids)):
        problems.append("rule IDs must be unique")
    for r in document.rules:
        if r.effect not in EFFECTS:
            problems.append(f"rule {r.id}: effect must be one of {EFFECTS}")
        if r.effect == "require_approval" and not r.approver_role:
            problems.append(f"rule {r.id}: require_approval needs approver_role")
        if r.effect == "require_gate" and not r.gate:
            problems.append(f"rule {r.id}: require_gate needs gate")
        if r.blocks_release is not None and r.effect not in ("require_approval", "require_gate"):
            problems.append(f"rule {r.id}: only a rule that can reject a call can declare blocks_release")
    return problems


def unregistered_gate(document: PolicyDocument, tools: Iterable[str]) -> str | None:
    """A gate this policy requires that isn't a registered tool, if any."""
    registered = set(tools)
    return next(
        (r.gate for r in document.rules if r.effect == "require_gate" and r.gate not in registered), None
    )


def gate_not_registered(version: int, gate: str) -> str:
    return f"Policy v{version} requires {gate}, which isn't registered yet. Publish it first."


def load_policies(path: Path) -> dict[int, PolicyDocument]:
    """Every version in a policy file, keyed by version number."""
    with path.open() as f:
        documents = [parse_policy(raw) for raw in yaml.safe_load_all(f) if raw]
    return {d.version: d for d in documents}
