# Add a team's tool: how Security published review_release

This walkthrough follows the Security team as it adds its tool to the gateway: `review_release`, which asks Security to review a release before it ships. It's the tool the demo adds when Security joins. Security's tool lives behind its own Nexus Endpoint in its own Namespace, and the gateway never imports its code. Once it's published, agents can call it, policy decides when they must, and every call shows up in the console's audit.

Security did seven things, in the order any team would:

1. A contract (the tool's definition)
2. A registration file (how the gateway reaches it)
3. Generated Python types
4. A handler and a Worker
5. A policy rule and the team's display name
6. A Namespace and two Endpoints
7. A publish Workflow that registers the tool from Security's own Namespace

Every file named here is the real one in this repo, so you can read along. Steps 1 to 5 are already done. Steps 6 and 7 aren't: in the demo's start state Security doesn't exist yet, so you can run them yourself. You need the demo running (`make run`).

The examples use the default dev server port, 7233. If you set `TEMPORAL_PORT` in `.env`, use that port instead.

## 1. Write the contract

The contract is the tool definition. NexGen generates Security's Python types from it, and the gateway serves its input schema and description to agents. Security's is `contracts/security_review.nexusrpc.yaml`:

```yaml
services:
  SecurityReview:
    fqn: SecurityReview
    description: Security reviews of releases.
    operations:
      reviewRelease:
        fqn: review_release
        description: >-
          Ask Security to review a release. A reviewer agent reads the change,
          then a Security engineer approves or rejects it.
        input: { $ref: "#/$defs/ReviewReleaseInput" }
        output: { $ref: "#/$defs/ReviewReleaseOutput" }

$defs:
  ReviewReleaseInput:
    type: object
    additionalProperties: false
    properties:
      service:
        type: string
        enum: ["checkout-api"]
        description: The service the release belongs to.
      version:
        type: string
        pattern: "^[0-9]+\\.[0-9]+\\.[0-9]+$"
        description: The version to review, as MAJOR.MINOR.PATCH, for example 2.4.0.
    required: [service, version]
```

The output, `ReviewReleaseOutput`, carries the reviewer agent's findings and recommendation, and two fields the gateway reads: `decision` (`approved` or `rejected`) and `decided_by`. Those two make `review_release` usable as a gate, a tool policy can require before another call runs.

Operation keys are camelCase because NexGen requires it. The `fqn` is the snake_case name that agents, the gateway, and policy use. Who asked for the review isn't an input: the gateway sends it in Nexus headers.

## 2. Write the registration file

Nexus Endpoints don't publish their Operations at runtime, so the gateway keeps a registry. A team's registration file tells the gateway where its tools live. Security's is `contracts/security_review.registration.yaml`, beside the contract:

```yaml
contract: security_review.nexusrpc.yaml
owner_team: security
endpoint: security-tools

tools:
  review_release:
    mode_hint: async
    schedule_to_close_seconds: 604800 # 7 days
    summary: "review {service} {version}"
```

`mode_hint: async` tells the gateway the call outlives a request: a review waits for a person, so the gateway hands it back as a call handle and waits for the result. The 7-day timeout is how long a review may wait for that person. `summary` is how the audit and the Temporal Web UI describe each call.

## 3. Generate the types

```sh
nexgen python contracts/security_review.nexusrpc.yaml --output security/generated
```

This writes `security/generated/` with `ReviewReleaseInput`, `ReviewReleaseOutput`, and the `SecurityReview` Service definition. `make generate` runs it for every team. Never edit the output by hand; run the command again after changing the contract.

## 4. Write the handler and the Worker

Each team owns its connection code: `security/connection.py` is Security's own copy, and `config/profiles/local.toml` has a `[profile.security]` that points it at the `security` Namespace.

A review takes as long as a person takes, so Security's Operation is asynchronous. It's backed by a Workflow Update on Security's review desk, a long-lived Workflow in `security/review_desk_workflow.py` that runs the reviewer agent (three Activities) and holds the decision. `security/service_handler.py`:

```python
    @nexus.temporal_operation
    async def review_release(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: ReviewReleaseInput,
    ) -> nexus.TemporalOperationResult[ReviewReleaseOutput]:
        request = ReviewRequest(input.service, input.version, requesting_principal(ctx))
        desk = await ensure_desk(client.client, self._desk_config(input.service))  # Signal-with-Start
        # The Update backs the Operation, which completes when Security decides.
        with update_failures_keep_their_message():
            return await client.start_workflow_update(
                desk, ReviewDeskWorkflow.review_release, request, update_id=f"review-{ctx.request_id}"
            )
```

The handler starts the desk if it isn't running, starts the Update, and returns once the desk accepts it. The Operation completes when the Update returns, which is when a Security engineer decides, and Nexus delivers the result to the gateway through a completion callback. Deriving the Update ID from the Nexus request ID means a retried start request lands on the same review.

`security/worker.py` runs it all on Namespace `security`, Task Queue `security`: the review desk Workflow, the reviewer's Activities, this handler, and the publish Workflow from step 7.

## 5. Add a policy rule, and name the team

Policy denies any tool no rule allows, and Platform owns `config/policy.yaml`. Security's tool needed two rules, both in version 2, the Security mandate:

```yaml
  - id: deploy-production-security-gate
    tool: deploy
    when: { environment: production }
    effect: require_gate
    gate: review_release
    blocks_release:
      fields: [service, version, environment]
      label: "{service} {version} to {environment}"
  - id: review-on-request
    tool: review_release
    effect: allow
```

The first makes every production deployment run `review_release` first and go ahead only if Security approves. The second lets agents ask for a review directly. Version 1 has neither, so until Platform applies version 2, a call to `review_release` is denied. Platform can't apply version 2 before the tool is published: the registry refuses a policy that requires a gate it doesn't have.

So the console can name the team and its people, `config/principals.yaml` lists the team under `teams:` and a Security engineer, Abe Roover, with the `security-reviewer` role:

```yaml
teams:
  security: Security
```

## 6. Create the Namespace and the two Endpoints

Every team has two Endpoints: one the gateway calls its tools through, and one the team registers through. Security's are `security-tools` (to Namespace `security`, Task Queue `security`) and `platform-registry-security` (to Platform's registration Service: Namespace `platform`, Task Queue `gateway`).

In the demo, Security joins with one command:

```sh
make security-joins
```

You should see `Security joined: the security Namespace, the security-tools and platform-registry-security Endpoints, and Security's Worker.` The console now shows Security's Worker chip, its group under "Tools on the gateway" with no tools published yet, and its node in the live calls diagram. The console's first **Scenario** step does the same.

Under the hood, that's these commands, plus telling the demo supervisor about the new Worker (so the console's Kill and Restart work) and adding Security's people to the gateway's roster:

```sh
temporal operator namespace create --namespace security --address localhost:7233
temporal operator nexus endpoint create --name security-tools \
  --target-namespace security --target-task-queue security --address localhost:7233
temporal operator nexus endpoint create --name platform-registry-security \
  --target-namespace platform --target-task-queue gateway --address localhost:7233
uv run python -m security.worker
```

A new Namespace takes a moment to reach every part of the dev server. If you run these by hand and the first Endpoint command says the Namespace isn't found, run it again a second later; `make security-joins` waits for you.

The local dev server has no Endpoint allowlists, so any Namespace can call either Endpoint. On Temporal Cloud, each Endpoint has an allowlist of caller Namespaces: `platform-registry-security` would allow only Security's Namespace, and `security-tools` only the gateway's. This repo has been tested only on the local dev server.

## 7. Publish the tool from Security's own Namespace

Security never connects to Platform's Namespace. It registers its tool the way every team does: a small publish Workflow in its own Namespace calls Platform's registration Service over Nexus, through its own registration Endpoint. Platform reads the team from the Endpoint the call arrives on, which the server sets, so Security can register only Security's tools.

Teams share no code, so `security/publish.py` is Security's own copy of the publish Workflow; Release Engineering has its own in `release_eng/publish.py`. The copies differ only in the names at the top:

```python
TEAM_NAMESPACE = "security"
TASK_QUEUE = "security"
REGISTRATION_ENDPOINT = "platform-registry-security"
```

The Workflow sends the registration file and the contract it names, as text, and calls the Operation by name, so Security needs none of Platform's code:

```python
        registry = workflow.create_nexus_client(service="PlatformRegistry", endpoint=REGISTRATION_ENDPOINT)
        try:
            return await registry.execute_operation(
                "register_tool",
                asdict(request),
                schedule_to_close_timeout=timedelta(minutes=1),
                summary=f"publish {request.registration_file}",
            )
```

Now publish:

```sh
uv run python -m security.publish contracts/security_review.registration.yaml
```

You should see `Registered review_release with the gateway (registry v2), through platform-registry-security.` The publish Workflow is in the `security` Namespace's history, linked to the registration in Platform's registry, and the console's audit shows the tool as published by the Security team. `make publish-security` runs the same command (and joins first, if Security hasn't).

## Try it

- **In the console** (http://localhost:8090): "Tools on the gateway" lists `review_release` under Security. Click it to see the input schema, straight from the contract.
- **In Claude Code**, connected as in the README: `review_release` appears without reconnecting, because the gateway tells MCP clients when its tool list changes. Ask for a review now and the gateway refuses it, because policy version 1 has no rule for it.
- **Apply the mandate**: run `make apply-mandate` (or the console's last **Scenario** step). Then ask Claude Code to ship 2.4.0 to staging, then to production. The production deployment is held for Security's review: the reviewer agent runs its three steps, and the console asks Security to decide. Sign in as Abe Roover and approve, and the deployment runs.

`make reset` returns to the start state, so Security's Namespace, Endpoints, Worker, and tool are gone again.

## Do it for your own team

The same seven steps, with your team's names:

1. Your contract in `contracts/<team>.nexusrpc.yaml`.
2. Your registration file beside it, naming your tool Endpoint, `<team>-tools`.
3. `nexgen python contracts/<team>.nexusrpc.yaml --output <team>/generated`. NexGen creates the folders.
4. A `<team>/` folder with a copy of `gateway/connection.py`, your handler, and your Worker, and a `[profile.<team>]` in `config/profiles/local.toml`.
5. Platform adds your policy rules to `config/policy.yaml` and your team under `teams:` in `config/principals.yaml`. To load an edited rule into a running demo, run `uv run python -m demo.seed reload-policy`.
6. Your Namespace and your two Endpoints, `<team>-tools` and `platform-registry-<team>`, with the `temporal operator` commands in step 6. On Temporal Cloud, give `platform-registry-<team>` an allowlist with only your Namespace.
7. A copy of `security/publish.py` as `<team>/publish.py`, with your names at the top, run by your Worker. Then `uv run python -m <team>.publish contracts/<team>.registration.yaml`.

`make check-contracts` compares the registry fixture with every registration file, so record your tool and run the tests:

```sh
make update-fixtures
make test
```

`make reset` starts a fresh dev server, so after a reset, create your Namespace and Endpoints and publish again.

## What Security didn't touch

The gateway's code. Platform wrote the policy rules and listed the team, but the gateway's code didn't change, and Security never connected to Platform's Namespace. The gateway reached Security's tool by the names in its registry entry: Endpoint `security-tools`, Service `SecurityReview`, Operation `review_release`. That the tool is itself an agent, waiting days for a person, is Security's business: the gateway sees a Nexus Operation that returns a decision. Security kept its own Namespace, its own Worker, and its own release cycle.
