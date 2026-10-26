"""Run Claude Code headless (``claude -p``) inside the step's sandbox, through a central gateway.

An agent selects it with ``runtime: claude-code`` and names a Gateway of
``type: claude-code`` as its ``provider``. The gateway, managed by admins,
decides how Claude Code signs in:

* ``config.mode: api_key`` (the default) - the gateway's ``api_key_secret``
  becomes ``ANTHROPIC_API_KEY``; ``base_url`` and headers reach the CLI as
  ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_CUSTOM_HEADERS`` (LiteLLM, Portkey, ...).
* ``config.mode: subscription`` - each run uses the Claude subscription (Pro,
  Max, Team, Enterprise) of the person who started it, which they link once
  from Settings (``/v1/me/claude-code``). Anthropic's terms do not allow one
  person's subscription to serve other people's runs, so there is no shared
  subscription token, and runs started by service accounts or schedules
  without a person fail. The admin sets
  ``config.acknowledge_subscription_terms: true`` to turn the mode on.

``runtime_config`` (all optional):

* ``allowed_tools`` / ``disallowed_tools`` - Claude Code tool rules, e.g. ``["Bash(git *)", "Edit"]``.
* ``permission_mode`` - default ``bypassPermissions`` (the sandbox is the boundary).
* ``max_turns`` - defaults to the agent's ``limits.max_turns``.
* ``max_budget_usd`` - stop the CLI at this spend (API key mode).
* ``fallback_model``, ``extra_args`` (list of extra CLI flags).
* ``cli`` - the CLI to run (default ``claude``, from PATH or ``~/.local/bin``).
* ``install`` - command that installs the CLI when ``claude`` is not on PATH
  (default: Anthropic's native installer); ``false`` to never install.
* ``setup`` - command run before the CLI starts.
* ``poll_interval`` - seconds between reads of the event stream (default 1).
* ``timeout`` - seconds (defaults to the agent's ``limits.timeout``).

The CLI runs in the background and writes ``--output-format stream-json`` to a
file in the sandbox; the runtime tails it and emits each message as an
``agent.claude_code.*`` event, so runs show progress while Claude Code works.
The step's outputs come back through ``--json-schema`` structured output.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from typing import Any

from shoal.agents.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderUnavailable
from shoal.agents.runtime import AgentInvocation, AgentResult, AgentRuntime, install_skills
from shoal.core.errors import CredentialRequired, StepError
from shoal.core.ids import new_id
from shoal.core.models import ParamValidationError, validate_params
from shoal.core.scope import SHARED_NAMESPACE
from shoal.sandbox.base import shell_quote

GATEWAY_TYPE = "claude-code"
MODES = ("api_key", "subscription")
TOKEN_FIELD = "oauth_token"
STATE_DIR = ".shoal/claude-code"
DEFAULT_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
SUBSCRIPTION_TERMS = (
    "Subscription mode uses each person's own Claude plan for the runs they start. Anthropic's terms do not allow "
    "routing Free, Pro or Max credentials on behalf of other people, and plan usage limits assume ordinary, "
    "individual use. Use an API key for shared, scheduled or service-account runs."
)

PROMPT_TEMPLATE = """{prompt}

{skills}When you are done, reply with the structured output: {outputs}
Do not ask questions; no human is watching this session.
"""


class ClaudeCodeGatewayProvider(ModelProvider):
    """Claude Code CLI (claude -p): agents with runtime claude-code, or any model call, on an API key or a person's own plan.

    As a model gateway (the assistant, tool-loop agents) each call runs the
    unmodified CLI once on the Shoal server, with Claude Code's own tools turned
    off: the conversation and the caller's tool definitions go into the prompt,
    and the next turn (text and tool calls) comes back as structured output.
    In subscription mode the caller signs the call in with the token of the
    person it acts for (:func:`sign_in`); calls that act for nobody fail.
    """

    name = GATEWAY_TYPE

    def __init__(self) -> None:
        self.cli = "claude"

    def setup(self, engine: Any) -> None:
        self.cli = engine.settings.claude_code_cli

    async def complete(self, request: ModelRequest) -> ModelResponse:
        config = request.config
        # The server's own environment minus any Anthropic credentials it has: only the gateway's or the person's sign in.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "SHOAL_"))}
        if config.get("oauth_token"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = config["oauth_token"]
            mode = "subscription"
        elif config.get("api_key"):
            env["ANTHROPIC_API_KEY"] = config["api_key"]
            mode = "api_key"
        else:
            raise StepError(
                "this claude-code gateway runs on each person's own Claude plan, and this call acts for nobody "
                "(decisions, graders and service accounts need an API key gateway)"
            )
        if config.get("base_url"):
            env["ANTHROPIC_BASE_URL"] = config["base_url"]
        if config.get("headers"):
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in config["headers"].items())
        tools = [t.name for t in request.tools]
        schema = turn_schema(tools)
        system = (request.system + "\n\n" if request.system else "") + TURN_INSTRUCTIONS + describe_tools(request.tools)
        args = [
            self.cli, "-p", "--output-format", "json", "--model", request.model, "--tools", "",
            "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
            "--max-turns", str(int(config.get("max_turns", 4))), "--system-prompt", system, "--json-schema", json.dumps(schema),
        ]
        with tempfile.TemporaryDirectory(prefix="shoal-claude-") as home:  # no settings, history or project files
            env.update({"HOME": home, "IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"})
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=home, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise StepError(f"the Claude Code CLI ('{self.cli}') is not installed on this Shoal server (SHOAL_CLAUDE_CODE_CLI)") from exc
            try:
                out, err = await asyncio.wait_for(proc.communicate(transcript(request.messages).encode()), float(config.get("timeout", 600)))
            except TimeoutError as exc:
                proc.kill()
                raise ProviderUnavailable("Claude Code timed out") from exc
        try:
            result = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            stderr = err.decode(errors="replace")[-2000:]
            raise StepError(f"Claude Code failed (exit {proc.returncode}): {stderr or 'no output'}") from exc
        if result.get("is_error"):
            text = str(result.get("result") or result.get("subtype") or "error")
            if any(s in text.lower() for s in ("rate limit", "overloaded", "usage limit", "529", "429")):
                raise ProviderUnavailable(f"Claude Code: {text[:500]}")
            raise StepError(f"Claude Code: {text[:1000]}")
        return to_response(result, tools, mode, request.model)


TURN_INSTRUCTIONS = """You are the model behind another application's agent loop. You have no tools of your own here.
The conversation so far is given as a transcript. Reply with the assistant's next turn as structured output:
`text` is what you say, and `tool_calls` lists the application's tools you want to call now (empty when you are done).
Tool results come back in the next transcript as <tool_result> blocks.
"""


def describe_tools(tools: list[Any]) -> str:
    if not tools:
        return "\nThere are no application tools in this conversation: answer with text only.\n"
    lines = ["\nApplication tools (name, description and JSON Schema of the input):"]
    for tool in tools:
        lines.append(f"\n## {tool.name}\n{tool.description}\nInput schema: {json.dumps(tool.input_schema)}")
    return "\n".join(lines) + "\n"


def turn_schema(tools: list[str]) -> dict[str, Any]:
    call: dict[str, Any] = {
        "type": "object",
        "properties": {"name": {"type": "string", **({"enum": tools} if tools else {})}, "input": {"type": "object"}},
        "required": ["name", "input"],
    }
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_calls": {"type": "array", "items": call, "maxItems": 0 if not tools else 32}},
        "required": ["text", "tool_calls"],
    }


def transcript(messages: list[dict[str, Any]]) -> str:
    """Anthropic-shaped messages as a plain-text transcript for the prompt."""
    parts = []
    for message in messages:
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        body = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                body.append(block.get("text", ""))
            elif kind == "tool_use":
                body.append(f'<tool_call id="{block.get("id")}" name="{block.get("name")}">{json.dumps(block.get("input"))}</tool_call>')
            elif kind == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "".join(c.get("text", "") for c in result if isinstance(c, dict))
                error = ' is_error="true"' if block.get("is_error") else ""
                body.append(f'<tool_result id="{block.get("tool_use_id")}"{error}>{result}</tool_result>')
            elif kind in ("image", "document"):
                body.append(f"[{kind} omitted]")
        parts.append(f"<{message['role']}>\n" + "\n".join(body) + f"\n</{message['role']}>")
    return "\n\n".join(parts) + "\n\nWrite the assistant's next turn."


def to_response(result: dict[str, Any], tools: list[str], mode: str, model: str) -> ModelResponse:
    turn = result.get("structured_output")
    if not isinstance(turn, dict):
        try:
            turn = json.loads(str(result.get("result", "")))
        except ValueError:
            turn = {"text": str(result.get("result", "")), "tool_calls": []}
    content: list[dict[str, Any]] = []
    if text := str(turn.get("text") or "").strip():
        content.append({"type": "text", "text": text})
    for call in turn.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("name") in tools:
            content.append({"type": "tool_use", "id": new_id("toolu"), "name": call["name"], "input": call.get("input") or {}})
    usage = usage_of(result, mode)
    usage.pop("model_calls", None)
    return ModelResponse(content=content, stop_reason="tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
                         usage=usage, model=model)


async def personal_token(engine: Any, tenant_id: str, gateway: str, owner: str | None, who: str | None) -> str:
    """The Claude Code token `owner` linked for `gateway`; raises CredentialRequired when there is none."""
    values = await engine.user_credentials.get(tenant_id, owner, SHARED_NAMESPACE, gateway) if owner else None
    if not values or not values.get(TOKEN_FIELD):
        raise CredentialRequired(
            f"gateway '{gateway}' runs Claude Code on each person's own Claude subscription and "
            f"{who or owner or 'the person this acts for'} has not linked one"
            + ("" if owner else " (service accounts and schedules cannot use subscription mode)"),
            tenant_id=tenant_id, owner=owner, namespace=SHARED_NAMESPACE, connection=gateway, fields=[TOKEN_FIELD],
        )
    return str(values[TOKEN_FIELD])


async def sign_in(engine: Any, tenant_id: str, target: Any, owner: str | None, *, who: str | None = None, redactor: Any = None) -> None:
    """Sign a claude-code model target in for the person a call acts for (subscription mode). Others are left alone."""
    if target.provider_name != GATEWAY_TYPE:
        return
    resource = await engine.store.find_resource(tenant_id, "Gateway", target.gateway)
    spec = getattr(resource, "spec", None)
    if spec is None:
        raise StepError(f"'{GATEWAY_TYPE}' needs a Gateway resource of that type (admins create it under Gateways)")
    if problem := gateway_problem(spec):
        raise StepError(f"gateway '{target.gateway}': {problem}")
    if gateway_mode(spec.config, has_api_key=bool(spec.api_key_secret)) == "subscription":
        token = await personal_token(engine, tenant_id, target.gateway, owner, who)
        if redactor is not None:
            redactor.add(token)
        target.config["oauth_token"] = token


def gateway_mode(config: dict[str, Any], *, has_api_key: bool) -> str:
    mode = config.get("mode") or ("api_key" if has_api_key else "subscription")
    if mode not in MODES:
        raise StepError(f"claude-code gateway mode must be one of {list(MODES)}, not '{mode}'")
    return mode


def gateway_problem(spec: Any) -> str | None:
    """Why a claude-code gateway cannot run yet (shown on the admin screen), or None."""
    try:
        mode = gateway_mode(spec.config, has_api_key=bool(spec.api_key_secret))
    except StepError as exc:
        return exc.message
    if mode == "api_key" and not spec.api_key_secret:
        return "api_key mode needs api_key_secret"
    if mode == "subscription" and spec.config.get("acknowledge_subscription_terms") is not True:
        return "subscription mode needs config.acknowledge_subscription_terms: true. " + SUBSCRIPTION_TERMS
    return None


async def gateway_env(inv: AgentInvocation) -> tuple[str, dict[str, str]]:
    """The gateway's mode and the env that signs Claude Code in for this invocation."""
    ctx = inv.ctx
    gateway = inv.agent.provider
    resource = await ctx.engine.store.find_resource(ctx.tenant_id, "Gateway", gateway)
    spec = getattr(resource, "spec", None)
    if spec is None or spec.type != GATEWAY_TYPE:
        raise StepError(
            f"agent provider '{gateway}' must name a Gateway of type {GATEWAY_TYPE} for runtime claude-code "
            "(admins create it under Gateways)"
        )
    if problem := gateway_problem(spec):
        raise StepError(f"gateway '{gateway}': {problem}")
    target = await ctx.engine.models.gateway_target(ctx.tenant_id, gateway, inv.agent.model, redactor=ctx.redactor)
    config = target.config
    env: dict[str, str] = {}
    if config.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = config["base_url"]
    if config.get("headers"):
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in config["headers"].items())
    mode = gateway_mode(spec.config, has_api_key=bool(spec.api_key_secret))
    if mode == "api_key":
        env["ANTHROPIC_API_KEY"] = config["api_key"]
        return mode, env
    owner = await ctx.engine.user_credentials.owner_for(ctx.tenant_id, ctx.run.initiator)
    token = await personal_token(ctx.engine, ctx.tenant_id, gateway, owner, (ctx.run.initiator or {}).get("display_name"))
    ctx.redactor.add(token)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return mode, env


def cli_args(inv: AgentInvocation, schema: dict[str, Any], prompt_file: str) -> str:
    cfg = inv.agent.runtime_config
    if inv.agent.model.startswith("policy:"):
        raise StepError("runtime claude-code takes a model name (or alias such as 'opus'), not a model policy")
    args = [
        str(cfg.get("cli", "claude")), "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--model", inv.agent.model,
        "--permission-mode", str(cfg.get("permission_mode", "bypassPermissions")),
        "--max-turns", str(int(cfg.get("max_turns") or inv.agent.limits.max_turns)),
        "--json-schema", json.dumps(schema),
    ]
    if inv.agent.instructions:
        args += ["--append-system-prompt", inv.agent.instructions]
    for key, flag in (("allowed_tools", "--allowed-tools"), ("disallowed_tools", "--disallowed-tools")):
        if cfg.get(key):
            args += [flag, *[str(t) for t in cfg[key]]]
    if cfg.get("max_budget_usd") is not None:
        args += ["--max-budget-usd", str(float(cfg["max_budget_usd"]))]
    if cfg.get("fallback_model"):
        args += ["--fallback-model", str(cfg["fallback_model"])]
    args += [str(a) for a in cfg.get("extra_args", [])]
    return " ".join(shell_quote(a) for a in args) + f" < {shell_quote(prompt_file)}"


def describe(message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """A stream-json message as (event type, data) for the run's event log."""
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        return "agent.claude_code.init", {k: message.get(k) for k in ("model", "tools", "permissionMode", "claude_code_version") if k in message}
    if kind == "assistant":
        blocks = (message.get("message") or {}).get("content") or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        tools = [{"name": b.get("name"), "input": _clip(b.get("input"))} for b in blocks if b.get("type") == "tool_use"]
        if not text and not tools:
            return None
        return "agent.claude_code.message", {"text": text[:4000], "tool_calls": tools}
    if kind == "user":
        blocks = (message.get("message") or {}).get("content") or []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if not results:
            return None
        return "agent.claude_code.tool_results", {"results": [{"is_error": bool(b.get("is_error")), "content": _clip(b.get("content"))} for b in results]}
    if kind == "result":
        return "agent.claude_code.result", {k: message.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd", "duration_ms") if k in message}
    return None


def _clip(value: Any, limit: int = 1000) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def usage_of(result: dict[str, Any], mode: str) -> dict[str, float]:
    usage: dict[str, float] = {}
    u = result.get("usage") or {}
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        if isinstance(u.get(key), int | float):
            usage[key] = u[key]
    # On a subscription the plan pays; the CLI's figure is what the same tokens would cost on the API.
    if mode == "api_key" and isinstance(result.get("total_cost_usd"), int | float):
        usage["cost_usd"] = float(result["total_cost_usd"])
    usage["model_calls"] = float(result.get("num_turns") or 0)
    return usage


class ClaudeCodeRuntime(AgentRuntime):
    name = "claude-code"

    async def run(self, inv: AgentInvocation) -> AgentResult:
        cfg = inv.agent.runtime_config
        sandbox = inv.sandbox
        mode, auth_env = await gateway_env(inv)
        state = inv.ctx.state.get("claude_code") or {}
        if state.get("attempt") != inv.ctx.attempt:
            state = {"attempt": inv.ctx.attempt}
        inv.ctx.state["claude_code"] = state
        paths = {name: sandbox.resolve(f"{STATE_DIR}/{name}") for name in ("prompt.md", "stream.jsonl", "stderr.log", "exit_code")}
        # Credentials travel in the exec env (inherited by the background CLI), never on a command line.
        env = {**inv.env, **auth_env, "IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"}
        timeout = float(cfg.get("timeout") or inv.agent.limits.timeout.total_seconds())
        poll = float(cfg.get("poll_interval", 1.0))

        if state.get("started") and await _read(sandbox, paths["stream.jsonl"]) is None:
            state.pop("started")  # a fresh sandbox after a restart: start over
        if not state.get("started"):
            await self._prepare(inv, env, paths, timeout)
            command = cli_args(inv, inv.finish_schema(), paths["prompt.md"])
            q = {k: shell_quote(v) for k, v in paths.items()}
            body = f'export PATH="$HOME/.local/bin:$PATH"; {command} > {q["stream.jsonl"]} 2> {q["stderr.log"]}; echo $? > {q["exit_code"]}'
            launch = (
                f"mkdir -p {shell_quote(sandbox.resolve(STATE_DIR))} && rm -f {q['exit_code']} && : > {q['stream.jsonl']} && "
                f"nohup sh -c {shell_quote(body)} > /dev/null 2>&1 &"
            )
            started = await sandbox.exec(launch, env=env, timeout=30)
            if not started.ok:
                raise StepError("could not start Claude Code", details=started.to_dict(4000))
            state.update(started=True, offset=0, deadline=time.time() + timeout)
            await inv.ctx.checkpoint()
            await inv.ctx.emit("agent.claude_code.started", {"agent": inv.agent_name, "mode": mode, "model": inv.agent.model})

        result = await self._follow(inv, paths, state, poll)
        if result.get("is_error") or result.get("subtype") != "success":
            raise StepError(
                f"Claude Code stopped: {result.get('subtype') or 'error'}",
                retryable=result.get("subtype") == "error_during_execution",
                details={k: result.get(k) for k in ("subtype", "num_turns", "result", "errors") if k in result},
            )
        raw = result.get("structured_output")
        if not isinstance(raw, dict):
            try:
                raw = json.loads(str(result.get("result", "")).strip().removeprefix("```json").removesuffix("```"))
            except ValueError as exc:
                raise StepError("Claude Code did not return the structured outputs", details={"result": str(result.get("result"))[:4000]}) from exc
        raw = dict(raw)
        summary = str(raw.pop("summary", ""))
        try:
            outputs = validate_params(inv.outputs, raw)
        except ParamValidationError as exc:
            raise StepError(f"agent outputs invalid: {exc}", details={"outputs": raw}) from exc
        return AgentResult(outputs=outputs, summary=summary, usage=usage_of(result, mode), turns=int(result.get("num_turns") or 0))

    async def _prepare(self, inv: AgentInvocation, env: dict[str, str], paths: dict[str, str], timeout: float) -> None:
        cfg = inv.agent.runtime_config
        sandbox = inv.sandbox
        skills = await install_skills(inv)
        skills_text = (
            "Skills available (read the SKILL.md before use):\n" + "\n".join(f"- {n}: {d} ({p})" for n, d, p in skills) + "\n\n"
            if skills
            else ""
        )
        names = ", ".join([*inv.outputs, "summary"])
        await sandbox.write_file(paths["prompt.md"], PROMPT_TEMPLATE.format(prompt=inv.prompt, skills=skills_text, outputs=names))
        install = cfg.get("install", DEFAULT_INSTALL)
        if install:
            cli = shell_quote(str(cfg.get("cli", "claude")))
            check = await sandbox.exec(f'PATH="$HOME/.local/bin:$PATH" command -v {cli} || ({install})', env=env, timeout=timeout)
            if not check.ok:
                raise StepError("could not install Claude Code in the sandbox", details=check.to_dict(4000))
        if cfg.get("setup"):
            setup = await sandbox.exec(cfg["setup"], env=env, timeout=timeout)
            if not setup.ok:
                raise StepError("agent setup command failed", details=setup.to_dict(4000))

    async def _follow(self, inv: AgentInvocation, paths: dict[str, str], state: dict[str, Any], poll: float) -> dict[str, Any]:
        """Tail the stream file, emitting events, until the CLI exits. Returns its result message."""
        sandbox = inv.sandbox
        result: dict[str, Any] | None = None
        pending = ""
        while True:
            done = await _read(sandbox, paths["exit_code"])
            chunk = await sandbox.exec(f"tail -c +{int(state['offset']) + 1} {shell_quote(paths['stream.jsonl'])} 2>/dev/null", timeout=60)
            data = chunk.stdout
            state["offset"] = int(state["offset"]) + len(data.encode())
            pending += data
            *lines, pending = pending.split("\n")
            for line in lines:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("type") == "result":
                    result = message
                if (event := describe(message)) is not None:
                    await inv.ctx.emit(*event)
            if done is not None:
                break
            if time.time() > float(state["deadline"]):
                await sandbox.exec("pkill -f 'claude -p' || true", timeout=30)
                raise StepError("Claude Code timed out", retryable=True)
            await asyncio.sleep(poll)
        if result is None:
            stderr = (await _read(sandbox, paths["stderr.log"])) or ""
            raise StepError(
                f"Claude Code exited ({done.strip()}) without a result",
                details={"stderr": stderr[-4000:], "hint": _hint(stderr)},
            )
        return result


async def _read(sandbox: Any, path: str) -> str | None:
    try:
        return (await sandbox.read_file(path)).decode(errors="replace")
    except FileNotFoundError:
        return None


def _hint(stderr: str) -> str:
    lowered = stderr.lower()
    if "not found" in lowered and "claude" in lowered:
        return "the claude CLI is not installed in the sandbox image; set runtime_config.install or use an image with it"
    if any(s in lowered for s in ("invalid api key", "oauth", "401", "authentication")):
        return "Claude Code could not sign in: check the gateway's API key, or link your Claude subscription again in Settings"
    return ""
