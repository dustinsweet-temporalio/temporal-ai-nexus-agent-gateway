"""The tool checks `make setup` runs: uv, the Temporal CLI, and NexGen, each against its pinned version.

uv and the Temporal CLI are required; setup stops if either is missing or too old.
NexGen is needed only to regenerate code from the contracts (make generate and
make check-contracts); the generated code is committed, so a missing or different
NexGen is a warning with the install command, not a failure.

Run: uv run python -m demo.check_tools            (make setup)
     uv run python -m demo.check_tools --require-nexgen   (make generate, make check-contracts)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

UV_FLOOR = (0, 6, 0)
TEMPORAL_FLOOR = (1, 9, 1)
NEXGEN_PINNED = (0, 2, 5)
NEXGEN_RELEASE = "https://github.com/temporalio/nexgen/releases/tag/v0.2.5"


def version_of(command: list[str]) -> tuple[int, ...] | None:
    if shutil.which(command[0]) is None:
        return None
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
    return tuple(int(part) for part in match.groups()) if match else None


def dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def nexgen_message(found: tuple[int, ...] | None, required: bool) -> str:
    seen = dotted(found) if found else "not found"
    need = "is required" if required else "is needed only for make generate and make check-contracts"
    return (
        f"NexGen {dotted(NEXGEN_PINNED)} {need} (found: {seen}). Download the archive for your platform "
        f"from {NEXGEN_RELEASE}, extract it, and put nexgen on your PATH."
    )


def main() -> int:
    nexgen = version_of(["nexgen", "--version"])
    if "--require-nexgen" in sys.argv:  # make generate, make check-contracts
        if nexgen == NEXGEN_PINNED:
            return 0
        print(nexgen_message(nexgen, required=True))
        return 1

    failed = False
    uv = version_of(["uv", "--version"])
    if uv is None or uv < UV_FLOOR:
        found = dotted(uv) if uv else "not found"
        print(f"uv {dotted(UV_FLOOR)} or newer is required (found: {found}). "
              "Install or upgrade it: https://docs.astral.sh/uv/getting-started/installation/")  # fmt: skip
        failed = True
    else:
        print(f"uv {dotted(uv)}: ok")

    temporal = version_of(["temporal", "--version"])
    if temporal is None or temporal < TEMPORAL_FLOOR:
        found = dotted(temporal) if temporal else "not found"
        print(f"Temporal CLI {dotted(TEMPORAL_FLOOR)} or newer is required (found: {found}). "
              "Install or upgrade it: brew install temporal (macOS), or "
              "https://docs.temporal.io/cli#install-and-configure-the-cli")  # fmt: skip
        failed = True
    else:
        print(f"Temporal CLI {dotted(temporal)}: ok")

    print(
        f"NexGen {dotted(NEXGEN_PINNED)}: ok"
        if nexgen == NEXGEN_PINNED
        else nexgen_message(nexgen, required=False)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
