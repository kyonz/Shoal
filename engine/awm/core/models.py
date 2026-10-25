"""Declarative resource models.

Every user-authored object (workflow, agent, skill, sandbox profile, connection,
trigger, campaign) is a pydantic model wrapped in a Kubernetes-style envelope:

    apiVersion: awm.dev/v1
    kind: Workflow
    metadata: {name: jira-bugfix}
    spec: {...}

The same models validate YAML files, API payloads and stored state, and their
JSON schemas are published through the API so the UI and SDKs can render forms.
"""

from __future__ import annotations

import enum
from datetime import timedelta
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from awm.core.duration import Duration

API_VERSION = "awm.dev/v1"
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


class ScriptStep(StepBase):
    """Run a shell script in a sandbox. Output: ``exit_code``, ``stdout``, ``stderr``."""

    type: Literal["script"] = "script"  # type: ignore[misc]
    run: str
    sandbox: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str | None = None
    fail_on_error: bool = True


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


class WaitStep(StepBase):
    """Durable wait for a duration, until a time, or until a condition holds."""

    type: Literal["wait"] = "wait"  # type: ignore[misc]
    duration: Duration | None = None
    until: str | None = Field(None, description="Expression producing an ISO-8601 timestamp.")
    condition: WaitCondition | None = None
    max_wait: Duration | None = Field(None, description="Give up waiting for the condition after this long.")
    on_timeout: Literal["fail", "continue"] = "continue"

    @model_validator(mode="after")
    def _one_mode(self) -> WaitStep:
        if not (self.duration or self.until or self.condition):
            raise ValueError("wait step needs one of: duration, until, condition")
        return self


class ApprovalStep(StepBase):
    """Human-in-the-loop gate resolved through the API or UI."""

    type: Literal["approval"] = "approval"  # type: ignore[misc]
    message: str
    approvers: list[str] = Field(default_factory=list)
    max_wait: Duration | None = None
    on_timeout: Literal["reject", "approve"] = "reject"


class WorkflowCallStep(StepBase):
    """Run another workflow as a child run, optionally once per item (fan-out)."""

    type: Literal["workflow"] = "workflow"  # type: ignore[misc]
    workflow: str
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
    provider_config: dict[str, Any] = Field(default_factory=dict)
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
    provider_config: dict[str, Any] = Field(default_factory=dict)
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
    provider_config: dict[str, Any] = Field(default_factory=dict)
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
    runtime: str = Field("tool-loop", description="Agent runtime plugin: tool-loop or cli.")
    instructions: str = Field("", description="System prompt.")
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(
        default_factory=lambda: ["shell", "read_file", "write_file", "list_files"],
        description="Built-in tools, integration actions ('github.create_pull_request', 'github.*').",
    )
    subagents: list[str] = Field(default_factory=list, description="Agents this agent may delegate to.")
    credentials: list[str] = Field(
        default_factory=list,
        description="Integrations whose short-lived credentials are injected into the sandbox env, e.g. "
        "'github' or 'github:github-acme' (integration:connection).",
    )
    sandbox: str | None = None
    env: dict[str, str] = Field(default_factory=dict, description="Sandbox env; values may use expressions.")
    limits: AgentLimits = Field(default_factory=AgentLimits)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    guard: ToolGuardSpec | None = Field(None, description="Optional decision-model screening of tool calls.")


class SkillSpec(Model):
    description: str
    instructions: str = Field(description="SKILL.md body.")
    files: dict[str, str] = Field(default_factory=dict, description="Relative path -> file content.")


class NetworkPolicy(Model):
    egress: Literal["allow", "deny"] = "allow"
    allow_hosts: list[str] = Field(default_factory=list)


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


class ConnectionSpec(Model):
    integration: str
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="Non-secret settings.")
    secrets: dict[str, str] = Field(default_factory=dict, description="Connection field -> secret name.")


class TriageSpec(Model):
    """Semantic trigger filter: ask a decision model about the event before starting a run."""

    provider: str | None = None
    provider_config: dict[str, Any] = Field(default_factory=dict)
    state: Any = Field("{{ event }}", description="What the questions are about (expressions allowed).")
    questions: dict[str, DecisionQuestion] = Field(min_length=1)
    when: str = Field(description="Expression over `answers` (and `event`); fire only when true.")


class TriggerSpec(Model):
    description: str | None = None
    workflow: str | None = Field(None, description="Workflow to start.")
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
        default_factory=list, description="AWM actors that are this person, e.g. 'user:dana@acme.com'."
    )
    github_token_secret: str | None = Field(None, description="Secret holding the user's linked GitHub OAuth token.")


class GatewaySpec(Model):
    """How model requests reach a provider (direct API, LiteLLM, Portkey, OpenRouter, Bedrock, ...)."""

    description: str | None = None
    type: str = Field("anthropic", description="Provider plugin: anthropic, openai-compatible, scripted, ...")
    base_url: str | None = None
    api_key_secret: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


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


class EvalCase(Model):
    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict, description="Available to graders as `expect`.")


class Grader(Model):
    name: str
    type: Literal["expression", "decision"] = "expression"
    check: str | None = Field(None, description="expression: evaluated with outputs, expect, run.")
    question: DecisionQuestion | None = Field(None, description="decision: a score or boolean question.")
    state: Any = Field("{{ outputs }}", description="decision: state to send (expressions allowed).")
    weight: float = Field(1.0, gt=0)


class EvalSpec(Model):
    description: str | None = None
    workflow: str
    cases: list[EvalCase] = Field(min_length=1)
    graders: list[Grader] = Field(min_length=1)
    threshold: float = Field(0.8, ge=0, le=1)
    concurrency: int = Field(5, ge=1)
    decision_provider: str | None = None


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
    api_version: Literal["awm.dev/v1"] = Field(API_VERSION, alias="apiVersion")
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


AnyResource = Annotated[
    Workflow
    | Agent
    | Skill
    | SandboxProfile
    | Connection
    | Trigger
    | Campaign
    | Identity
    | Gateway
    | ModelPolicy
    | Blueprint
    | Eval,
    Field(discriminator="kind"),
]

RESOURCE_KINDS: dict[str, type[ResourceBase]] = {
    cls.model_fields["kind"].default: cls
    for cls in (
        Workflow, Agent, Skill, SandboxProfile, Connection, Trigger, Campaign,
        Identity, Gateway, ModelPolicy, Blueprint, Eval,
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
