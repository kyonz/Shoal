"""``script`` steps: languages, typed inputs and outputs."""

from __future__ import annotations

import shutil

import pytest
from pydantic import ValidationError

from shoal.core.models import ScriptStep, Workflow, parse_resource


def wf(name: str, steps: list[dict], **spec) -> Workflow:
    return parse_resource(
        {"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": name}, "spec": {"steps": steps, **spec}}
    )


async def run_wf(engine, tenant, workflow: Workflow, inputs: dict | None = None):
    await engine.store.apply_resource(tenant.id, workflow)
    run = await engine.executor.start_run(tenant.id, workflow.metadata.name, inputs or {})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    return run, {s.step_id: s for s in await engine.store.get_steps(run.id)}


PYTHON = """
import shoal_step
items = shoal_step.inputs["items"]
shoal_step.set_output("count", len(items))
shoal_step.set_output("total", sum(i["n"] for i in items))
print("counted", len(items))
"""


async def test_python_code_with_typed_inputs_and_outputs(engine, tenant):
    run, steps = await run_wf(
        engine,
        tenant,
        wf(
            "py",
            [
                {"id": "data", "type": "set", "values": {"items": [{"n": 2}, {"n": 5}]}},
                {
                    "id": "count",
                    "type": "script",
                    "language": "python",
                    "code": PYTHON,
                    "inputs": {"items": "{{ steps.data.output['items'] }}"},
                    "outputs": {"count": {"type": "integer"}, "total": {"type": "number"}},
                },
            ],
            outputs={"total": "{{ steps.count.output.total }}"},
        ),
    )
    assert run.status == "succeeded", run.error
    out = steps["count"].output
    assert out["count"] == 2 and out["total"] == 7
    assert out["outputs"] == {"count": 2, "total": 7}
    assert out["exit_code"] == 0 and "counted 2" in out["stdout"]
    assert run.outputs == {"total": 7}


async def test_code_is_not_rendered(engine, tenant):
    code = 'import json, os\nd = {"k": {"v": 1}}\njson.dump({"s": "{{ x }}", "d": d}, open(os.environ["SHOAL_OUTPUTS"], "w"))\n'
    run, steps = await run_wf(engine, tenant, wf("literal", [{"id": "s", "type": "script", "language": "python", "code": code}]))
    assert run.status == "succeeded", run.error
    assert steps["s"].output["outputs"] == {"s": "{{ x }}", "d": {"k": {"v": 1}}}


async def test_shell_code_reads_scalar_inputs_from_env(engine, tenant):
    code = 'printf \'{"greeting": "hi %s", "n": %s}\' "$SHOAL_INPUT_NAME" "$SHOAL_INPUT_N" > "$SHOAL_OUTPUTS"\n'
    run, steps = await run_wf(
        engine,
        tenant,
        wf(
            "sh",
            [{"id": "s", "type": "script", "code": code, "inputs": {"name": "{{ inputs.who }}", "n": 3},
              "outputs": {"greeting": {}, "n": {"type": "integer"}}}],
            inputs={"who": {"type": "string"}},
        ),
        {"who": "Trevor"},
    )
    assert run.status == "succeeded", run.error
    assert steps["s"].output["greeting"] == "hi Trevor" and steps["s"].output["n"] == 3


async def test_missing_or_mistyped_outputs_fail_the_step(engine, tenant):
    code = 'import shoal_step\nshoal_step.set_output("count", "many")\n'
    run, steps = await run_wf(
        engine,
        tenant,
        wf("bad", [{"id": "s", "type": "script", "language": "python", "code": code,
                    "outputs": {"count": {"type": "integer"}, "label": {"type": "string"}}}]),
    )
    assert run.status == "failed"
    assert "outputs are invalid" in steps["s"].error["message"]
    assert "'label' is required" in steps["s"].error["message"]


async def test_failed_program_fails_unless_fail_on_error_is_off(engine, tenant):
    code = 'import sys\nprint("nope", file=sys.stderr)\nsys.exit(4)\n'
    run, steps = await run_wf(engine, tenant, wf("fails", [{"id": "s", "type": "script", "language": "python", "code": code}]))
    assert run.status == "failed" and "exited with 4" in steps["s"].error["message"]
    run, steps = await run_wf(
        engine, tenant,
        wf("tolerates", [{"id": "s", "type": "script", "language": "python", "code": code, "fail_on_error": False,
                          "outputs": {"x": {"type": "integer"}}}]),
    )
    assert run.status == "succeeded" and steps["s"].output["exit_code"] == 4 and "nope" in steps["s"].output["stderr"]


async def test_run_scripts_see_inputs_too(engine, tenant):
    run, steps = await run_wf(
        engine, tenant,
        wf("legacy", [{"id": "s", "type": "script", "run": "cat \"$SHOAL_INPUTS\"", "inputs": {"a": [1, 2]}}]),
    )
    assert run.status == "succeeded", run.error
    assert steps["s"].output["stdout"].strip() == '{"a": [1, 2]}'


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
async def test_node_code(engine, tenant):
    code = 'const { inputs, setOutput } = require("shoal_step");\nsetOutput("upper", inputs.word.toUpperCase());\n'
    run, steps = await run_wf(
        engine, tenant,
        wf("js", [{"id": "s", "type": "script", "language": "node", "code": code, "inputs": {"word": "shoal"},
                   "outputs": {"upper": {"type": "string"}}}]),
    )
    assert run.status == "succeeded", run.error
    assert steps["s"].output["upper"] == "SHOAL"


def test_validation():
    with pytest.raises(ValidationError, match="exactly one of"):
        ScriptStep(id="s")
    with pytest.raises(ValidationError, match="exactly one of"):
        ScriptStep(id="s", run="true", code="true")
    with pytest.raises(ValidationError, match="'language' applies"):
        ScriptStep(id="s", run="true", language="python")
    with pytest.raises(ValidationError, match="reserved"):
        ScriptStep(id="s", code="x", outputs={"stdout": {}})


async def test_script_runs_in_a_container_style_backend(engine, tenant, tmp_path):
    """Paths handed to the program are sandbox paths (/workspace/...), not host paths."""
    import asyncio

    from shoal.sandbox.base import Sandbox, SandboxBackend
    from shoal.sandbox.wrap import parse_wrapped, wrapped_command, write_chunks

    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen: list[list[str]] = []

    async def raw(argv: list[str]) -> str:  # a container's exec API, played by a local shell
        seen.append(argv)
        argv = [a.replace("/workspace", str(workspace)) for a in argv]
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out.decode()

    class Box(Sandbox):
        id, workdir = "box", "/workspace"

        async def exec(self, command, *, cwd=None, env=None, timeout=None):
            return parse_wrapped(await raw(wrapped_command(command, cwd=cwd or self.workdir, env=env, timeout=timeout)), 0.0)

        async def write_file(self, path, data):
            for argv in write_chunks(self.resolve(path), data.encode() if isinstance(data, str) else data):
                await raw(argv)

        async def read_file(self, path):
            return (workspace / self.resolve(path).removeprefix("/workspace/")).read_bytes()

        async def close(self):
            return None

    class BoxBackend(SandboxBackend):
        name = "box"

        async def create(self, spec):
            return Box()

    engine.registry.register("sandboxes", "box", BoxBackend())
    await engine.store.apply_resource(
        tenant.id, parse_resource({"apiVersion": "shoal.dev/v1", "kind": "SandboxProfile", "metadata": {"name": "box"},
                                   "spec": {"backend": "box", "scope": "step"}})
    )
    code = 'import os, shoal_step\nshoal_step.set_output("where", os.environ["SHOAL_INPUTS"])\n'
    run, steps = await run_wf(
        engine, tenant,
        wf("boxed", [{"id": "s", "type": "script", "sandbox": "box", "language": "python", "code": code,
                      "outputs": {"where": {"type": "string"}}}]),
    )
    assert run.status == "succeeded", run.error
    assert steps["s"].output["where"] == f"{workspace}/.shoal/steps/s-1/inputs.json"  # as the fake maps it
    sent = " ".join(" ".join(argv) for argv in seen)
    assert "/workspace/.shoal/steps/s-1/script.py" in sent and str(tmp_path) not in sent
