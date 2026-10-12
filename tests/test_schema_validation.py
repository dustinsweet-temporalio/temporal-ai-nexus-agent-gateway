"""Schema validation: the gateway checks every tool's arguments against its contract and lists every violation."""

from __future__ import annotations

from typing import Any

import pytest

from gateway.contracts import argument_violations, derive_registry_entries

SCHEMAS = {e["name"]: e["input_schema"] for e in derive_registry_entries()}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_release_status", {"service": "checkout-api"}),
        ("deploy", {"service": "checkout-api", "version": "2.3.1", "environment": "staging"}),
        ("deploy", {"service": "checkout-api", "version": "10.20.30", "environment": "production"}),
        ("review_release", {"service": "checkout-api", "version": "2.4.0"}),
    ],
)
def test_valid_arguments_pass(tool: str, arguments: dict[str, Any]) -> None:
    assert argument_violations(SCHEMAS[tool], arguments) == []


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("get_release_status", {}, ["arguments: 'service' is a required property"]),
        (
            "get_release_status",
            {"service": "inventory-api"},
            ["service: 'inventory-api' is not one of ['checkout-api']"],
        ),
        (
            "deploy",
            {"service": "checkout-api", "version": "2.3", "environment": "moon", "force": True},
            [
                "arguments: Additional properties are not allowed ('force' was unexpected)",
                "environment: 'moon' is not one of ['staging', 'production']",
                "version: '2.3' does not match '^[0-9]+\\\\.[0-9]+\\\\.[0-9]+$'",
            ],
        ),
        (
            "deploy",
            {"service": "checkout-api"},
            [
                "arguments: 'environment' is a required property",
                "arguments: 'version' is a required property",
            ],
        ),
        (
            "review_release",
            {"service": "checkout-api", "version": 240},
            ["version: 240 is not of type 'string'"],
        ),
    ],
)
def test_every_violation_is_listed(tool: str, arguments: dict[str, Any], expected: list[str]) -> None:
    assert argument_violations(SCHEMAS[tool], arguments) == expected
