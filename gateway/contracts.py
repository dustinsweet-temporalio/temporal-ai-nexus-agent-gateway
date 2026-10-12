"""Build the gateway's tool definitions from each team's NexGen contract.

The gateway imports no code from any team. It knows a tool only by its registry
entry: the Endpoint, Service, and Operation names to call over Nexus, plus the
input schema and description it serves to agents. All of that comes from two
files the owning team keeps side by side in contracts/: its NexGen contract
(`*.nexusrpc.yaml`) and its registration file (`*.registration.yaml`, routing and
timeouts). Nothing a team needs to publish lives in a Platform-owned file.

NexGen generates Python types and validators for the teams, but it does not emit
JSON Schema, so this module reads the contract directly and resolves its local
`$ref`s into one self-contained Draft 2020-12 schema per tool.

Run `python -m gateway.contracts check` to confirm the committed registry fixture
still matches the contracts, or `... update` (`make update-fixtures`) to rewrite it
after an intended contract change.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"
REGISTRY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "registry_entries.json"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class ContractError(Exception):
    """A contract or tool registration can't be turned into a registry entry."""


def parse_contract(text: str, name: str) -> dict[str, Any]:
    contract = yaml.safe_load(text)
    if not isinstance(contract, dict) or "services" not in contract:
        raise ContractError(f"{name} is not a NexusRPC document (no `services` section)")
    return contract


def load_contract(path: Path) -> dict[str, Any]:
    return parse_contract(path.read_text(), str(path))


def contract_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_operation(contract: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    """Return (service wire name, operation definition) for an Operation's wire name.

    Wire names are the `fqn` values. Without an `fqn`, NexGen derives a different
    name than the key, so this sample requires one on every service and Operation.
    """
    for service_key, service in contract["services"].items():
        for op_key, op in service.get("operations", {}).items():
            if op.get("fqn") == name:
                if "fqn" not in service:
                    raise ContractError(f"service {service_key} has no `fqn`")
                return service["fqn"], op
            if op_key == name:
                raise ContractError(f"operation {op_key} needs `fqn: {name}`")
    raise ContractError(f"no operation with `fqn: {name}`")


def resolve_schema(contract: dict[str, Any], node: Any, _seen: tuple[str, ...] = ()) -> Any:
    """Inline every local `$ref` and drop `x-*` extension keywords."""
    if isinstance(node, list):
        return [resolve_schema(contract, item, _seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ContractError(f"only local `#/$defs/...` references are supported, got {ref}")
        if ref in _seen:
            raise ContractError(f"recursive reference {ref} can't be inlined")
        target = contract.get("$defs", {}).get(ref.removeprefix("#/$defs/"))
        if target is None:
            raise ContractError(f"{ref} is not defined")
        resolved = resolve_schema(contract, target, (*_seen, ref))
        # Keywords next to a $ref (such as a description) win over the target's.
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return {**resolved, **resolve_schema(contract, siblings, _seen)}
    return {
        key: resolve_schema(contract, value, _seen) for key, value in node.items() if not key.startswith("x-")
    }


def tool_input_schema(contract: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    if "input" not in operation:
        return {"$schema": DRAFT_2020_12, "type": "object", "additionalProperties": False}
    schema = {"$schema": DRAFT_2020_12, **resolve_schema(contract, copy.deepcopy(operation["input"]))}
    Draft202012Validator.check_schema(schema)
    return schema


TEAM_KEYS = {"contract", "owner_team", "endpoint", "tools"}
TOOL_KEYS = {"mode_hint", "schedule_to_close_seconds"}
OPTIONAL_TOOL_KEYS = {"summary"}


def registration_files() -> list[Path]:
    return sorted(CONTRACTS_DIR.glob("*.registration.yaml"))


def parse_registration(registration_file: str, text: str) -> dict[str, Any]:
    """Check one team's registration file: exactly the team keys, and each tool's keys."""
    name = Path(registration_file).name
    team = yaml.safe_load(text)
    if not isinstance(team, dict) or set(team) != TEAM_KEYS:
        raise ContractError(f"{name} must have exactly {sorted(TEAM_KEYS)}")
    for tool_name, tool in team["tools"].items():
        if not isinstance(tool, dict) or not TOOL_KEYS <= set(tool) <= TOOL_KEYS | OPTIONAL_TOOL_KEYS:
            raise ContractError(
                f"{name}: tool {tool_name} needs {sorted(TOOL_KEYS)} and may add {sorted(OPTIONAL_TOOL_KEYS)}"
            )
        if tool["mode_hint"] not in ("sync", "async"):
            raise ContractError(f"{name}: tool {tool_name} mode_hint must be sync or async")
    return team


def tool_registrations(registration_file: str, team: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A flat registration per tool, with the file paths as the team's repo names them."""
    contract = str(Path(registration_file).parent / team["contract"])
    return {
        name: {
            "owner_team": team["owner_team"],
            "endpoint": team["endpoint"],
            "contract": contract,
            "registration": registration_file,
            "mode_hint": tool["mode_hint"],
            "schedule_to_close_seconds": tool["schedule_to_close_seconds"],
            "summary_template": tool.get("summary", ""),
        }
        for name, tool in team["tools"].items()
    }


# region snippet:register-tool
def build_registry_entry(
    name: str, registration: dict[str, Any], contract: dict[str, Any], contract_sha: str
) -> dict[str, Any]:
    """Build a registry entry from the owning team's contract. No team code involved."""
    service, operation = find_operation(contract, name)
    return {
        "name": name,
        "service": service,
        "operation": name,
        "description": " ".join(operation["description"].split()),
        "input_schema": tool_input_schema(contract, operation),
        "contract_sha256": contract_sha,
        **registration,  # owner team, Endpoint, mode, timeout, summary, file paths
    }
    # endregion


def registry_entries_from_text(
    registration_file: str, registration_text: str, contract_text: str
) -> list[dict[str, Any]]:
    """Registry entries from a team's registration file and contract, as text.

    Platform's registration handler receives both files over Nexus, so it never reads a
    team's files from disk.
    """
    team = parse_registration(registration_file, registration_text)
    contract = parse_contract(contract_text, str(team["contract"]))
    sha = hashlib.sha256(contract_text.encode()).hexdigest()
    registrations = tool_registrations(registration_file, team)
    return [build_registry_entry(name, reg, contract, sha) for name, reg in registrations.items()]


def team_registry_entries(registration_file: Path) -> list[dict[str, Any]]:
    """Registry entries for one team's tools, from its registration file and the contract beside it."""
    path = registration_file.resolve()
    text = path.read_bytes().decode()
    contract = path.parent / parse_registration(path.name, text)["contract"]
    if not contract.exists():
        raise ContractError(f"{path.name}: contract {contract.name} is not beside it")
    return registry_entries_from_text(str(path.relative_to(REPO_ROOT)), text, contract.read_bytes().decode())


def derive_registry_entries() -> list[dict[str, Any]]:
    return [entry for path in registration_files() for entry in team_registry_entries(path)]


def _render(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, indent=2, sort_keys=True) + "\n"


def fixture_drift(fixture: Path = REGISTRY_FIXTURE) -> list[str]:
    """Describe every way the committed fixture differs from the current contracts."""
    if not fixture.exists():
        return [f"{fixture.relative_to(REPO_ROOT)} is missing"]
    committed = {e["name"]: e for e in json.loads(fixture.read_text())}
    derived = {e["name"]: e for e in derive_registry_entries()}
    problems = [
        f"tool {n} is in the fixture but in no contracts/*.registration.yaml"
        for n in committed.keys() - derived.keys()
    ]
    problems += [f"tool {n} is missing from the fixture" for n in derived.keys() - committed.keys()]
    for name in sorted(committed.keys() & derived.keys()):
        for key in sorted(committed[name].keys() | derived[name].keys()):
            if committed[name].get(key) != derived[name].get(key):
                problems.append(
                    f"tool {name}: `{key}` differs from what {derived[name]['contract']} produces"
                )
    return problems


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "check"
    if command == "update":
        REGISTRY_FIXTURE.write_text(_render(derive_registry_entries()))
        print(f"Wrote {REGISTRY_FIXTURE.relative_to(REPO_ROOT)}")
        return 0
    if command != "check":
        print("usage: python -m gateway.contracts [check|update]", file=sys.stderr)
        return 2
    problems = fixture_drift()
    for problem in problems:
        print(f"DRIFT: {problem}")
    if problems:
        print(
            "The registry fixture no longer matches the contracts. If the contract change is "
            "intended, run: make update-fixtures"
        )
        return 1
    print(f"Registry fixture matches the contracts ({len(derive_registry_entries())} tools).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


def argument_violations(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Every way the arguments break the tool's schema, one plain line each.

    The gateway checks arguments against the contract's schema before dispatch, so
    a malformed call is refused here with every problem listed, and never reaches
    the team's Namespace. The team's generated validators check again on arrival.
    """
    validator = Draft202012Validator(schema)
    problems = []
    for error in sorted(validator.iter_errors(arguments), key=lambda e: (list(e.absolute_path), e.message)):
        where = ".".join(str(p) for p in error.absolute_path) or "arguments"
        problems.append(f"{where}: {error.message}")
    return problems
