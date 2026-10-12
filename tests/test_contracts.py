"""Contracts: generated code, registry fixture, and gateway schemas all agree.

These tests run offline and call no model. The first one needs the pinned NexGen
binary on your PATH.
"""

from __future__ import annotations

import filecmp
import subprocess
from pathlib import Path
from typing import Any

import nexusrpc
import pytest
import temporalio.converter
from jsonschema import Draft202012Validator
from temporalio.exceptions import ApplicationError

from gateway import contracts
from release_eng.generated import DeployInput, ReleaseEngineering, ReleaseStatusInput
from security.generated import ReviewReleaseInput, SecurityReview

ROOT = contracts.REPO_ROOT
NEXGEN = "nexgen"
NEXGEN_VERSION = "0.2.5"

GENERATED = {
    "contracts/release_engineering.nexusrpc.yaml": ROOT / "release_eng" / "generated",
    "contracts/security_review.nexusrpc.yaml": ROOT / "security" / "generated",
}


def _same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return not mismatch and not errors and all(_same_tree(a / d, b / d) for d in cmp.common_dirs)


def test_generated_code_matches_contracts(tmp_path: Path) -> None:
    try:
        version = subprocess.run(
            [NEXGEN, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        pytest.fail(f"NexGen not found. Install {NEXGEN_VERSION} and put it on your PATH.")
    assert version == f"nexgen {NEXGEN_VERSION}", f"NexGen {NEXGEN_VERSION} is required, found {version!r}"
    for contract, committed in GENERATED.items():
        out = tmp_path / committed.parent.name
        subprocess.run([NEXGEN, "python", str(ROOT / contract), "--output", str(out)], check=True)
        assert _same_tree(out, committed), f"{committed.relative_to(ROOT)} is stale. Run: make generate"


def test_registry_fixture_matches_contracts() -> None:
    assert contracts.fixture_drift() == []


def test_each_team_publishes_from_its_own_files() -> None:
    """A team's registration file sits beside its contract and names only that contract."""
    files = contracts.registration_files()
    # The demo's two teams; a team added with docs/add-a-tool.md joins them.
    assert {"release_engineering.registration.yaml", "security_review.registration.yaml"} <= {
        f.name for f in files
    }
    for path in files:
        entries = contracts.team_registry_entries(path)
        assert entries, f"{path.name} registers no tools"
        expected_contract = path.name.replace(".registration.yaml", ".nexusrpc.yaml")
        assert {e["contract"] for e in entries} == {f"contracts/{expected_contract}"}
        assert {e["registration"] for e in entries} == {f"contracts/{path.name}"}


def test_wire_names_match_registry() -> None:
    """The names the gateway dispatches to are the names the handlers serve."""
    served = {}
    for service in (ReleaseEngineering, SecurityReview):
        definition = nexusrpc.get_service_definition(service)
        assert definition is not None
        for op in definition.operation_definitions.values():
            served[op.name] = definition.name
    demo_teams = {"release-eng", "security"}  # the teams whose generated code this test imports
    entries = [e for e in contracts.derive_registry_entries() if e["owner_team"] in demo_teams]
    assert {e["operation"]: e["service"] for e in entries} == served
    assert all(e["name"] == e["operation"] for e in entries)


def _properties(schema: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    found = []
    if isinstance(schema, dict):
        for name, prop in schema.get("properties", {}).items():
            found.append((f"{path}{name}", prop))
            found += _properties(prop, f"{path}{name}.")
        found += _properties(schema.get("items"), f"{path}[].")
    return found


@pytest.mark.parametrize("contract_file", sorted(GENERATED))
def test_every_field_has_a_description(contract_file: str) -> None:
    """Descriptions flow into the schema agents see, so none may be missing."""
    contract = contracts.load_contract(ROOT / contract_file)
    for service in contract["services"].values():
        for op in service["operations"].values():
            assert op.get("description"), f"{op['fqn']} has no description"
            for side in ("input", "output"):
                schema = contracts.resolve_schema(contract, op[side])
                for path, prop in _properties(schema):
                    assert prop.get("description"), f"{op['fqn']} {side}: {path} has no description"


# Arguments an agent might send, and whether the contract allows them.
CASES: list[tuple[str, type, dict[str, Any], bool]] = [
    ("get_release_status", ReleaseStatusInput, {"service": "checkout-api"}, True),
    ("get_release_status", ReleaseStatusInput, {"service": "inventory-api"}, False),
    ("get_release_status", ReleaseStatusInput, {}, False),
    ("deploy", DeployInput, {"service": "checkout-api", "version": "2.3.1", "environment": "staging"}, True),
    ("deploy", DeployInput, {"service": "checkout-api", "version": "2.3", "environment": "staging"}, False),
    ("deploy", DeployInput, {"service": "checkout-api", "version": "2.3.1", "environment": "moon"}, False),
    (
        "deploy",
        DeployInput,
        {"service": "checkout-api", "version": "2.3.1", "environment": "production", "force": True},
        False,
    ),
    ("review_release", ReviewReleaseInput, {"service": "checkout-api", "version": "2.4.0"}, True),
    ("review_release", ReviewReleaseInput, {"service": "checkout-api"}, False),
]


@pytest.mark.parametrize(("tool", "model", "arguments", "valid"), CASES)
async def test_gateway_schema_and_generated_validator_agree(
    tool: str, model: type, arguments: dict[str, Any], valid: bool
) -> None:
    """The gateway's check before dispatch and the handler's check give the same answer."""
    entry = next(e for e in contracts.derive_registry_entries() if e["name"] == tool)
    gateway_ok = Draft202012Validator(entry["input_schema"]).is_valid(arguments)

    # Decode the way a Worker does: the data converter applies NexGen's validators.
    converter = temporalio.converter.default()
    try:
        await converter.decode(await converter.encode([arguments]), [model])
        handler_ok = True
    except ApplicationError as err:
        assert err.type == "PayloadValidationError"
        handler_ok = False

    assert gateway_ok == handler_ok == valid
