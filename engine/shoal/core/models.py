"""Declarative resource models.

Every user-authored object (workflow, agent, skill, sandbox profile, connection,
trigger, campaign) is a pydantic model wrapped in a Kubernetes-style envelope:

    apiVersion: shoal.dev/v1
    kind: Workflow
    metadata: {name: jira-bugfix}
    spec: {...}

The same models validate YAML files, API payloads and stored state, and their
JSON schemas are published through the API so the UI and SDKs can render forms.
"""

from __future__ import annotations

import enum
import re
from datetime import timedelta
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shoal.core.duration import Duration

API_VERSION = "shoal.dev/v1"
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
STEP_ID_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- params


class ParamSpec(Model):
    """A typed input or output parameter."""

    type: Literal["string", "integer", "number", "boolean", "object", "array", "any"] = "string"
    description: str | None = None
    default: Any = None
    required: bool | None = None
    enum: list[Any] | None = None

    @property
    def is_required(self) -> bool:
        if self.required is not None:
            return self.required
        return self.default is None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {} if self.type == "any" else {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.default is not None:
            schema["default"] = self.default
        if self.enum:
            schema["enum"] = self.enum
        return schema


def params_json_schema(params: dict[str, ParamSpec]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: p.json_schema() for name, p in params.items()},
        "required": [name for name, p in params.items() if p.is_required],
    }


_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


class ParamValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_params(params: dict[str, ParamSpec], values: dict[str, Any] | None) -> dict[str, Any]:
    """Apply defaults, light coercion and type checks. Unknown keys are kept."""
    values = dict(values or {})
    errors: list[str] = []
    for name, spec in params.items():
        if values.get(name) is None:
            if spec.default is not None:
                values[name] = spec.default
            elif spec.is_required:
                errors.append(f"'{name}' is required")
            continue
        value = values[name]
        if spec.type in ("integer", "number") and isinstance(value, str):
            try:
                value = int(value) if spec.type == "integer" else float(value)
            except ValueError:
                pass
        if spec.type == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if spec.type == "string" and isinstance(value, int | float) and not isinstance(value, bool):
            value = str(value)
        expected = _PY_TYPES.get(spec.type)
        if expected and (not isinstance(value, expected) or (spec.type != "boolean" and isinstance(value, bool))):
            errors.append(f"'{name}' must be of type {spec.type}")
        if spec.enum and value not in spec.enum:
            errors.append(f"'{name}' must be one of {spec.enum}")
        values[name] = value
    if errors:
        raise ParamValidationError(errors)
    return values


# --------------------------------------------------------------------------- steps


class RetryPolicy(Model):
    max_attempts: int = Field(1, ge=1, le=100)
    backoff: Duration = timedelta(seconds=10)
    backoff_factor: float = Field(2.0, ge=1.0)
    max_backoff: Duration = timedelta(hours=1)

    def delay_for(self, attempt: int) -> timedelta:
        """Delay before ``attempt`` (2 = first retry)."""
        seconds = self.backoff.total_seconds() * (self.backoff_factor ** max(0, attempt - 2))
        return timedelta(seconds=min(seconds, self.max_backoff.total_seconds()))


def _no_clear_text_keys(config: dict[str, Any]) -> dict[str, Any]:
    """Decision provider settings name secrets (``api_key_secret``); they never hold a credential literally."""
    bad = [k for k in credential_fields(config)
           if not k.endswith("_secret") and isinstance(config[k], str) and "{{" not in config[k]]
    if bad:
        raise ValueError(f"provider_config {bad} look like credentials; name a secret instead, e.g. api_key_secret")
    return config


DecisionConfig = Annotated[dict[str, Any], AfterValidator(_no_clear_text_keys)]


class StepBase(Model):
    id: str = Field(pattern=STEP_ID_PATTERN)
    name: str | None = None
    needs: list[str] | None = Field(
        None, description="Steps that must finish first. Defaults to the previous step in the list."
    )
    if_: str | None = Field(None, alias="if", description="Expression; the step is skipped when false.")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout: Duration | None = None
    on_error: Literal["fail", "continue"] = "fail"

    type: ClassVar[str]


class AgentStep(StepBase):
    """Run an agent inside a sandbox until it returns the declared outputs."""

    type: Literal["agent"] = "agent"  # type: ignore[misc]
    agent: str | None = Field(None, description="Agent resource name; defaults to the workflow default agent.")
    prompt: str
    sandbox: str | None = None
    outputs: dict[str, ParamSpec] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list, description="Extra skills for this step.")
    tools: list[str] = Field(default_factory=list, description="Extra tools for this step.")
    connections: dict[str, str] = Field(default_factory=dict)


class ActionStep(StepBase):
    """Call an integration action, e.g. ``github.merge_pull_request``."""

    type: Literal["action"] = "action"  # type: ignore[misc]
    action: str = Field(pattern=r"^[a-z0-9_-]+\.[a-z0-9_]+$")
    connection: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


SCRIPT_RESERVED_OUTPUTS = frozenset({"exit_code", "stdout", "stderr", "timed_out", "duration_seconds", "outputs"})


class ScriptStep(StepBase):
    """Run a script in a sandbox: deterministic code instead of an agent.

    Either ``run`` (a shell command line, rendered as an expression) or ``code``
    (a program in ``language``, used verbatim). ``inputs`` are rendered and
    handed to the program as JSON in the file named by ``$SHOAL_INPUTS``; it
    writes a JSON object of ``outputs`` to ``$SHOAL_OUTPUTS``. Python programs
    can also ``import shoal_step`` (``inputs``, ``set_output``, ``set_outputs``).
    Output: ``exit_code``, ``stdout``, ``stderr``, the declared outputs (also
    under ``outputs``).
    """

    type: Literal["script"] = "script"  # type: ignore[misc]
    run: str | None = Field(None, description="Shell command line; expressions are rendered.")
    code: str | None = Field(None, description="Program source in `language`; used verbatim, never rendered.")
    language: Literal["shell", "bash", "python", "node"] = "shell"
    inputs: dict[str, Any] = Field(default_factory=dict, description="Values (expressions allowed) passed as JSON.")
    outputs: dict[str, ParamSpec] = Field(default_factory=dict, description="Typed outputs the program must write.")
    sandbox: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str | None = None
    fail_on_error: bool = True
    cloud_identities: list[str] = Field(
        default_factory=list, description="CloudIdentity resources whose short-lived credentials the script gets (§7.1)."
    )

    @model_validator(mode="after")
    def _one_source(self) -> ScriptStep:
        if (self.run is None) == (self.code is None):
            raise ValueError("script step needs exactly one of: run, code")
        if self.run is not None and self.language != "shell":
            raise ValueError("'language' applies to 'code'; 'run' is always a shell command line")
        reserved = SCRIPT_RESERVED_OUTPUTS & set(self.outputs)
        if reserved:
            raise ValueError(f"reserved output names: {sorted(reserved)}")
        return self


class WaitCondition(Model):
    """Poll an action until ``check`` (evaluated with ``result``) is true."""

    action: str = Field(pattern=r"^[a-z0-9_-]+\.[a-z0-9_]+$")
    connection: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    check: str
    poll_interval: Duration = timedelta(minutes=15)
    wake_on: list[str] = Field(
        default_factory=list,
        description="Event keys that wake this wait early, e.g. 'github:pr:acme/api#12'.",
    )


class WaitSignal(Model):
    """Wait for data sent into the run.

    ``name`` takes signals sent with ``POST /v1/runs/{id}/signals/{name}`` or to the
    run's callback URL (``{{ run.signal_url }}/{name}``); signals that arrive before
    the step waits are kept for it. ``event`` takes the next integration event with
    that wake key (e.g. ``github:pr:acme/api#12``) while the step is waiting.
    """

    name: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    event: str | None = Field(None, description="Expression producing an integration event wake key.")
    match: str | None = Field(
        None, description="Expression evaluated with `signal` (the payload); signals that do not match stay queued."
    )

    @model_validator(mode="after")
    def _one_source(self) -> WaitSignal:
        if bool(self.name) == bool(self.event):
            raise ValueError("signal wait needs exactly one of: name, event")
        return self


class WaitStep(StepBase):
    """Durable wait for a duration, until a time, until a condition holds, or for a signal."""

    type: Literal["wait"] = "wait"  # type: ignore[misc]
    duration: Duration | None = None
    until: str | None = Field(None, description="Expression producing an ISO-8601 timestamp.")
    condition: WaitCondition | None = None
    signal: WaitSignal | None = None
    max_wait: Duration | None = Field(None, description="Give up waiting for the condition or signal after this long.")
    on_timeout: Literal["fail", "continue"] = "continue"

    @model_validator(mode="after")
    def _one_mode(self) -> WaitStep:
        modes = [m for m in (self.duration, self.until, self.condition, self.signal) if m]
        if len(modes) != 1:
            raise ValueError("wait step needs exactly one of: duration, until, condition, signal")
        return self


class ApprovalStep(StepBase):
    """Human-in-the-loop gate resolved through the API or UI."""

    type: Literal["approval"] = "approval"  # type: ignore[misc]
    message: str
    approvers: list[str] = Field(default_factory=list)
    max_wait: Duration | None = None
    on_timeout: Literal["reject", "approve"] = "reject"
    on_reject: Literal["fail", "continue"] = Field(
        "fail",
        description="'fail' ends the run when the step is rejected (or expires rejected); "
        "'continue' succeeds with approved: false so later steps can branch on it.",
    )


class WorkflowCallStep(StepBase):
    """Run another workflow as a child run, optionally once per item (fan-out).

    A failed child fails the step; with ``retry:``, a retry runs only the failed children again.
    """

    type: Literal["workflow"] = "workflow"  # type: ignore[misc]
    workflow: str
    version: int | None = Field(None, ge=1, description="Pin the child workflow version; omitted, the current one.")
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    for_each: str | None = Field(None, description="Expression producing a list; exposes `item` and `index`.")
    concurrency: int = Field(5, ge=1, le=1000)


class SetStep(StepBase):
    """Compute values from expressions; they become the step output."""

    type: Literal["set"] = "set"  # type: ignore[misc]
    values: dict[str, Any]


class DecisionQuestion(Model):
    """A typed question for a decision model (Jev-style System One, or an LLM fallback)."""

    type: Literal["choice", "score", "boolean"]
    instructions: str
    options: dict[str, str] = Field(default_factory=dict, description="choice: option key -> description.")
    levels: list[str] = Field(default_factory=list, description="score: ordered rubric levels, lowest first.")

    @model_validator(mode="after")
    def _check(self) -> DecisionQuestion:
        if self.type == "choice" and not (2 <= len(self.options) <= 255):
            raise ValueError("choice questions need 2-255 options")
        if self.type == "score" and len(self.levels) < 2:
            raise ValueError("score questions need at least 2 levels")
        return self


class DecideStep(StepBase):
    """Ask a decision model typed questions about some state and branch on the answers."""

    type: Literal["decide"] = "decide"  # type: ignore[misc]
    provider: str | None = Field(None, description="Decision provider plugin (jev, llm); defaults to settings.")
    provider_config: DecisionConfig = Field(default_factory=dict)
    state: Any = Field(description="Text or JSON (expressions allowed) the questions are about.")
    questions: dict[str, DecisionQuestion] = Field(min_length=1)
    min_confidence: float | None = Field(None, ge=0, le=1)
    on_low_confidence: Literal["continue", "fail", "approval"] = Field(
        "continue", description="'approval' holds the run for a human when any answer is below min_confidence."
    )


class GradeStep(StepBase):
    """Score an output against a rubric; fail or flag below a threshold."""

    type: Literal["grade"] = "grade"  # type: ignore[misc]
    provider: str | None = None
    provider_config: DecisionConfig = Field(default_factory=dict)
    subject: Any = Field(description="What to grade (expression).")
    instructions: str
    levels: list[str] = Field(min_length=2, description="Ordered rubric levels, worst first.")
    threshold: float = Field(0.7, ge=0, le=1, description="Minimum normalised score (0..1) to pass.")
    on_fail: Literal["fail", "continue"] = "continue"


Step = Annotated[
    AgentStep
    | ActionStep
    | ScriptStep
    | WaitStep
    | ApprovalStep
    | WorkflowCallStep
    | SetStep
    | DecideStep
    | GradeStep,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- resources: specs


class WorkflowDefaults(Model):
    agent: str | None = None
    sandbox: str | None = None


class WorkflowSpec(Model):
    description: str | None = None
    inputs: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict, description="Output name -> expression.")
    connections: dict[str, str] = Field(
        default_factory=dict, description="Integration key -> connection name used by default."
    )
    defaults: WorkflowDefaults = Field(default_factory=WorkflowDefaults)
    timeout: Duration | None = None
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowSpec:
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        known = set(ids)
        for step in self.steps:
            for dep in step.needs or []:
                if dep not in known:
                    raise ValueError(f"step '{step.id}' needs unknown step '{dep}'")
                if dep == step.id:
                    raise ValueError(f"step '{step.id}' cannot depend on itself")
        # cycle detection over resolved dependencies
        deps = self.dependencies()
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValueError(f"dependency cycle through step '{node}'")
            visiting.add(node)
            for d in deps[node]:
                visit(d)
            visiting.discard(node)
            done.add(node)

        for i in ids:
            visit(i)
        return self

    def dependencies(self) -> dict[str, list[str]]:
        """Resolved dependencies: explicit ``needs`` or the previous step."""
        out: dict[str, list[str]] = {}
        prev: str | None = None
        for step in self.steps:
            if step.needs is not None:
                out[step.id] = list(step.needs)
            else:
                out[step.id] = [prev] if prev else []
            prev = step.id
        return out

    def step(self, step_id: str) -> StepBase:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)


class AgentLimits(Model):
    max_turns: int = Field(60, ge=1, le=1000)
    max_tokens: int = Field(32000, ge=256, description="Max output tokens per model call.")
    timeout: Duration = timedelta(hours=1)
    budget_usd: float | None = Field(None, ge=0)


class ToolGuardSpec(Model):
    """Screen mutating tool calls with a decision model before they run (§7.5)."""

    provider: str | None = None
    provider_config: DecisionConfig = Field(default_factory=dict)
    tools: list[str] = Field(
        default_factory=lambda: ["shell", "write_file", "edit_file", "*__*"],
        description="Glob patterns of tool names to screen.",
    )
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {"irreversible": 0.7, "intent_mismatch": 0.7},
        description="Block when the probability for a check is at or above its threshold.",
    )


class AgentSpec(Model):
    description: str | None = None
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    runtime: str = Field("tool-loop", description="Agent runtime plugin: tool-loop, cli (a coding agent CLI in the sandbox) or a2a.")
    instructions: str = Field("", description="System prompt.")
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(
        default_factory=lambda: ["shell", "read_file", "write_file", "list_files"],
        description="Built-in tools (shell, read_file, ...), integration actions ('github.create_pull_request', 'github.*') "
        "and MCP servers ('mcp:<connection>' for all its tools, 'mcp:<connection>.<tool>' for one).",
    )
    subagents: list[str] = Field(default_factory=list, description="Agents this agent may delegate to.")
    credentials: list[str] = Field(
        default_factory=list,
        description="Integrations whose short-lived credentials are injected into the sandbox env, e.g. "
        "'github' or 'github:github-acme' (integration:connection).",
    )
    cloud_identities: list[str] = Field(
        default_factory=list,
        description="CloudIdentity resources (e.g. an AWS IAM role) whose short-lived credentials are injected into "
        "the sandbox env. Each identity must grant this agent and workflow.",
    )
    sandbox: str | None = None
    env: dict[str, str] = Field(default_factory=dict, description="Sandbox env; values may use expressions.")
    limits: AgentLimits = Field(default_factory=AgentLimits)
    provider_config: dict[str, Any] = Field(
        default_factory=dict, description="Model settings (temperature, effort, ...). Endpoints and credentials belong on a Gateway."
    )
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    guard: ToolGuardSpec | None = Field(None, description="Optional decision-model screening of tool calls.")

    @model_validator(mode="after")
    def _no_endpoint_overrides(self) -> AgentSpec:
        bad = sorted({*credential_fields(self.provider_config), *(set(self.provider_config) & GATEWAY_ONLY)})
        if bad:
            raise ValueError(f"provider_config cannot set {bad}: endpoints and credentials are managed on Gateways")
        return self


class SkillSpec(Model):
    description: str
    instructions: str = Field(description="SKILL.md body.")
    files: dict[str, str] = Field(default_factory=dict, description="Relative path -> file content.")


class NetworkPolicy(Model):
    """Enforced by the sandbox backend; a backend that cannot enforce a policy refuses to start the sandbox."""

    egress: Literal["allow", "deny"] = Field("allow", description="'deny' blocks all outbound traffic.")
    allow_hosts: list[str] = Field(
        default_factory=list, description="Only these hosts may be reached (needs a backend that supports it, e.g. remote)."
    )


class SandboxProfileSpec(Model):
    description: str | None = None
    backend: str = "local"
    image: str | None = Field(None, description="Container image or template (backend specific).")
    cpu: float | None = Field(None, gt=0)
    memory_mb: int | None = Field(None, gt=0)
    disk_mb: int | None = Field(None, gt=0)
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    ttl: Duration = timedelta(hours=4)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    scope: Literal["step", "run"] = Field(
        "run", description="'run' shares one sandbox (and workspace) across all steps of a run."
    )
    suspend_on_wait: bool = Field(True, description="Suspend run-scoped sandboxes during long waits.")
    config: dict[str, Any] = Field(default_factory=dict, description="Backend-specific settings.")


class OAuthSpec(Model):
    """OAuth 2.0 authorization code flow (with PKCE) for per-user connection credentials."""

    authorize_url: str
    token_url: str
    scopes: list[str] = Field(default_factory=list)
    client_id: str
    client_secret_secret: str | None = Field(None, description="Secret (in the connection's namespace) with the client secret.")
    token_field: str = Field("token", description="Connection field the access token fills.")
    extra_params: dict[str, str] = Field(default_factory=dict, description="Extra authorize parameters, e.g. audience.")


class ConnectionSpec(Model):
    integration: str
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="Non-secret settings.")
    secrets: dict[str, str] = Field(default_factory=dict, description="Connection field -> secret name.")
    credentials: Literal["shared", "per_user", "per_user_or_shared"] = Field(
        "shared",
        description="Whose credential calls use: the connection's (`shared`), the person the run acts for "
        "(`per_user`; the step waits until they connect), or theirs when they have one (`per_user_or_shared`).",
    )
    user_fields: list[str] = Field(
        default_factory=list,
        description="Connection fields each person supplies (default: the fields in `secrets`).",
    )
    oauth: OAuthSpec | None = Field(None, description="Lets people connect with OAuth instead of pasting a token.")

    @model_validator(mode="after")
    def _personal_fields(self) -> ConnectionSpec:
        if self.credentials != "shared" and not (self.user_fields or self.secrets or self.oauth):
            raise ValueError(f"credentials: {self.credentials} needs `user_fields` (or `secrets`, or `oauth`) "
                             "to say which fields each person supplies")
        return self


class TriageSpec(Model):
    """Semantic trigger filter: ask a decision model about the event before starting a run."""

    provider: str | None = None
    provider_config: DecisionConfig = Field(default_factory=dict)
    state: Any = Field("{{ event }}", description="What the questions are about (expressions allowed).")
    questions: dict[str, DecisionQuestion] = Field(min_length=1)
    when: str = Field(description="Expression over `answers` (and `event`); fire only when true.")


class TriggerSpec(Model):
    description: str | None = None
    workflow: str | None = Field(None, description="Workflow to start.")
    workflow_version: int | None = Field(
        None, ge=1, description="Pin to this workflow version; omitted, runs use the current version."
    )
    campaign: str | None = Field(None, description="Campaign to (re)launch instead of a single run.")
    connection: str | None = Field(None, description="Connection whose events fire this trigger.")
    event: str = Field(description="Event type, e.g. 'issue_updated', 'pull_request', 'received', 'schedule'.")
    schedule: str | None = Field(None, description="Cron expression for event='schedule'.")
    timezone: str = Field("UTC", description="IANA time zone for the cron schedule.")
    overlap: Literal["allow", "skip"] = Field(
        "allow", description="'skip' does not fire while a run started by this trigger is still active."
    )
    filter: str | None = Field(None, description="Expression over `event`; fire only when true.")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Workflow inputs (expressions).")
    dedupe_key: str | None = Field(None, description="Expression; events with a seen key are ignored.")
    triage: TriageSpec | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> TriggerSpec:
        if bool(self.workflow) == bool(self.campaign):
            raise ValueError("a trigger targets exactly one of 'workflow' or 'campaign'")
        if self.workflow_version and not self.workflow:
            raise ValueError("'workflow_version' only applies to workflow triggers; pin campaigns in the campaign")
        if self.event == "schedule":
            if not self.schedule:
                raise ValueError("schedule triggers need a cron 'schedule'")
        elif not self.connection:
            raise ValueError("event triggers need a 'connection'")
        return self


class TargetSelector(Model):
    connection: str | None = None
    resource: str | None = Field(None, description="Integration resource, e.g. 'repository'.")
    filter: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] | None = Field(None, description="Static targets instead of a query.")
    key: str = Field("{{ target.id }}", description="Expression giving a stable key per target.")
    limit: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> TargetSelector:
        if self.items is None and not (self.connection and self.resource):
            raise ValueError("targets need either 'items' or 'connection' + 'resource'")
        return self


class CampaignPolicy(Model):
    concurrency: int = Field(5, ge=1, le=10000)
    runs_per_hour: int | None = Field(None, ge=1)
    max_failures: int | None = Field(None, ge=1, description="Pause the campaign after this many failures.")
    budget_usd: float | None = Field(None, ge=0)


class CampaignSpec(Model):
    description: str | None = None
    workflow: str
    workflow_version: int | None = Field(
        None, ge=1, description="Pin to this workflow version; omitted, each launch pins the version current at launch."
    )
    targets: TargetSelector
    inputs: dict[str, Any] = Field(default_factory=dict, description="Workflow inputs (expressions over target).")
    policy: CampaignPolicy = Field(default_factory=CampaignPolicy)


class IdentitySpec(Model):
    """One person across systems, used to attribute runs to the human who drove them."""

    display_name: str | None = None
    email: str | None = None
    github: str | None = Field(None, description="GitHub login.")
    jira_account_id: str | None = None
    slack_id: str | None = None
    actors: list[str] = Field(
        default_factory=list, description="Shoal actors that are this person, e.g. 'user:dana@acme.com'."
    )
    github_token_secret: str | None = Field(None, description="Secret holding the user's linked GitHub OAuth token.")


class CloudIdentityGrant(Model):
    """Who may use a cloud identity. Nothing may use it until a workflow is listed."""

    workflows: list[str] = Field(
        default_factory=list,
        description="Workflow name globs whose steps may use the identity ('deploy-*'). From another namespace "
        "(an identity in `shared`), qualify with the namespace: 'team-a/deploy', '*/deploy'.",
    )
    agents: list[str] = Field(
        default_factory=list,
        description="If set, only agent steps running these agents (globs) may use it; script steps may not.",
    )


AWS_ROLE_ARN = r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$"


class AwsRoleConfig(Model):
    role_arn: str = Field(pattern=AWS_ROLE_ARN, description="IAM role to assume, e.g. arn:aws:iam::123456789012:role/deploy.")
    region: str = Field("us-east-1", pattern=r"^[a-z]{2}(-[a-z]+)+-[0-9]$", description="STS endpoint and AWS_REGION in the sandbox.")
    duration: Duration = Field(timedelta(hours=1), description="Session length, 15m to 12h (within the role's maximum).")
    session_policy: dict[str, Any] | None = Field(None, description="Inline IAM policy that narrows the role for these sessions.")
    policy_arns: list[str] = Field(default_factory=list, max_length=10, description="Managed policies that narrow the role.")

    @model_validator(mode="after")
    def _duration_in_range(self) -> AwsRoleConfig:
        if not timedelta(minutes=15) <= self.duration <= timedelta(hours=12):
            raise ValueError("duration must be between 15m and 12h")
        return self


class CloudIdentitySpec(Model):
    """A cloud role a namespace brings to Shoal, and which workflows and agents may use it (§7.1).

    At run time Shoal exchanges a credential that starts from its own deployment for short-lived
    credentials of the role, scoped to one step attempt and attributed to the run and its initiator.
    """

    description: str | None = None
    provider: str = Field("aws", description="Credential provider plugin (shoal.credentials): aws.")
    trust: Literal["web_identity", "ambient"] = Field(
        "web_identity",
        description="web_identity: the role trusts Shoal's OIDC issuer and Shoal presents a per-step token. "
        "ambient: the role trusts the deployment's own cloud identity (IRSA, Pod Identity, instance role), "
        "with a per-identity ExternalId; the operator must enable it.",
    )
    aws: AwsRoleConfig | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="Settings for other providers.")
    allow: CloudIdentityGrant = Field(default_factory=CloudIdentityGrant)

    @model_validator(mode="after")
    def _provider_settings(self) -> CloudIdentitySpec:
        if self.provider == "aws" and self.aws is None:
            raise ValueError("provider 'aws' needs an 'aws' block with role_arn")
        if bad := credential_fields(self.config):
            raise ValueError(f"config {bad} look like credentials; cloud identities never hold secrets")
        return self


# Header and config names that carry credentials; they must come from secrets, never clear text.
CREDENTIAL_NAME = re.compile(r"(^|[-_])(authorization|auth|api[-_]?key|key|token|secret|password|passwd|cookie|credentials?)($|[-_])", re.I)


# Agent provider_config may tune a model but not redirect it around the central gateway.
GATEWAY_ONLY = frozenset({"base_url", "headers", "api_key", "endpoint", "url", "region", "project"})


def credential_fields(values: dict[str, Any]) -> list[str]:
    return sorted(k for k in values if CREDENTIAL_NAME.search(k))


def endpoint_fields(values: dict[str, Any]) -> list[str]:
    """Settings that choose where a request (and its credentials) goes: ``host``, ``url``, ``api_url``, ..."""
    return sorted(k for k in values if k in ("host", "url") or k.endswith(("_url", "_host")))


class GatewaySpec(Model):
    """How model requests reach a provider (direct API, LiteLLM, Portkey, OpenRouter, Bedrock, ...).

    Gateways are tenant-wide and managed by admins (``gateways:manage``).
    Credentials come only from secrets in the tenant's ``shared`` namespace.
    """

    description: str | None = None
    type: str = Field("anthropic", description="Provider plugin: anthropic, openai-compatible, scripted, ...")
    base_url: str | None = None
    api_key_secret: str | None = Field(None, description="Secret (in `shared`) holding the API key.")
    headers: dict[str, str] = Field(default_factory=dict, description="Non-secret headers.")
    secret_headers: dict[str, str] = Field(
        default_factory=dict, description="Header name to secret name (in `shared`), e.g. {x-portkey-api-key: portkey-key}."
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_clear_text_credentials(self) -> GatewaySpec:
        if bad := credential_fields(self.headers):
            raise ValueError(f"headers {bad} look like credentials; put them in secret_headers (header: secret name)")
        if bad := credential_fields(self.config):
            raise ValueError(f"config {bad} look like credentials; use api_key_secret or secret_headers")
        return self


class ModelCandidate(Model):
    model: str
    gateway: str | None = Field(None, description="Gateway resource or provider plugin name.")
    weight: float = Field(1.0, gt=0)
    effort: str | None = None


class ModelRuleMatch(Model):
    agents: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    difficulty: list[str] = Field(default_factory=list, description="Match routed difficulty levels.")


class ModelRule(Model):
    name: str
    match: ModelRuleMatch = Field(default_factory=ModelRuleMatch)
    candidates: list[ModelCandidate] = Field(min_length=1)
    strategy: Literal["fallback", "weighted"] = Field(
        "fallback", description="'fallback' tries candidates in order; 'weighted' splits traffic (A/B)."
    )


class ModelPolicySpec(Model):
    description: str | None = None
    allow_models: list[str] = Field(default_factory=list, description="If set, only these models may run.")
    deny_models: list[str] = Field(default_factory=list)
    difficulty_router: DecisionQuestion | None = Field(
        None, description="Optional score question; its level is matched by rules' match.difficulty."
    )
    decision_provider: str | None = None
    rules: list[ModelRule] = Field(min_length=1)


class BlueprintParam(Model):
    """Something the adopting team supplies. ``connection`` and ``secret`` params take the
    name of a connection (visible from the namespace) or a secret in the namespace."""

    type: Literal["string", "number", "boolean", "connection", "secret", "choice"] = "string"
    description: str | None = None
    integration: str | None = Field(None, description="For type=connection: the integration it must be.")
    default: Any = None
    options: list[str] = Field(default_factory=list)
    required: bool = True


class BlueprintSpec(Model):
    title: str
    summary: str
    readme: str = ""
    version: str = Field("0.1.0", pattern=r"^\d+\.\d+\.\d+([-+].+)?$")
    icon: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    status: Literal["draft", "published", "featured", "deprecated"] = Field(
        "draft", description="Only published and featured blueprints can be adopted; featuring needs blueprints:feature."
    )
    namespaces: list[str] = Field(default_factory=list, description="Namespaces that may adopt it (empty: all).")
    params: dict[str, BlueprintParam] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(
        min_length=1,
        description="Resource templates (apiVersion/kind/metadata/spec). Expressions over `params`, `prefix` and "
        "`blueprint` are filled in at adoption; all other {{ }} expressions are left for run time.",
    )


class SkillSourceSpec(Model):
    """A skills marketplace the tenant imports skills from (a git repository, a registry, or any
    ``shoal.skill_sources`` plugin). Tenant-wide; its secrets are read from the ``shared`` namespace."""

    source: str = Field(description="Skill source plugin: git, registry, or any installed plugin.")
    title: str | None = None
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="Non-secret settings (see the plugin's schema).")
    secrets: dict[str, str] = Field(default_factory=dict, description="Config field -> secret name in `shared`.")
    namespaces: list[str] = Field(default_factory=list, description="Namespaces that may browse and import (empty: all).")
    enabled: bool = True

    @model_validator(mode="after")
    def _no_clear_text_credentials(self) -> SkillSourceSpec:
        if bad := credential_fields(self.config):
            raise ValueError(f"config {bad} look like credentials; map them to secrets in `shared` under secrets")
        return self


class EvalCase(Model):
    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict, description="Available to graders as `expect`.")
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Carried to results and eval sinks.")


class EvalPlatformSpec(Model):
    """An eval platform connected once by an admin (tenant-wide, ``eval-platforms:manage``).

    Evals point at it with ``platform: <name>`` instead of repeating sink settings.
    Credentials are secret names in the tenant's ``shared`` namespace.
    """

    description: str | None = None
    sink: str = Field(description="Eval sink plugin, e.g. 'braintrust' or 'langfuse'.")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Sink settings; credentials as secret names (api_key_secret, secret_key_secret)."
    )

    @model_validator(mode="after")
    def _no_clear_text_credentials(self) -> EvalPlatformSpec:
        leaked = [k for k in credential_fields(self.config) if not k.endswith("_secret")]
        if leaked:
            raise ValueError(f"config keys {leaked} look like credentials: store them as secrets and use <name>_secret")
        return self


class _PlatformOrSink(Model):
    sink: str | None = Field(None, description="Eval sink plugin, e.g. 'braintrust' or 'langfuse'.")
    platform: str | None = Field(None, description="An EvalPlatform; its sink and settings apply, `config` overrides them.")
    config: dict[str, Any] = Field(default_factory=dict, description="Sink settings; secrets by name (api_key_secret).")

    @model_validator(mode="after")
    def _one_target(self) -> _PlatformOrSink:
        if bool(self.sink) == bool(self.platform):
            raise ValueError("set exactly one of `sink` or `platform`")
        if self.platform and (endpoints := endpoint_fields(self.config)):
            raise ValueError(f"{', '.join(endpoints)}: an eval platform's endpoints are set on the platform only")
        return self


class EvalDatasetRef(_PlatformOrSink):
    """Cases imported from an eval platform (``shoal.eval_sinks`` plugin) each time the eval runs."""

    name: str = Field(description="Dataset name on the platform.")
    limit: int | None = Field(None, ge=1)


class EvalRunQuery(Model):
    """Grade past runs instead of starting new ones (online evals over production runs)."""

    workflow: str | None = Field(None, description="Defaults to the eval's workflow.")
    status: list[Literal["succeeded", "failed"]] = Field(default_factory=lambda: ["succeeded"])
    since: Duration | None = Field(None, description="Only runs created within this window.")
    labels: dict[str, str] = Field(default_factory=dict)
    version: int | None = Field(None, description="Only runs of this workflow version.")
    limit: int = Field(50, ge=1, le=5000)


class Grader(Model):
    """Scores one case from 0 to 1.

    * ``expression``: ``check`` is evaluated with ``outputs``, ``expect``, ``inputs``, ``run`` and
      ``case``; true/false score 1/0 and a number is used as the score (clamped to 0..1).
    * ``json_schema``: ``subject`` must validate against ``schema``.
    * ``llm_judge``: a pinned judge model places ``subject`` on the ``levels`` rubric.
    * ``decision``: a decision model (Jev or the LLM fallback) answers a ``score`` or ``boolean`` question.
    """

    name: str
    type: Literal["expression", "json_schema", "llm_judge", "decision"] = "expression"
    check: str | None = Field(None, description="expression: evaluated with outputs, expect, inputs, run, case.")
    schema_: dict[str, Any] | None = Field(None, alias="schema", description="json_schema: the JSON schema.")
    question: DecisionQuestion | None = Field(None, description="decision: a score or boolean question.")
    state: Any = Field("{{ outputs }}", description="decision: state to send (expressions allowed).")
    subject: Any = Field("{{ outputs }}", description="json_schema / llm_judge: what is graded (expressions allowed).")
    rubric: str | None = Field(None, description="llm_judge: grading instructions.")
    levels: list[str] = Field(
        default_factory=lambda: ["fail", "pass"], description="llm_judge: ordered rubric levels, worst first."
    )
    model: str = Field("claude-sonnet-5-5", description="llm_judge: pinned judge model.")
    gateway: str = Field("anthropic", description="llm_judge: Gateway resource or provider plugin.")
    provider: str | None = Field(None, description="decision: provider plugin; defaults to the eval's.")
    config: dict[str, Any] = Field(default_factory=dict, description="Provider or gateway settings.")
    weight: float = Field(1.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Grader:
        if self.type == "expression" and not self.check:
            raise ValueError(f"grader '{self.name}': expression graders need 'check'")
        if self.type == "json_schema" and not self.schema_:
            raise ValueError(f"grader '{self.name}': json_schema graders need 'schema'")
        if self.type == "llm_judge" and (not self.rubric or len(self.levels) < 2):
            raise ValueError(f"grader '{self.name}': llm_judge graders need a 'rubric' and at least 2 levels")
        if self.type == "decision" and (self.question is None or self.question.type == "choice"):
            raise ValueError(f"grader '{self.name}': decision graders need a score or boolean 'question'")
        return self


class EvalSinkRef(_PlatformOrSink):
    """Export each finished eval run (traces and scores) to an eval platform."""


class EvalGate(Model):
    """Pass/fail rule for an eval run, used to gate promotion of a new version."""

    fail_under: float | None = Field(None, ge=0, le=1, description="Minimum mean score.")
    max_regression: float | None = Field(
        None, ge=0, le=1, description="Maximum drop in mean score against the baseline run."
    )
    baseline: str | None = Field(
        None, description="'previous' (last completed run), 'version:N' (latest run at workflow version N) or a run id."
    )


class EvalVariant(Model):
    """One arm of a comparison: a workflow version plus input overrides (e.g. a different agent or model policy)."""

    name: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    version: int | None = Field(None, ge=1, description="Workflow version; defaults to the current one.")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Merged over every case's inputs.")


class EvalSpec(Model):
    description: str | None = None
    workflow: str
    variants: list[EvalVariant] = Field(
        default_factory=list, description="Arms to compare; defaults to one 'default' arm at the current version."
    )
    cases: list[EvalCase] = Field(default_factory=list)
    dataset: EvalDatasetRef | None = None
    from_runs: EvalRunQuery | None = None
    graders: list[Grader] = Field(min_length=1)
    threshold: float = Field(0.8, ge=0, le=1, description="A case passes when its weighted score is at least this.")
    require_success: bool | None = Field(
        None,
        description="A case whose workflow run did not succeed scores 0 and fails, whatever its graders say "
        "(their scores are still recorded). Defaults to true for cases and false for from_runs, which picks "
        "run statuses itself; set false to grade runs that are expected to fail.",
    )
    concurrency: int = Field(5, ge=1)
    decision_provider: str | None = None
    gate: EvalGate = Field(default_factory=EvalGate)
    sinks: list[EvalSinkRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> EvalSpec:
        if not (self.cases or self.dataset or self.from_runs):
            raise ValueError("an eval needs 'cases', a 'dataset' or 'from_runs'")
        if self.from_runs and (self.cases or self.dataset):
            raise ValueError("'from_runs' grades past runs and cannot be combined with 'cases' or 'dataset'")
        names = [c.name for c in self.cases]
        if len(names) != len(set(names)):
            raise ValueError("case names must be unique")
        graders = [g.name for g in self.graders]
        if len(graders) != len(set(graders)):
            raise ValueError("grader names must be unique")
        variants = [v.name for v in self.variants]
        if len(variants) != len(set(variants)):
            raise ValueError("variant names must be unique")
        return self


# --------------------------------------------------------------------------- envelopes


class Metadata(Model):
    name: str = Field(pattern=NAME_PATTERN)
    namespace: str | None = Field(None, pattern=NAME_PATTERN, description="Namespace (defaults to the current one).")
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] | None = Field(
        None, description="Free-form notes for tools (e.g. the builder's layout). Not versioned; omitted means keep."
    )


class ResourceBase(Model):
    api_version: Literal["shoal.dev/v1"] = Field(API_VERSION, alias="apiVersion")
    metadata: Metadata

    plural: ClassVar[str]

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class Workflow(ResourceBase):
    kind: Literal["Workflow"] = "Workflow"
    spec: WorkflowSpec
    plural: ClassVar[str] = "workflows"


class Agent(ResourceBase):
    kind: Literal["Agent"] = "Agent"
    spec: AgentSpec
    plural: ClassVar[str] = "agents"


class Skill(ResourceBase):
    kind: Literal["Skill"] = "Skill"
    spec: SkillSpec
    plural: ClassVar[str] = "skills"


class SandboxProfile(ResourceBase):
    kind: Literal["SandboxProfile"] = "SandboxProfile"
    spec: SandboxProfileSpec
    plural: ClassVar[str] = "sandbox-profiles"


class Connection(ResourceBase):
    kind: Literal["Connection"] = "Connection"
    spec: ConnectionSpec
    plural: ClassVar[str] = "connections"


class Trigger(ResourceBase):
    kind: Literal["Trigger"] = "Trigger"
    spec: TriggerSpec
    plural: ClassVar[str] = "triggers"


class Campaign(ResourceBase):
    kind: Literal["Campaign"] = "Campaign"
    spec: CampaignSpec
    plural: ClassVar[str] = "campaigns"


class Identity(ResourceBase):
    kind: Literal["Identity"] = "Identity"
    spec: IdentitySpec
    plural: ClassVar[str] = "identities"


class CloudIdentity(ResourceBase):
    kind: Literal["CloudIdentity"] = "CloudIdentity"
    spec: CloudIdentitySpec
    plural: ClassVar[str] = "cloud-identities"


class Gateway(ResourceBase):
    kind: Literal["Gateway"] = "Gateway"
    spec: GatewaySpec
    plural: ClassVar[str] = "gateways"


class ModelPolicy(ResourceBase):
    kind: Literal["ModelPolicy"] = "ModelPolicy"
    spec: ModelPolicySpec
    plural: ClassVar[str] = "model-policies"


class Blueprint(ResourceBase):
    kind: Literal["Blueprint"] = "Blueprint"
    spec: BlueprintSpec
    plural: ClassVar[str] = "blueprints"


class Eval(ResourceBase):
    kind: Literal["Eval"] = "Eval"
    spec: EvalSpec
    plural: ClassVar[str] = "evals"


class EvalPlatform(ResourceBase):
    kind: Literal["EvalPlatform"] = "EvalPlatform"
    spec: EvalPlatformSpec
    plural: ClassVar[str] = "eval-platforms"


class SkillSource(ResourceBase):
    kind: Literal["SkillSource"] = "SkillSource"
    spec: SkillSourceSpec
    plural: ClassVar[str] = "skill-sources"


AnyResource = Annotated[
    Workflow
    | Agent
    | Skill
    | SandboxProfile
    | Connection
    | Trigger
    | Campaign
    | Identity
    | CloudIdentity
    | Gateway
    | ModelPolicy
    | Blueprint
    | Eval
    | SkillSource
    | EvalPlatform,
    Field(discriminator="kind"),
]

RESOURCE_KINDS: dict[str, type[ResourceBase]] = {
    cls.model_fields["kind"].default: cls
    for cls in (
        Workflow, Agent, Skill, SandboxProfile, Connection, Trigger, Campaign,
        Identity, CloudIdentity, Gateway, ModelPolicy, Blueprint, Eval, SkillSource, EvalPlatform,
    )
}
KIND_BY_PLURAL: dict[str, str] = {cls.plural: kind for kind, cls in RESOURCE_KINDS.items()}


def parse_resource(document: dict[str, Any]) -> ResourceBase:
    kind = document.get("kind")
    cls = RESOURCE_KINDS.get(kind or "")
    if cls is None:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(RESOURCE_KINDS)}")
    return cls.model_validate(document)


# --------------------------------------------------------------------------- runtime enums


class RunStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled)


class StepStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (StepStatus.succeeded, StepStatus.failed, StepStatus.skipped, StepStatus.cancelled)


class CampaignStatus(enum.StrEnum):
    draft = "draft"
    running = "running"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"


class TargetStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"
