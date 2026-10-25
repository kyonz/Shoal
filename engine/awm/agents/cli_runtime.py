"""Run an external coding-agent CLI (Claude Code, Codex, Aider, ...) inside the sandbox.

``runtime_config``:

* ``command`` - shell command; ``$AWM_PROMPT_FILE`` and ``$AWM_OUTPUT_FILE``
  are exported. Defaults to Claude Code in non-interactive mode.
* ``setup`` - optional command run once before (e.g. install the CLI).
* ``timeout`` - seconds (defaults to the agent's limits.timeout).

The agent is told to write its outputs as JSON to ``$AWM_OUTPUT_FILE``.
"""

from __future__ import annotations

import json

from awm.agents.runtime import AgentInvocation, AgentResult, AgentRuntime, install_skills, outputs_description
from awm.core.errors import StepError
from awm.core.models import ParamValidationError, validate_params

DEFAULT_COMMAND = (
    'claude -p "$(cat "$AWM_PROMPT_FILE")" --output-format json --permission-mode bypassPermissions '
    '--max-turns "${AWM_MAX_TURNS:-60}"'
)

PROMPT_TEMPLATE = """{instructions}

{prompt}

{skills}
When you are done, write a JSON object to the file {output_file} with these fields:
{outputs}
Write valid JSON only. Do not ask questions; no human is watching this session.
"""


class CliRuntime(AgentRuntime):
    name = "cli"

    async def run(self, inv: AgentInvocation) -> AgentResult:
        cfg = inv.agent.runtime_config
        sandbox = inv.sandbox
        prompt_file = sandbox.resolve(".awm/prompt.md")
        output_file = sandbox.resolve(".awm/output.json")
        skills = await install_skills(inv)
        skills_text = (
            "Skills available (read the SKILL.md before use):\n" + "\n".join(f"- {n}: {d} ({p})" for n, d, p in skills)
            if skills
            else ""
        )
        await sandbox.write_file(
            prompt_file,
            PROMPT_TEMPLATE.format(
                instructions=inv.agent.instructions,
                prompt=inv.prompt,
                skills=skills_text,
                output_file=output_file,
                outputs=outputs_description(inv.outputs),
            ),
        )
        env = {
            **inv.env,
            "AWM_PROMPT_FILE": prompt_file,
            "AWM_OUTPUT_FILE": output_file,
            "AWM_MAX_TURNS": str(inv.agent.limits.max_turns),
        }
        if inv.provider_config.get("api_key") and inv.agent.provider == "anthropic":
            env.setdefault("ANTHROPIC_API_KEY", inv.provider_config["api_key"])
        timeout = float(cfg.get("timeout") or inv.agent.limits.timeout.total_seconds())

        if cfg.get("setup"):
            setup = await sandbox.exec(cfg["setup"], env=env, timeout=timeout)
            if not setup.ok:
                raise StepError("agent setup command failed", details=setup.to_dict(4000))

        await inv.ctx.emit("agent.cli.started", {"agent": inv.agent_name, "command": cfg.get("command", DEFAULT_COMMAND)})
        result = await sandbox.exec(cfg.get("command", DEFAULT_COMMAND), env=env, timeout=timeout)
        await inv.ctx.emit("agent.cli.finished", {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]})
        if result.timed_out:
            raise StepError("agent CLI timed out", retryable=True)

        try:
            raw = json.loads((await sandbox.read_file(output_file)).decode())
        except (FileNotFoundError, ValueError) as exc:
            raise StepError(
                f"agent CLI did not write valid JSON to {output_file}",
                details={"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            ) from exc
        summary = str(raw.pop("summary", ""))
        try:
            outputs = validate_params(inv.outputs, raw)
        except ParamValidationError as exc:
            raise StepError(f"agent outputs invalid: {exc}", details={"outputs": raw}) from exc
        usage = _usage_from_stdout(result.stdout)
        usage["sandbox_seconds"] = result.duration_seconds
        return AgentResult(outputs=outputs, summary=summary, usage=usage)


def _usage_from_stdout(stdout: str) -> dict[str, float]:
    """Claude Code's JSON output includes total cost; other CLIs are ignored."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    usage: dict[str, float] = {}
    if isinstance(data, dict):
        if isinstance(data.get("total_cost_usd"), int | float):
            usage["cost_usd"] = float(data["total_cost_usd"])
        u = data.get("usage") or {}
        for key in ("input_tokens", "output_tokens"):
            if isinstance(u.get(key), int):
                usage[key] = u[key]
    return usage
