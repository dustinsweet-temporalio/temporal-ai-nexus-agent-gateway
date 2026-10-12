"""Policy: every rule in both versions, the blocked-release rule, and default deny. Pure, no Temporal."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.models import BlockedRelease, BlockSpec, PolicyDocument, PolicyRule
from gateway.policy import PolicyError, blocked_release_for, evaluate, load_policies, parse_policy

POLICIES = load_policies(Path(__file__).resolve().parent.parent / "config" / "policy.yaml")
V1, V2 = POLICIES[1], POLICIES[2]


def deploy(version: str, environment: str) -> dict[str, str]:
    return {"service": "checkout-api", "version": version, "environment": environment}


BLOCKED_230 = BlockedRelease(
    tool="deploy",
    fields={"service": "checkout-api", "version": "2.3.0", "environment": "production"},
    label="checkout-api 2.3.0 to production",
    blocked_by="noah@pinecrest.example",
    blocked_by_name="Noah Proovle",
    reason="2.3.0 has the tax rounding bug.",
    at="2026-10-03T17:42:05+00:00",
)


def test_the_file_holds_both_versions() -> None:
    assert sorted(POLICIES) == [1, 2]
    assert V1.default == V2.default == "deny"


@pytest.mark.parametrize(
    ("policy", "tool", "arguments", "effect", "rule_id"),
    [
        (V1, "get_release_status", {"service": "checkout-api"}, "allow", "status-read"),
        (V1, "deploy", deploy("2.3.1", "staging"), "allow", "deploy-staging"),
        (V1, "deploy", deploy("2.3.1", "production"), "require_approval", "deploy-production-approval"),
        (V1, "review_release", {"service": "checkout-api", "version": "2.4.0"}, "deny", "default"),
        (V2, "get_release_status", {"service": "checkout-api"}, "allow", "status-read"),
        (V2, "deploy", deploy("2.4.0", "staging"), "allow", "deploy-staging"),
        (V2, "deploy", deploy("2.4.0", "production"), "require_gate", "deploy-production-security-gate"),
        (V2, "review_release", {"service": "checkout-api", "version": "2.4.0"}, "allow", "review-on-request"),
        (V2, "something_new", {}, "deny", "default"),
    ],
)
def test_every_rule(
    policy: PolicyDocument, tool: str, arguments: dict[str, str], effect: str, rule_id: str
) -> None:
    decision = evaluate(policy, [], tool, arguments)
    assert (decision.effect, decision.rule_id, decision.policy_version) == (effect, rule_id, policy.version)


def test_v1_production_needs_the_checkout_lead() -> None:
    decision = evaluate(V1, [], "deploy", deploy("2.3.1", "production"))
    assert decision.approver_role == "checkout-lead"


def test_v2_production_is_gated_by_security_review() -> None:
    decision = evaluate(V2, [], "deploy", deploy("2.4.0", "production"))
    assert decision.gate == "review_release"


def test_default_deny_explains_itself() -> None:
    decision = evaluate(V1, [], "review_release", {"service": "checkout-api", "version": "2.4.0"})
    assert decision.reason == "Policy v1 has no rule that allows review_release with these arguments."


@pytest.mark.parametrize("policy", [V1, V2])
def test_a_blocked_release_is_denied_before_approval_or_gate(policy: PolicyDocument) -> None:
    decision = evaluate(policy, [BLOCKED_230], "deploy", deploy("2.3.0", "production"))
    assert (decision.effect, decision.rule_id, decision.policy_version) == (
        "deny",
        "blocked-release",
        policy.version,
    )


def test_the_blocked_release_message_reads_in_local_time() -> None:
    decision = evaluate(
        V1, [BLOCKED_230], "deploy", deploy("2.3.0", "production"), display_utc_offset_minutes=-420
    )
    assert decision.reason == (
        "checkout-api 2.3.0 to production was rejected by Noah Proovle at 10:42. "
        "A rejected release stays blocked; ship a new version instead."
    )


def test_a_block_covers_only_that_release() -> None:
    assert evaluate(V1, [BLOCKED_230], "deploy", deploy("2.3.0", "staging")).effect == "allow"
    assert evaluate(V1, [BLOCKED_230], "deploy", deploy("2.3.1", "production")).effect == "require_approval"


def test_invalid_policies_are_refused() -> None:
    with pytest.raises(PolicyError, match="require_gate needs gate"):
        parse_policy({"version": 3, "rules": [{"id": "x", "tool": "deploy", "effect": "require_gate"}]})
    with pytest.raises(PolicyError, match="rule IDs must be unique"):
        parse_policy(
            {
                "version": 3,
                "rules": [
                    {"id": "x", "tool": "a", "effect": "allow"},
                    {"id": "x", "tool": "b", "effect": "allow"},
                ],
            }
        )


def test_first_matching_rule_wins() -> None:
    policy = PolicyDocument(
        version=9,
        default="deny",
        rules=[PolicyRule("first", "deploy", "deny"), PolicyRule("second", "deploy", "allow")],
    )
    assert evaluate(policy, [], "deploy", deploy("2.3.1", "staging")).rule_id == "first"


# ---------------------------------------------------------------- blocks_release

PRODUCTION_RELEASE = BlockSpec(
    fields=["service", "version", "environment"], label="{service} {version} to {environment}"
)


def test_both_production_rules_declare_what_a_rejection_blocks() -> None:
    approval = evaluate(V1, [], "deploy", deploy("2.3.1", "production"))
    gate = evaluate(V2, [], "deploy", deploy("2.4.0", "production"))
    assert approval.blocks_release == gate.blocks_release == PRODUCTION_RELEASE


def test_rules_that_cannot_reject_declare_nothing() -> None:
    assert evaluate(V1, [], "deploy", deploy("2.3.1", "staging")).blocks_release is None
    assert evaluate(V1, [], "get_release_status", {"service": "checkout-api"}).blocks_release is None


def test_a_declared_rejection_blocks_the_declared_fields_with_the_declared_label() -> None:
    assert blocked_release_for(PRODUCTION_RELEASE, "deploy", deploy("2.3.0", "production")) == (
        {"service": "checkout-api", "version": "2.3.0", "environment": "production"},
        "checkout-api 2.3.0 to production",
    )


def test_a_rule_without_the_declaration_blocks_nothing() -> None:
    assert blocked_release_for(None, "deploy", deploy("2.3.0", "production")) is None


def test_a_block_matches_on_the_tool_and_every_recorded_field() -> None:
    assert evaluate(V1, [BLOCKED_230], "get_release_status", {"service": "checkout-api"}).effect == "allow"
    other_tool = BlockedRelease(**{**BLOCKED_230.__dict__, "tool": "rollback"})
    assert evaluate(V1, [other_tool], "deploy", deploy("2.3.0", "production")).effect == "require_approval"


def test_only_rules_that_can_reject_may_declare_blocks_release() -> None:
    block = {"fields": ["service"], "label": "{service}"}
    with pytest.raises(PolicyError, match="only a rule that can reject a call can declare blocks_release"):
        parse_policy(
            {
                "version": 3,
                "rules": [{"id": "x", "tool": "deploy", "effect": "allow", "blocks_release": block}],
            }
        )
    with pytest.raises(PolicyError, match="blocks_release needs fields and a label"):
        parse_policy(
            {
                "version": 3,
                "rules": [
                    {
                        "id": "x",
                        "tool": "deploy",
                        "effect": "require_approval",
                        "approver_role": "r",
                        "blocks_release": {},
                    }
                ],
            }
        )
