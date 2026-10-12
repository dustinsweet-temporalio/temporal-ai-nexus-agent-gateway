"""The gateway knows teams only by their contracts: it imports no team code."""

from __future__ import annotations

import ast
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "gateway"
TEAM_PACKAGES = ("release_eng", "security")


def test_the_gateway_imports_nothing_from_any_team() -> None:
    offenders = []
    for path in sorted(GATEWAY.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] in TEAM_PACKAGES]
    assert offenders == []
