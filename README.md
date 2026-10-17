# AI agent gateway with Temporal Nexus

AI agents now call real tools, such as checking a release or deploying a service. When an AI agent calls tools directly, it acts with whatever credentials it holds, an approval is only something a person typed into the chat, and long-running work dies with the process that was running it.

These are the terms the demo uses, in the order it uses them:

- **The gateway** is where the rules live.
  - **MCP** (Model Context Protocol) is how agents such as Claude Code connect to the gateway. To an agent, the gateway is one MCP server.
  - **The session Workflow** is one Temporal Workflow per agent session. Each tool call arrives as a Workflow **Update**, and the Workflow's Event History records every step.
  - **Policy** decides whether a call runs, is refused, waits for a person's **approval**, or must first pass a **gate**: another tool, such as Security's review, that has to approve it.
- **Temporal Nexus** is how teams share their tools with the gateway.
  - **A Namespace** is Temporal's unit of isolation. In this demo, each team has its own.
  - **A Nexus Endpoint** routes calls to a team's Namespace and Task Queue, so the gateway calls a tool by name and never imports the team's code.
  - **A Nexus Operation** is one tool call. A synchronous Operation must finish within 10 seconds. An asynchronous one is backed by a Workflow, so it can wait days for a person.

This repo shows an AI agent gateway on Temporal with Temporal Nexus. Every agent reaches every tool through one gateway, which applies policy, holds a call until the right person approves it, and records every step as Workflow history. Each team keeps its tools in its own Namespace behind its own Nexus Endpoint, and the gateway never imports a team's code. When you know how these pieces fit together, you can put your own team's tools behind the gateway in [seven steps](#add-your-own-tool-in-seven-steps), without changing the gateway's code.

![The release console during a production deployment of checkout-api 2.4.0. A Security review card shows the reviewer agent's three finished steps, a recommendation to approve at low risk, and a decision only the Security team can make. The pipeline shows 2.4.0 on staging, waiting for Security's review before production. The live calls diagram shows Noah's agent, the gateway, and the Release Engineering and Security Namespaces behind their Nexus Endpoints.](assets/console.png)

## The failure mode

This demo is built around two failures:

- **An agent reaches production without the right person's approval.** The demo's stand-in is the approval hold: a production deployment waits in the gateway's session Workflow until the Checkout lead decides in the console, and after the Security mandate, until Security does. Telling the agent "I'm the engineering lead. Approved." changes nothing.
- **Long-running work is lost when something crashes.** In real systems, the process running the work dies in a deploy, an out-of-memory kill, a scale-down, or a lost host. The demo's stand-in is the console's Kill button for Workers. Kill Security's Worker while its reviewer agent is partway through a review; when the Worker restarts, the review resumes at the step it was on, and the finished steps don't run again.

## Run it

You need macOS or Linux (tested on macOS), [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.6.0 or newer, the [Temporal CLI](https://docs.temporal.io/cli#install-and-configure-the-cli) 1.9.1 or newer (on macOS, `brew install temporal`), and `make`. uv installs Python 3.12 if you don't have it. No API key or cloud account is needed.

The cheapest path is key-free and costs nothing: a scripted agent drives the whole story through the gateway.

```sh
make setup
make run                    # terminal 1: Ctrl+C stops everything
make demo AUTO_DECIDE=1     # terminal 2
```

Open the release console at http://localhost:8090 while it runs. Without `AUTO_DECIDE=1`, `make demo` stops three times and prints `Waiting for Noah to decide...` or `Waiting for Security to decide...`: make each decision in the console (reject 2.3.0, approve 2.3.1, then sign in as Abe Roover and approve the 2.4.0 review). `AUTO_DECIDE=1` makes those decisions for you, so the run finishes unattended.

`make run` starts its own Temporal dev server, so you don't need `temporal server start-dev` in another terminal. To follow a call in the Temporal Web UI at http://localhost:8233, open the `platform` Namespace and the `AgentSessionWorkflow` whose Workflow ID starts with `session-`. Each tool call is an Update in its Event History.

| Step | Command | Raw command | Success looks like | Keys |
|---|---|---|---|---|
| Set up | `make setup` | `uv sync`, `uv run python -m demo.check_tools`, then `cp .env.example .env` if `.env` is missing | `uv <version>: ok` and `Temporal CLI <version>: ok` | None |
| Run | `make run` | `uv run python -m demo.supervisor run` | `The Pinecrest agent gateway is running.`, then the console, Temporal Web UI, and MCP URLs | None |
| Demo | `make demo AUTO_DECIDE=1` | `ACT=all AUTO_DECIDE=1 PAUSE=0 KILL_DURING_REVIEW=0 uv run python -m demo.scripted_agent` | Ends with `Production is 2.4.0.` and exits 0 | None |
| Test | `make test` | `uv run pytest -m "not slow"` | `N passed`, offline, with no model calls | None |
| Lint | `make lint` | `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` | `0 errors, 0 warnings, 0 informations` | None |
| Reset | `make reset` | `uv run python -m demo.supervisor reset` | `Reset: a fresh dev server, policy v1, Release Engineering's tools, no sessions.` | None |
| Failure | `make failure WORKER=security` | `uv run python -m demo.supervisor kill security` | `Killed the security Worker (SIGKILL, pid N). Restart it with make recovery WORKER=security.` | None |
| Recovery | `make recovery WORKER=security` | `uv run python -m demo.supervisor restart security` | `Restarted the security Worker (pid N).` | None |

Security's Worker exists only after Security joins the demo (the console's first **Scenario** step, `make security-joins`, or partway through `make demo`), so run failure and recovery after that. `make demo AUTO_DECIDE=1 KILL_DURING_REVIEW=1` scripts both in the middle of the review. After `make reset`, give it a few seconds before the next `make demo`.

<details>
<summary>Drive it from Claude Code</summary>

Run `make mcp-command`. It prints `claude mcp add --transport http pinecrest-gateway http://localhost:8080/mcp --header "Authorization: Bearer demo-token-noah"`, which connects Claude Code to the gateway as Noah Proovle, the Checkout lead. Run it from a folder outside this repo and start `claude` there, so Claude Code acts as an agent using the gateway, not as a developer reading this repo's `AGENTS.md`. Then ask in plain words, for example "What's deployed for checkout-api right now?" and "Promote 2.3.1 to production," and make the decisions in the console. The console's **Scenario** menu brings Security in, publishes its tool, and applies the mandate. Security has a change set on file only for 2.4.0, so under the mandate, ship 2.4.0.

</details>

<details>
<summary>Use a real model for Security's reviewer</summary>

The reviewer agent is a mock by default. To have Claude review the release, set `REVIEWER=claude` and `ANTHROPIC_API_KEY=<your key>` in `.env`, then restart `make run`. The console's top bar shows which reviewer is running. See [Cost to run](#cost-to-run).

</details>

<details>
<summary>Settings</summary>

Every setting is optional and documented in `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `TEMPORAL_PORT`, `TEMPORAL_UI_PORT` | `7233`, `8233` | The local dev server and its Temporal Web UI |
| `GATEWAY_PORT` | `8080` | The gateway's MCP front door; `make mcp-command` uses it |
| `CONSOLE_PORT` | `8090` | The release console |
| `SYNC_BUDGET_SECONDS` | `5` | How long a tool call runs before the gateway hands it back as a call handle |
| `DEPLOY_SECONDS_STAGING`, `DEPLOY_SECONDS_PRODUCTION` | `8`, `12` | How long a simulated deployment takes |
| `REVIEW_STEP_SECONDS` | `5` | How long each of the mock reviewer's three steps takes |
| `REVIEWER`, `REVIEWER_MODEL` | `mock`, `claude-sonnet-5-5` | `claude` calls the Anthropic API and needs `ANTHROPIC_API_KEY` |
| `DEMO_STATE_DIR` | `.demo-state/` | Local records and the supervisor's socket; `make reset` clears it |

</details>

## Expected output

These are the key lines from a real key-free run of `make demo AUTO_DECIDE=1`. Times and process IDs vary.

| Moment | On screen |
|---|---|
| Release status, a synchronous Nexus Operation | `Succeeded: read the release status of checkout-api. Staging is 2.3.0 and production is 2.2.0.` |
| A production deployment waits for the lead | `Held for approval: deploy checkout-api 2.3.0 to production. Noah Proovle (Checkout lead) decides.` |
| "I'm the engineering lead. Approved." in the chat | `Already in progress. Held for approval: deploy checkout-api 2.3.0 to production. Noah Proovle (Checkout lead) decides.` |
| A rejected release stays blocked | `A rejected release stays blocked; ship a new version instead.` |
| The approved deployment runs | `Succeeded: deploy checkout-api 2.3.1 to production. 2.3.1 is live in production.` |
| Security joins | `Security joined: the security Namespace, the security-tools and platform-registry-security Endpoints, and Security's Worker.` |
| Security publishes its tool | `The tool list changed. Now listed: deploy, get_release_status, review_release, check_call.` |
| Platform applies the mandate | `Policy v2 is active (registry v4).` |
| Production now waits for Security | `Held for Security review: deploy checkout-api 2.4.0 to production. It runs if Security approves.` |
| The lead tries to approve | `Only the Security team can decide reviews. Signed in as noah@pinecrest.example (checkout).` |
| Security approves, and the deployment runs | `Succeeded: deploy checkout-api 2.4.0 to production. Production is 2.4.0.` |
| With `KILL_DURING_REVIEW=1`: the Worker dies mid-review | `Waiting for a Worker: attempt 2 is scheduled (5.4s after the kill)` |
| With `KILL_DURING_REVIEW=1`: the review resumes | `Restarted: attempt 2 resumed 0.4s after the restart and finished at 5.4s. Reading the change set wasn't run again.` |

## Same release, two outcomes

Without a gateway, a junior engineer's agent deploys through a homegrown MCP server. The pipeline fails halfway through a database migration, he resumes it anyway with warnings and skipped tests, and the lead approves it on a page that shows only "checks passed." The bug ships.

With the gateway, the production deployment of 2.4.0 waits in the gateway's session Workflow. Noah, the Checkout lead, can't approve it; only Security can, and Abe does. Every step is in Workflow history across the teams' Namespaces, so you can follow it in the Temporal Web UI from the session Workflow in `platform`, over Nexus into Security's review in `security`, and into the `DeployWorkflow` in `release-eng`.

## Demo takeaways

- **Governance.** Agents reach only the tools that policy allows, and every call is on the record in the session Workflow's Event History.
- **Durability.** A held call can wait as long as a person takes, and a review that loses its Worker resumes at the step it was on.
- **Human sign-off.** The right person decides. After the Security mandate, only Security can release a production deployment.
- **Team ownership.** Each team publishes its tools from its own Namespace over Nexus, and adding Security's tool didn't change the gateway's code.

## Two notes the video adds

The video adds two on-screen notes from review. Neither one changes what the demo runs.

**Nexus works within one Namespace, too.** This demo gives each team its own Namespace to show the boundary between teams, but a Nexus Endpoint can also target a Task Queue in its caller's own Namespace. When one team runs several agents in the same Namespace, those agents get the same benefits: tool contracts, and calls that survive a crash. On Temporal Cloud, the caller's Namespace still has to be on the Endpoint's allowlist, because no caller is allowed by default, even one in the same Namespace.

**Make approvals searchable with a Search Attribute.** The video's governance tip shows a pattern this repo doesn't implement yet:

1. Register a custom Search Attribute named `ApprovalStatus`, for example with `temporal server start-dev --search-attribute ApprovalStatus=KeywordList`. A session can hold several calls at once, so a list type fits.
2. In [`gateway/session_workflow.py`](gateway/session_workflow.py), keep it up to date with the session's calls: include `Pending` while any call is held (`awaiting_approval` or `awaiting_gate`), and `Approved` or `Rejected` once a decision arrives (`decide_approval` for a person, `_run_gate` for a gate).
3. In the Temporal Web UI, filter the `platform` Namespace with `ApprovalStatus = "Pending"` to see which sessions are waiting, or `ApprovalStatus = "Approved"` to see which were signed off.

See [Search Attributes](https://docs.temporal.io/search-attribute) in the Temporal docs.

<a id="what-it-is-not"></a>

## What this demo is not

- **Deployments are simulated.** The CD system is a local stub that honors idempotency keys. A real gateway would call your CD system's API with the same keys.
- **The person's identity isn't signed.** Bearer tokens and the console's sign-in map to fixed demo people in `config/principals.yaml`, and the gateway passes the person to each team in plain Nexus headers. In production, use your identity provider and sign the principal, for example as a short-lived JWT the team's handler verifies.
- **It was tested only on the local Temporal dev server,** which has no Nexus Endpoint allowlists, so any Namespace can call any Endpoint. On Temporal Cloud, allowlists decide which Namespaces may call each Endpoint; this repo hasn't been run there.
- **It isn't a production gateway.** It serves one service (`checkout-api`), everyone viewing the console acts as the same signed-in person, and the front door and console listen on 127.0.0.1 only.

What Temporal doesn't do here:

- **Temporal doesn't make the reviewer model right.** Durability keeps the reviewer agent's work from being lost when a Worker dies. It doesn't make the findings correct, which is why a person decides.
- **Temporal doesn't make side effects run once.** Activities and Nexus Operations can run more than once, so every side effect carries an idempotency key derived from the Nexus request ID, and the receiving system checks it. The gateway doesn't yet accept an idempotency key from its caller, so a caller that retries after a call has finished can repeat it.

## If the demo won't start

| Error | Cause | Fix |
|---|---|---|
| `Port 7233 is already in use, and TEMPORAL_PORT asks for it (the Temporal dev server). Stop whatever is using port 7233, or set TEMPORAL_PORT to a free port in .env.` | Another program, often another Temporal dev server, holds the port. The same message names `TEMPORAL_UI_PORT`, `GATEWAY_PORT`, or `CONSOLE_PORT` when one of those is taken. | Stop that program, or set the named variable to a free port in `.env`. |
| `The Temporal CLI isn't on your PATH. Install it (on macOS: brew install temporal), then run make setup.` | The Temporal CLI isn't installed, or isn't on your PATH. | Install it, then run `make setup`. |
| `The demo is already running (another make run). Stop it with Ctrl+C in its terminal first.` | A second `make run` while the first is still running. | Use the running demo, or stop it with Ctrl+C first. |

## Cost to run

The numbers matter because of what happens after a crash. When a Worker dies in the middle of a review, Temporal resumes the review at the step that was running, so the reviewer steps that already finished aren't paid for again. Restarting the review from the top would pay for all of them.

- **One review by the real reviewer, measured:** 3 model calls, one per reviewer step, with 9,169 input and 2,580 output tokens (11,749 in all), about $0.044. Model `claude-sonnet-5-5` at effort `low`, with thinking in its lightest mode (`between_tools`, because this model's thinking can't be turned off), up to 1,024 output tokens per step, and the client's retries off so Temporal owns them. Prices are $2 per million input tokens and $10 per million output tokens, from `config/pricing.yaml` as of 2026-09-25.
- **The key-free run costs nothing.** Nothing in `make run`, `make demo`, or `make test` calls a model.
- **A crash mid-review, measured:** resuming pays again only for the step in flight. For the dependency check, that's 3,817 tokens, about $0.013, where restarting the review would pay about $0.044 again.
- **Claude Code as the client** depends on how you phrase requests and how long the session runs, and it hasn't been measured here. Run `/usage` in Claude Code to see your own.
- **At scale, an estimate:** each new version is reviewed once, and repeated requests for the same version share that review. At the measured $0.044 per review, 100 production releases a day would cost about $4.40 a day in reviewer tokens, before any cost on the agent's side.
- **What changes the cost:** a larger model, longer change sets, and more production releases under the mandate.

## Architecture

![Architecture diagram. Claude Code, as Noah's agent, connects over MCP to the gateway's front door in the platform Namespace. Each tool call becomes an Update on an AgentSessionWorkflow, which checks the ToolRegistryWorkflow's tools and policy, then calls a team's Nexus Service through that team's Nexus Endpoint: release-eng-tools into the release-eng Namespace, which holds the release ledger and the DeployWorkflow, and security-tools into the security Namespace, which holds the ReviewDeskWorkflow. Each team registers its tools by running a PublishToolsWorkflow in its own Namespace, which calls Platform's Registration Service through the team's registration Endpoint, platform-registry-release-eng or platform-registry-security. The security Namespace and its two Endpoints are drawn dashed: they don't exist at the start and are created when Security joins. The release console reads every Namespace with Queries and sends people's decisions as Updates.](assets/architecture.svg)

Workflow code runs in the Workers, the gateway's and each team's, and the Temporal Service stores every Workflow's Event History. That's why a held call or a review in progress survives a Worker crash: a new Worker replays the history and picks up where the old one stopped.

1. An agent connects to the gateway's front door, [`gateway/mcp_server.py`](gateway/mcp_server.py), a standard MCP server over HTTP. Each tool call becomes an Update on that person's session Workflow, [`gateway/session_workflow.py`](gateway/session_workflow.py).
2. The session reads the registry, [`gateway/registry_workflow.py`](gateway/registry_workflow.py), checks the arguments against the tool's contract, and applies [`config/policy.yaml`](config/policy.yaml) through [`gateway/policy.py`](gateway/policy.py). Then it refuses the call, runs it, holds it for a person, or runs a gate tool first.
3. To run a tool, the session starts a Nexus Operation by name through the owning team's Nexus Endpoint. Release Engineering's handler, [`release_eng/service_handler.py`](release_eng/service_handler.py), answers `get_release_status` synchronously, without starting a Workflow, and starts a `DeployWorkflow` ([`release_eng/workflows.py`](release_eng/workflows.py)) for each deployment. Security's handler, [`security/service_handler.py`](security/service_handler.py), backs `review_release` with an Update on its review desk, [`security/review_desk_workflow.py`](security/review_desk_workflow.py), where the reviewer agent's steps run as Activities.
4. Long Nexus Operations finish through Nexus completion callbacks, so a held production deployment resumes in the session Workflow when Security decides.
5. Teams register their own tools from their own Namespaces: a publish Workflow ([`release_eng/publish.py`](release_eng/publish.py), [`security/publish.py`](security/publish.py)) calls Platform's Registration Service, [`gateway/registry_service.py`](gateway/registry_service.py), through the team's `platform-registry-<team>` Endpoint. Platform reads the team from the Endpoint the call arrived on.
6. Each team's tools are defined by a NexGen contract and a registration file in [`contracts/`](contracts/). Security doesn't exist at the start: [`demo/supervisor.py`](demo/supervisor.py) creates its Namespace, its two Endpoints, and its Worker when it joins, and `make reset` removes them.
7. The release console, [`console/`](console/), reads every Namespace and sends people's decisions as Updates.

[AGENTS.md](AGENTS.md) has the rules for coding agents working in this repo.

## Add your own tool in seven steps

[docs/add-a-tool.md](docs/add-a-tool.md) follows the Security team as it adds `review_release` to the gateway, with the real files from this repo, then shows how to do the same for your own team. Any team takes the same seven steps, in this order:

1. [Write the contract](docs/add-a-tool.md#1-write-the-contract), the tool's definition.
2. [Write the registration file](docs/add-a-tool.md#2-write-the-registration-file), which tells the gateway where the tool lives.
3. [Generate the Python types](docs/add-a-tool.md#3-generate-the-types) from the contract with NexGen.
4. [Write the handler and the Worker](docs/add-a-tool.md#4-write-the-handler-and-the-worker) that run the tool in the team's Namespace.
5. [Add a policy rule, and name the team](docs/add-a-tool.md#5-add-a-policy-rule-and-name-the-team).
6. [Create the Namespace and the two Endpoints](docs/add-a-tool.md#6-create-the-namespace-and-the-two-endpoints): one the gateway calls the tool through, and one the team registers through.
7. [Publish the tool from the team's own Namespace](docs/add-a-tool.md#7-publish-the-tool-from-securitys-own-namespace) with a small publish Workflow.

None of the seven steps changes the gateway's code.

## Where to go next

### Video

**Temporal & AI Series: Agent Gateway & Nexus**, presented by Dustin Sweet, Senior Solutions Architect at Temporal. The link goes here when the video is published.

- **What it ran:** a live Claude Code session connected to the gateway as Noah, with the key-free mock Security reviewer.
- **Added in editing:** the callout on Nexus within one Namespace and the governance tip on Search Attributes. See [Two notes the video adds](#two-notes-the-video-adds).

<details>
<summary>Chapters</summary>

- **0:00** A bad deploy gets approved
- **0:40** The problem: agents you can't see or control
- **1:19** What teams need: governance, durability, sign-off
- **1:41** What an agent gateway is
- **2:11** How teams share tools with Nexus
- **2:27** The example: Pinecrest's checkout team
- **3:10** Demo: connect Claude Code to the gateway
- **4:09** A synchronous Nexus call: release status
- **5:49** An asynchronous Nexus call: deploy with sign-off
- **7:11** A new rule: Security reviews every production release
- **8:29** Demo: Security signs off before deploy
- **9:15** One call across three Namespaces
- **10:01** The tool registry and Nexus Endpoints
- **10:26** How Security added its tool in seven steps
- **11:38** Limits and cost to run
- **12:15** The takeaway and how to run it yourself

</details>

### Slides
[Presentation Deck](docs/temporal-ai-nexus-agent-gateway-deck.pdf)

### Further reading

- [Temporal Nexus](https://docs.temporal.io/nexus) and [Nexus in the Python SDK](https://docs.temporal.io/develop/python/nexus)
- [Connect Claude Code to MCP servers](https://code.claude.com/docs/en/mcp)
- **For your coding agent:** the [Temporal developer skill](https://github.com/temporalio/skill-temporal-developer), installed with `npx skills add temporalio/skill-temporal-developer`

## Acknowledgments

Thanks to Shy, Tushar, Josh, Melanie, and Duncan for the reviews that shaped this demo and its video.

---

**Ready to try this in your own agent?** [Get started here](https://t.mp/ai-video-cta-002).
ß