"""Built-in step handlers: set, action, script, wait, approval, workflow."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from shoal.core.errors import StepError
from shoal.core.models import (
    SCRIPT_RESERVED_OUTPUTS,
    ActionStep,
    ApprovalStep,
    ParamValidationError,
    RunStatus,
    ScriptStep,
    SetStep,
    WaitStep,
    WorkflowCallStep,
    validate_params,
)
from shoal.integrations.base import IntegrationError
from shoal.runtime.context import Completed, StepContext, StepResult, Suspended
from shoal.runtime.signals import event_key, signal_key
from shoal.sandbox.base import shell_quote


class SetHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, SetStep)
        return Completed(ctx.render(step.values))


class ActionHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, ActionStep)
        params = ctx.render(step.with_)
        integration = step.action.split(".", 1)[0]
        await ctx.emit("action.called", {"action": step.action, "params": params})
        output = await ctx.engine.integrations.call(
            ctx.tenant_id,
            step.action,
            params,
            connection=ctx.connection_for(integration, step.connection),
            initiator=ctx.run.initiator,
            run_id=ctx.run.id,
        )
        return Completed(output)


_SCRIPT_FILES = {"shell": "script.sh", "bash": "script.sh", "python": "script.py", "node": "script.js"}
MAX_SCRIPT_OUTPUTS_BYTES = 1_000_000

# Helpers written next to every program so scripts need no Shoal package installed.
PYTHON_HELPER = """\
\"\"\"Shoal script step helpers: ``inputs`` and ``set_output(name, value)``.\"\"\"
import json as _json
import os as _os

with open(_os.environ["SHOAL_INPUTS"]) as _f:
    inputs = _json.load(_f)
outputs = {}


def set_outputs(values):
    outputs.update(values)
    with open(_os.environ["SHOAL_OUTPUTS"], "w") as f:
        _json.dump(outputs, f, default=str)


def set_output(name, value):
    set_outputs({name: value})
"""

NODE_HELPER = """\
// Shoal script step helpers: inputs and setOutput(name, value).
const fs = require("fs");
const inputs = JSON.parse(fs.readFileSync(process.env.SHOAL_INPUTS, "utf8"));
const outputs = {};
function setOutputs(values) {
  Object.assign(outputs, values);
  fs.writeFileSync(process.env.SHOAL_OUTPUTS, JSON.stringify(outputs));
}
function setOutput(name, value) { setOutputs({ [name]: value }); }
module.exports = { inputs, outputs, setOutput, setOutputs };
"""


def _script_command(language: str, step_dir: str, script: str) -> str:
    d, s = shell_quote(step_dir), shell_quote(script)
    if language == "python":
        return f"PYTHONPATH={d}${{PYTHONPATH:+:$PYTHONPATH}} exec python3 {s}"
    if language == "node":
        return f"NODE_PATH={d}${{NODE_PATH:+:$NODE_PATH}} exec node {s}"
    return f"exec {'bash' if language == 'bash' else 'sh'} {s}"


def _input_var(name: str) -> str:
    return "SHOAL_INPUT_" + re.sub(r"[^A-Za-z0-9_]", "_", name).upper()


def _input_env(inputs: dict[str, Any]) -> dict[str, str]:
    """Scalar inputs as ``SHOAL_INPUT_<NAME>`` for shell scripts."""
    env = {}
    for name, value in inputs.items():
        if isinstance(value, bool):
            env[_input_var(name)] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            env[_input_var(name)] = str(value)
    return env


class ScriptHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, ScriptStep)
        inputs = ctx.render(step.inputs)
        env = {k: str(v) for k, v in ctx.render(step.env).items()}
        if step.cloud_identities:
            env = {**await ctx.engine.cloud.environment(ctx, step.cloud_identities), **env}
        profile = step.sandbox or ctx.workflow.defaults.sandbox
        timeout = step.timeout.total_seconds() if step.timeout else None
        async with ctx.engine.sandboxes.acquire(ctx.run, profile) as sandbox:
            rel = f".shoal/steps/{step.id}-{ctx.attempt}"
            step_dir = sandbox.resolve(rel)
            inputs_path, outputs_path = f"{step_dir}/inputs.json", f"{step_dir}/outputs.json"
            await sandbox.write_file(inputs_path, json.dumps(inputs, default=str))
            await sandbox.write_file(outputs_path, "{}")
            if step.code is not None:
                source = step.code
                script_path = f"{step_dir}/{_SCRIPT_FILES[step.language]}"
                await sandbox.write_file(script_path, step.code)
                if step.language == "python":
                    await sandbox.write_file(f"{step_dir}/shoal_step.py", PYTHON_HELPER)
                elif step.language == "node":
                    await sandbox.write_file(f"{step_dir}/shoal_step.js", NODE_HELPER)
                command = _script_command(step.language, step_dir, script_path)
            else:
                source = command = ctx.render(step.run)
            env = {**_input_env(inputs), **env, "SHOAL_INPUTS": inputs_path, "SHOAL_OUTPUTS": outputs_path,
                   "SHOAL_STEP_DIR": step_dir}
            await ctx.emit("script.started", {"sandbox_id": sandbox.id, "language": step.language if step.code is not None else "shell",
                                              "script": source[:2000]})
            result = await sandbox.exec(command, cwd=step.workdir, env=env, timeout=timeout)
            declared = await self._read_outputs(sandbox, outputs_path)
        output = ctx.redactor(result.to_dict())
        await ctx.emit("script.finished", {"exit_code": result.exit_code, "duration": result.duration_seconds})
        usage = {"sandbox_seconds": result.duration_seconds}
        if step.fail_on_error and not result.ok:
            raise StepError(
                f"script exited with {result.exit_code}" + (" (timed out)" if result.timed_out else ""),
                details={"stdout": output["stdout"][-4000:], "stderr": output["stderr"][-4000:]},
            )
        if result.ok and step.outputs:
            try:
                declared = validate_params(step.outputs, declared)
            except ParamValidationError as exc:
                raise StepError(
                    f"script outputs are invalid: {exc}",
                    details={"errors": exc.errors, "stderr": output["stderr"][-4000:]},
                ) from exc
        declared = ctx.redactor(declared)
        top = {k: v for k, v in declared.items() if k not in SCRIPT_RESERVED_OUTPUTS}
        return Completed({**top, **output, "outputs": declared}, usage=usage)

    @staticmethod
    async def _read_outputs(sandbox: Any, path: str) -> dict[str, Any]:
        try:
            raw = await sandbox.read_file(path)
        except Exception:  # the program removed it; backends raise their own errors
            return {}
        if len(raw) > MAX_SCRIPT_OUTPUTS_BYTES:
            raise StepError(f"script outputs exceed {MAX_SCRIPT_OUTPUTS_BYTES} bytes")
        try:
            value = json.loads(raw or b"{}")
        except ValueError as exc:
            raise StepError(f"$SHOAL_OUTPUTS is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise StepError("$SHOAL_OUTPUTS must hold a JSON object")
        return value


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class WaitHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, WaitStep)
        now = ctx.now()

        if step.signal is not None:
            return await self._signal(ctx, step, now)
        if step.condition is None:
            if step.duration is not None:
                until = ctx.deadline("until", step.duration)
            else:
                if "until" not in ctx.state:
                    ctx.state["until"] = _parse_time(ctx.render(step.until)).isoformat()
                until = datetime.fromisoformat(ctx.state["until"])
            if now >= until:
                return Completed({"waited_until": until.isoformat(), "timed_out": False})
            return Suspended(wake_at=until, reason=f"waiting until {until.isoformat()}")

        cond = step.condition
        assert cond is not None
        deadline = ctx.deadline("deadline", step.max_wait) if step.max_wait else None
        ctx.state["polls"] = ctx.state.get("polls", 0) + 1
        result: dict[str, Any] | None = None
        try:
            integration = cond.action.split(".", 1)[0]
            result = await ctx.engine.integrations.call(
                ctx.tenant_id,
                cond.action,
                ctx.render(cond.with_),
                connection=ctx.connection_for(integration, cond.connection),
                initiator=ctx.run.initiator,
                run_id=ctx.run.id,
            )
        except IntegrationError as exc:
            if not exc.retryable:
                raise
            await ctx.emit("wait.poll_error", {"error": str(exc)})

        if result is not None and ctx.truthy(cond.check, result=result):
            return Completed({"satisfied": True, "timed_out": False, "result": result, "polls": ctx.state["polls"]})

        if deadline is not None and now >= deadline:
            if step.on_timeout == "fail":
                raise StepError(f"condition not met within {step.max_wait}", details={"result": result})
            return Completed({"satisfied": False, "timed_out": True, "result": result, "polls": ctx.state["polls"]})

        next_poll = now + cond.poll_interval
        if deadline is not None:
            next_poll = min(next_poll, deadline)
        keys = [k for k in (ctx.render(cond.wake_on) or []) if k]
        await ctx.emit("wait.polled", {"satisfied": False, "next_poll": next_poll.isoformat()})
        return Suspended(wake_at=next_poll, wait_keys=keys, reason="condition not met")

    async def _signal(self, ctx: StepContext, step: WaitStep, now: datetime) -> StepResult:
        sig = step.signal
        assert sig is not None
        if sig.name:
            name = sig.name
            wait_key = signal_key(ctx.run.id, name)
        else:
            if "event" not in ctx.state:  # render once so the key is stable across resumes
                ctx.state["event"] = str(ctx.render(sig.event))
            name = wait_key = event_key(ctx.state["event"])
        match = (lambda payload: ctx.truthy(sig.match, signal=payload)) if sig.match else None
        row = await ctx.engine.signals.take(ctx.run.id, name, ctx.step.id, match)
        if row is not None:
            return Completed({
                "received": True, "timed_out": False, "signal": row.id, "name": sig.name or ctx.state["event"],
                "payload": row.data.get("payload"), "source": row.source, "sender": row.sender,
                "received_at": row.created_at.isoformat(),
            })
        deadline = ctx.deadline("deadline", step.max_wait) if step.max_wait else None
        if deadline is not None and now >= deadline:
            if step.on_timeout == "fail":
                raise StepError(f"no '{sig.name or ctx.state['event']}' signal within {step.max_wait}")
            return Completed({"received": False, "timed_out": True, "payload": None})
        return Suspended(wake_at=deadline, wait_keys=[wait_key], reason=f"waiting for signal {sig.name or ctx.state['event']}")


class ApprovalHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, ApprovalStep)
        decision = ctx.state.get("decision")
        if decision:
            return self._outcome(step, {
                "approved": decision["approved"],
                "by": decision.get("by"),
                "comment": decision.get("comment"),
                "timed_out": False,
            })
        deadline = ctx.deadline("deadline", step.max_wait) if step.max_wait else None
        if deadline is not None and ctx.now() >= deadline:
            return self._outcome(
                step, {"approved": step.on_timeout == "approve", "by": None, "comment": None, "timed_out": True}
            )
        if not ctx.state.get("requested"):
            ctx.state["requested"] = True
            ctx.state["message"] = ctx.render(step.message)
            await ctx.emit(
                "approval.requested",
                {"message": ctx.state["message"], "approvers": step.approvers, "deadline": deadline and deadline.isoformat()},
            )
        return Suspended(
            wake_at=deadline, wait_keys=[f"approval:{ctx.run.id}:{step.id}"], reason="awaiting approval"
        )

    @staticmethod
    def _outcome(step: ApprovalStep, output: dict[str, Any]) -> StepResult:
        """A rejection stops the run unless the step says ``on_reject: continue``."""
        if output["approved"] or step.on_reject == "continue":
            return Completed(output)
        if output["timed_out"]:
            message = f"not approved within {step.max_wait}"
        else:
            message = f"rejected by {output['by'] or 'unknown'}" + (f": {output['comment']}" if output["comment"] else "")
        raise StepError(message, details=output)


class WorkflowHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, WorkflowCallStep)
        executor = ctx.engine.executor
        store = ctx.engine.store
        children: list[str | None] = ctx.state.get("children") or []

        if "items" not in ctx.state:
            if step.for_each:
                items = ctx.render(step.for_each)
                if not isinstance(items, list):
                    raise StepError(f"for_each must produce a list, got {type(items).__name__}")
            else:
                items = [None]
            ctx.state["items"] = items
            children = [None] * len(items)
        items = ctx.state["items"]

        statuses: dict[str, Any] = {}
        for run_id in filter(None, children):
            statuses[run_id] = await store.get_run(ctx.tenant_id, run_id)
        if ctx.state.setdefault("attempt", ctx.attempt) != ctx.attempt:
            # A retry (retry: max_attempts > 1) starts again only the children that did not succeed.
            ctx.state["attempt"] = ctx.attempt
            children = [
                rid if rid and statuses[rid].status == RunStatus.succeeded else None for rid in children
            ]
            statuses = {rid: statuses[rid] for rid in filter(None, children)}
        active = sum(1 for r in statuses.values() if not RunStatus(r.status).terminal)

        for index, item in enumerate(items):
            if children[index] is not None or active >= step.concurrency:
                continue
            inputs = ctx.render(step.with_, item=item, index=index)
            child = await executor.start_run(
                ctx.tenant_id,
                step.workflow,
                inputs,
                version=step.version,
                started_by=f"run:{ctx.run.id}",
                context={**ctx.run.context, "parent": {"run_id": ctx.run.id, "step_id": step.id, "index": index}},
                parent_run_id=ctx.run.id,
                parent_step_id=step.id,
                campaign_id=ctx.run.campaign_id,
            )
            children[index] = child.id
            statuses[child.id] = child
            active += 1
            await ctx.emit("workflow.child_started", {"run_id": child.id, "workflow": step.workflow, "index": index})
        ctx.state["children"] = children
        await ctx.checkpoint()

        pending = [rid for rid in children if rid is None or not RunStatus(statuses[rid].status).terminal]
        if pending:
            return Suspended(
                wait_keys=[f"run:{rid}" for rid in children if rid and rid in pending],
                reason=f"waiting for {len(pending)} child run(s)",
                wake_at=ctx.now() + timedelta(minutes=5),  # safety net in case a wake is missed
            )

        results = []
        failed = 0
        for index, run_id in enumerate(children):
            child = statuses[run_id]
            ok = child.status == RunStatus.succeeded
            failed += 0 if ok else 1
            results.append({"index": index, "run_id": run_id, "status": child.status, "outputs": child.outputs or {}, "error": child.error})
        if step.for_each is None:
            child = results[0]
            if child["status"] != RunStatus.succeeded:
                raise StepError(
                    f"child workflow '{step.workflow}' {child['status']}",
                    retryable=child["status"] == RunStatus.failed,
                    details={"run_id": child["run_id"], "error": child["error"]},
                )
            return Completed({"run_id": child["run_id"], **child["outputs"]})
        output = {"results": results, "succeeded": len(results) - failed, "failed": failed}
        if failed:
            # Retryable so `retry:` on the step re-runs the failed children (cancelled ones mean someone stopped them).
            retryable = any(r["status"] == RunStatus.failed for r in results)
            raise StepError(f"{failed} of {len(results)} child runs did not succeed", retryable=retryable, details=output)
        return Completed(output)
