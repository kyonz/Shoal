# Writing plugins

Everything pluggable in Shoal follows one pattern:

1. **Subclass the base class** from `shoal.sdk.plugins` (or `shoal.sdk` for integrations).
2. **Expose it with an entry point** in the group `shoal.<group>`, in your package's `pyproject.toml`:
   ```toml
   [project.entry-points."shoal.sandboxes"]
   firecracker = "shoal_firecracker:FirecrackerBackend"
   ```
   Install the package next to the engine and the plugin appears in the API (`GET /v1/plugins`),
   the UI pickers and resource validation. Nothing in the engine needs to change.
3. **Pass the contract** from `shoal.sdk.testing` in your own tests. The built-in plugins pass the
   same contracts (`engine/tests/test_plugin_contracts.py`).

Plugins that need the engine (to read secrets, or to use the engine's HTTP transport in tests)
define `setup(self, engine)`, which is called once before first use. Configuration comes from the
resource that selects the plugin (a SandboxProfile's `config`, a Gateway's `config`, an Eval's
`sinks[].config`, ...). Credentials are always *names of secrets*, never values.

| Group | Base class | What it does | Contract | Out of process |
|---|---|---|---|---|
| `shoal.integrations` | `Integration` | Actions, resources and webhook events for an external system | `check_integration` | Remote integration protocol (any language); MCP servers via the built-in `mcp` integration |
| `shoal.providers` | `ModelProvider` | Sends model requests to an LLM API; selected through Gateways | `check_model_provider` | Any OpenAI-compatible endpoint via `openai-compatible` |
| `shoal.sandboxes` | `SandboxBackend`, `Sandbox` | Where agents and scripts run commands and keep files | `check_sandbox_backend` | The `remote` backend's HTTP contract |
| `shoal.secrets` | `SecretProvider` | Where secret values are read from (and written to, when `writable`) | `check_secret_provider` | |
| `shoal.eval_sinks` | `EvalSink` | Exports eval results and traces to an eval platform; imports datasets | `check_eval_sink` | |
| `shoal.decision_providers` | `DecisionProvider` | Answers typed questions (choice, score, boolean) | `check_decision_provider` | |
| `shoal.agent_runtimes` | `AgentRuntime` | Runs an agent: tool loop, CLI agent, remote agent | | Any A2A agent via `a2a` |
| `shoal.credentials` | `CredentialProvider` | Issues short-lived cloud credentials for a CloudIdentity | | |
| `shoal.orchestrators` | `Orchestrator` | Drives runs: the built-in Postgres executor, or Temporal | | |
| `shoal.dispatch_queues` | `DispatchQueue` | Tells workers a run is due: the runs table itself, Amazon SQS, or your queue | `check_dispatch_queue` | |
| `shoal.skill_sources` | `SkillSource` | Marketplaces that skills are imported from | `check_skill_source` | `shoal.skill-source/v1`, served for any source by `shoal.sdk.skill_server` |

`check_entry_point(group, name)` checks that an installed package exposes the plugin in the right
group and that it subclasses the right base.

## Integrations

See [integrations.md](integrations.md): the SDK, scaffold, test kit and the remote protocol.

## Model providers (`shoal.providers`)

```python
from shoal.sdk.plugins import ModelProvider, ModelRequest, ModelResponse, ProviderUnavailable

class MyProvider(ModelProvider):
    name = "my-llm"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # request.config holds the Gateway's base_url, headers, api_key (resolved from a secret) and model settings
        ...
        return ModelResponse(content=[{"type": "text", "text": "..."}], stop_reason="end_turn",
                             usage={"input_tokens": 10, "output_tokens": 3, "cost_usd": 0.0001})
```

Messages and content blocks use the Anthropic Messages shape (`text`, `tool_use`, `tool_result`).
Raise `ProviderUnavailable` for rate limits and outages so model policies can fall back.

## Sandboxes (`shoal.sandboxes`)

Implement `create` (and `attach` when sandboxes survive a worker restart) on the backend, and
`exec`, `write_file`, `read_file` and `close` on the sandbox. `exec` runs in the workdir by
default, applies `spec.env` plus per-call `env`, stops commands at `timeout` and reports
`timed_out`. `read_file` raises `FileNotFoundError` for missing files. Backends that can pause a
VM implement `suspend`/`resume`, which Shoal uses during long waits.

## Secret backends (`shoal.secrets`)

`get(tenant_id, name)` returns the value or `None`. Writable backends also implement `put` and
`delete`. The active namespace is available from `shoal.core.scope.current_namespace()`. Choose
the lookup order with `SHOAL_SECRET_PROVIDERS`.

## Eval sinks (`shoal.eval_sinks`)

Implement `export_eval`, `export_trace` and `import_dataset`, and describe the settings in
`config_schema` (JSON Schema). Use `self.client(base_url)` for HTTP so tests can mock it.
Optionally implement `test_connection` (a cheap call returning `{"ok": bool, "detail": ...}`)
and `list_datasets` (names), which back the eval platform test and dataset picker.
Braintrust and Langfuse are the reference implementations.

## Decision providers (`shoal.decision_providers`)

`decide` answers every question. Choice answers are option keys, score answers are levels, and
boolean answers are booleans, each with probabilities and a confidence.
`shoal.decisions.base.answer_from_probabilities` builds a correct `Answer` from a probability
map.

## Agent runtimes and cloud credentials

Agent runtimes receive an `AgentInvocation` (agent, prompt, tools, sandbox, model client) and
return an `AgentResult` whose outputs match the step's schema. Cloud credential providers issue
an STS-style session for a `CloudIdentity`. See `shoal/cloud/aws.py`.

## Skill sources (`shoal.skill_sources`)

Marketplaces that skills are imported from. Subclass `SkillSource` with a pydantic
`config_model` (fields typed `SecretStr` come from secrets in `shared`), implement `list` and
`fetch`, and optionally `search` (the default filters the listing). `fetch` returns a
`SkillBundle`: the SKILL.md body (`parse_skill_md` splits off the frontmatter), the other files as
text, a `version` and, where there is one, the exact `ref`. An unknown skill raises
`SkillSourceError` with status 404. Check it with `check_skill_source`; `git` and `registry` are
the reference implementations, and `shoal.sdk.skill_server` serves any source over
`shoal.skill-source/v1` so it can run as its own service. See [skills.md](skills.md).

## Orchestrators (`shoal.orchestrators`)

An orchestrator decides when and where a run is advanced; `Executor.advance` decides what
advancing means. Implement the `Orchestrator` protocol: `submit` (a run was created), `wake`
(called after every `Store.wake_run`), `serve` (the worker side), `tick` (repair missed starts and
wakes) and `drain` (advance everything due, for tests). Take the run's lease with
`Store.lease_run` before calling `Executor.advance`, and keep the database as the source of
truth. Operators pick one with `SHOAL_ORCHESTRATOR`. The Temporal orchestrator
(`shoal/runtime/temporal/`) is the reference implementation; see [Temporal](temporal.md).

## Dispatch queues (`shoal.dispatch_queues`)

A dispatch queue carries wake-ups ("run X is due at T") from whoever moved a
run's `next_wake_at` to the workers; the database stays the source of truth and
workers still claim each run with a lease, so the queue may deliver a message
more than once, early or late, or lose one (the database sweep finds that run).
Implement the `DispatchQueue` protocol from `shoal.sdk.plugins` with
`external = True`:

```python
from shoal.sdk.plugins import Delivery, DispatchQueue, QueueError, Wakeup

class RedisQueue:
    name = "redis"
    external = True
    batch_size = 100

    def setup(self, engine): self.url = os.environ["SHOAL_REDIS_URL"]
    async def start(self): ...                                  # connect; raise QueueError on bad config
    async def close(self): ...
    def groups(self, lanes): return [tuple(lanes)]              # one receive per group (per queue)
    async def publish(self, wakeups: list[Wakeup]): ...         # deliver each at or after wakeup.due_at
    async def receive(self, lanes, max_messages, wait) -> list[Delivery]: ...  # hide them until ack/retry
    async def ack(self, deliveries): ...
    async def retry(self, deliveries, delay): ...               # visible again after `delay` seconds
```

A queue whose longest delay is short may deliver a far-off wake-up early; the
worker sends it again for the rest of the wait. `check_dispatch_queue(queue)`
from `shoal.sdk.testing` checks a started queue. Operators pick one with
`SHOAL_DISPATCH_QUEUE`. The SQS queue (`shoal/runtime/queues/sqs.py`) is the
reference implementation; see [Dispatch queues](dispatch-queues.md).
