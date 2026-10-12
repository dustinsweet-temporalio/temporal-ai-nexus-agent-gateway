# Every target prints each raw command before it runs it (make's default echo),
# so you can copy any step and run it yourself.

# A port set in .env wins over the default; only that one line of .env is read.
env_value = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -n 1)
GATEWAY_PORT ?= $(or $(call env_value,GATEWAY_PORT),8080)
CONSOLE_PORT ?= $(or $(call env_value,CONSOLE_PORT),8090)
ACT ?= all
AUTO_DECIDE ?= 0
PAUSE ?= 0
KILL_DURING_REVIEW ?= 0
WORKER ?= security

.PHONY: help setup run demo failure recovery reset security-joins apply-mandate publish-security console mcp-command \
	generate check-contracts update-fixtures test lint

## help: list the targets
help:
	grep -E '^## ' Makefile | sed 's/^## /  make /'

## setup: install dependencies, check uv, the Temporal CLI, and NexGen, and create .env if it's missing
setup:
	command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	uv sync
	uv run python -m demo.check_tools
	test -f .env || cp .env.example .env

## run: start the dev server, the gateway and Release Engineering Workers, the front door, and the console; Ctrl+C stops it all
run:
	uv run python -m demo.supervisor run

## demo: play Act 1 and Act 2 through the front door (ACT=1|2, AUTO_DECIDE=1, PAUSE=1, KILL_DURING_REVIEW=1)
demo:
	ACT=$(ACT) AUTO_DECIDE=$(AUTO_DECIDE) PAUSE=$(PAUSE) KILL_DURING_REVIEW=$(KILL_DURING_REVIEW) uv run python -m demo.scripted_agent

## failure: kill a Worker with SIGKILL (WORKER=security, release-eng, or gateway)
failure:
	uv run python -m demo.supervisor kill $(WORKER)

## recovery: restart the Worker that failure killed (same WORKER)
recovery:
	uv run python -m demo.supervisor restart $(WORKER)

## reset: back to the demo's start state: stop everything, clear .demo-state/, fresh dev server, reseed
reset:
	uv run python -m demo.supervisor reset

## security-joins: scenario control, Security joins: its Namespace, its two Endpoints, and its Worker
security-joins:
	uv run python -m demo.supervisor join security

## publish-security: scenario control, Security registers review_release from its own Namespace (joins first if needed)
publish-security: security-joins
	uv run python -m security.publish contracts/security_review.registration.yaml --by "scenario control (Security)"

## apply-mandate: scenario control, Platform loads policy version 2 (after publish-security)
apply-mandate:
	uv run python -m demo.seed apply-mandate

## console: only the release console, for working on it (make run already starts it)
console:
	uv run python -m console.server

## mcp-command: print the command that connects Claude Code to the gateway as Noah
mcp-command:
	echo 'claude mcp add --transport http pinecrest-gateway http://localhost:$(GATEWAY_PORT)/mcp --header "Authorization: Bearer demo-token-noah"'

## generate: regenerate each team's Python types from its NexGen contract, Platform's included
generate:
	uv run python -m demo.check_tools --require-nexgen
	rm -rf release_eng/generated security/generated gateway/generated
	nexgen python contracts/release_engineering.nexusrpc.yaml --output release_eng/generated
	nexgen python contracts/security_review.nexusrpc.yaml --output security/generated
	nexgen python contracts/platform_registry.nexusrpc.yaml --output gateway/generated

## check-contracts: fail if generated code or the registry fixture drifted from the contracts
check-contracts:
	uv run python -m demo.check_tools --require-nexgen
	rm -rf .contract-check
	nexgen python contracts/release_engineering.nexusrpc.yaml --output .contract-check/release_eng
	nexgen python contracts/security_review.nexusrpc.yaml --output .contract-check/security
	nexgen python contracts/platform_registry.nexusrpc.yaml --output .contract-check/gateway
	diff -r -x __pycache__ .contract-check/release_eng release_eng/generated || { echo "Generated code is out of date. Run: make generate"; exit 1; }
	diff -r -x __pycache__ .contract-check/security security/generated || { echo "Generated code is out of date. Run: make generate"; exit 1; }
	diff -r -x __pycache__ .contract-check/gateway gateway/generated || { echo "Generated code is out of date. Run: make generate"; exit 1; }
	rm -rf .contract-check
	uv run python -m gateway.contracts check

## update-fixtures: rewrite the registry fixture after an intended contract or registration change
update-fixtures:
	uv run python -m gateway.contracts update

## test: run the test suite except slow tests (offline, spends no tokens); uv run pytest -m slow runs those
test:
	uv run pytest -m "not slow"

## lint: lint, check formatting, and type-check
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
