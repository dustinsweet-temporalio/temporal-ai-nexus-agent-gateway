# AGENTS.md

An AI agent gateway on Temporal with Temporal Nexus, for a fictional retailer, Pinecrest Outfitters. Agents reach tools only through the gateway (`gateway/`, Namespace `platform`), which applies policy, holds calls for a person's approval, and records every step. Each team owns its tools behind its own Nexus Endpoint: Release Engineering (`release_eng/`, Namespace `release-eng`) and Security (`security/`, Namespace `security`). Security isn't part of the start state; it joins during the demo. `README.md` explains the design and its limits, and `docs/add-a-tool.md` walks through adding a team's tool.

## Always

- Run `make test` (offline, no model calls) and `make lint` before proposing a change. `make lint` passes with `0 errors` from pyright.
- Run `make generate` after editing a contract in `contracts/`, then `make check-contracts`. Run `make update-fixtures` after an intended contract or registration change.
- Use `make run` to start the demo and `make demo AUTO_DECIDE=1` to play it unattended. `make reset` returns to the start state.
- Give each Operation in a contract an `fqn`, the snake_case name used on the wire, in the registry, and in policy. Operation keys are camelCase because NexGen requires it.
- Keep Workflow code deterministic: no I/O, randomness, or wall-clock reads.
- Give every Activity explicit timeouts and a retry policy.
- Derive backing execution IDs from the Nexus request ID (for example `deploy-<request id>`), and give every side effect that can repeat an idempotency key the receiving system checks.
- Send Workflow-to-Workflow Updates from an Activity, with a deterministic Update ID.
- Read a refused Update's reason from the error's `.message`, never `str()`, and pass it to the caller as is.
- Use a 1-second RPC timeout on every Query.
- Store times in Workflow state as RFC 3339 text, converted at the contract boundary.
- Declare `blocks_release` on a policy rule that can reject a call; the gateway never interprets argument names. A gate tool returns `decision` (`approved` or `rejected`) and `decided_by`.
- Have teams register tools through their own `platform-registry-<team>` Endpoint, with their own copy of `publish.py`. Platform reads the team from `nexus.info().endpoint`.
- Use the color tokens in the `:root` block of `console/web/styles.css`; `tests/test_console_style.py` fails on any other color.

## Ask first

- Changing an existing field in a contract.
- Changing what a policy rule means, or adding an effect.
- Adding a tool, a team, or a Namespace.
- Upgrading the pinned Temporal SDK, Temporal CLI, or NexGen versions.
- Changing the console's layout or what it shows.

## Never

- Connect team code to the platform Namespace, or import `release_eng/` or `security/` from the gateway.
- Authorize on Nexus headers or links; a caller can set its own.
- Register the startup smoke test's Endpoint (`gateway-smoke-check`) as a tool or draw it in the console.
- Hand-edit anything under `*/generated/`.
- Call a model in tests.
- Set an LLM client's retries above 0. Temporal owns retries.
- Commit `.env`, API keys, real account IDs, or real company names.
- Use em dashes in docs.
