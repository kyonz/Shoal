"""Contract checks for every plugin type (integrations have their own in :mod:`.contract`).

Each ``check_*`` exercises the behaviour the engine relies on and raises
``AssertionError`` with a readable message on the first violation. Run them from
your plugin's tests::

    async def test_contract(tmp_path):
        await check_sandbox_backend(MyBackend(), workdir=str(tmp_path / "ws"))
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from shoal.core.models import DecisionQuestion
from shoal.sdk.plugins import (
    PLUGIN_GROUPS,
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
    EvalSink,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    SandboxBackend,
    SandboxSpec,
    ToolDef,
)

STOP_REASONS = {"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"}


def check_entry_point(group: str, name: str) -> Any:
    """The installed package exposes ``name`` in ``shoal.<group>`` and it loads as the right kind of plugin."""
    assert group in PLUGIN_GROUPS or group == "steps", f"unknown plugin group '{group}'"
    found = [ep for ep in entry_points(group=f"shoal.{group}") if ep.name == name]
    assert found, f"no entry point '{name}' in group 'shoal.{group}' (is the package installed?)"
    target = found[0].load()
    if group in PLUGIN_GROUPS and isinstance(PLUGIN_GROUPS[group].base, type) and isinstance(target, type):
        base = PLUGIN_GROUPS[group].base
        if not getattr(base, "_is_protocol", False):
            assert issubclass(target, base), f"{target.__name__} should subclass {base.__name__}"
    return target


async def check_sandbox_backend(backend: SandboxBackend, *, workdir: str = "/workspace", spec: SandboxSpec | None = None) -> None:
    """Create a sandbox, run commands, move files, and clean up."""
    spec = spec or SandboxSpec(workdir=workdir, env={"SHOAL_CONTRACT": "yes"}, labels={"shoal/tenant": "contract"})
    sandbox = await backend.create(spec)
    try:
        assert sandbox.id, "sandboxes need a stable id (used to re-attach after a worker restart)"
        result = await sandbox.exec("echo out; echo err >&2; exit 3")
        assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n"), f"exec: {result}"
        assert (await sandbox.exec("echo $SHOAL_CONTRACT")).stdout == "yes\n", "spec.env must reach commands"
        assert (await sandbox.exec("echo $X", env={"X": "1"})).stdout == "1\n", "per-exec env must reach commands"
        workdir = getattr(sandbox, "workdir", spec.workdir)  # local backends map the workdir to a host path
        assert (await sandbox.exec("pwd")).stdout.strip() == workdir, "commands run in the workdir by default"
        await sandbox.write_file("dir/x.txt", "hello")
        assert await sandbox.read_file("dir/x.txt") == b"hello", "read_file returns what write_file wrote"
        await sandbox.write_file("bin.dat", bytes(range(256)))
        assert await sandbox.read_file("bin.dat") == bytes(range(256)), "files are binary-safe"
        assert (await sandbox.exec("cat x.txt", cwd="dir")).stdout == "hello", "cwd is relative to the workdir"
        try:
            await sandbox.read_file("missing.txt")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("read_file of a missing file must raise FileNotFoundError")
        assert any(p.endswith("dir/x.txt") for p in await sandbox.list_files(".")), "list_files lists nested files"
        timed = await sandbox.exec("sleep 5", timeout=0.5)
        assert timed.timed_out and timed.exit_code != 0, "a command over its timeout is stopped and reported"
        again = await backend.attach(sandbox.id, spec)
        if again is not None:
            assert await again.read_file("dir/x.txt") == b"hello", "attach returns the same sandbox"
    finally:
        await sandbox.close()


async def check_model_provider(provider: ModelProvider, *, model: str, config: dict[str, Any] | None = None,
                               check_tools: bool = True) -> None:
    """A plain request and a tool request come back in the engine's message shape."""
    plain = await provider.complete(ModelRequest(model=model, system="Answer briefly.",
                                                 messages=[{"role": "user", "content": "Say OK."}], tools=[],
                                                 max_tokens=256, config=dict(config or {})))
    _check_response(plain)
    assert plain.text, "a plain request should return some text"
    if not check_tools:
        return
    tool = ToolDef("record", "Record a word.", {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]})
    called = await provider.complete(ModelRequest(model=model, system="Always call the record tool with the word 'ok'.",
                                                  messages=[{"role": "user", "content": "Go."}], tools=[tool],
                                                  max_tokens=256, config=dict(config or {})))
    _check_response(called)
    uses = called.tool_uses
    assert uses and uses[0].get("name") == "record" and uses[0].get("id"), f"expected a tool_use block for 'record': {called.content}"
    assert isinstance(uses[0].get("input"), dict), "tool_use input must be an object"


def _check_response(response: ModelResponse) -> None:
    assert isinstance(response, ModelResponse), "complete() must return a ModelResponse"
    assert isinstance(response.content, list) and all(isinstance(b, dict) and "type" in b for b in response.content), \
        "content is a list of typed blocks"
    assert response.stop_reason in STOP_REASONS, f"unknown stop_reason {response.stop_reason!r}"
    assert all(isinstance(v, int | float) for v in response.usage.values()), "usage values are numbers (tokens, cost_usd)"


async def check_secret_provider(provider: Any, tenant_id: str, *, known: dict[str, str]) -> None:
    """Known names resolve, unknown ones are None; writable providers round-trip."""
    for name, value in known.items():
        assert await provider.get(tenant_id, name) == value, f"get({name!r}) should return its value"
    assert await provider.get(tenant_id, "shoal-contract-missing") is None, "unknown secrets must be None, not an error"
    if getattr(provider, "writable", False):
        await provider.put(tenant_id, "shoal-contract", "v1")
        await provider.put(tenant_id, "shoal-contract", "v2")
        assert await provider.get(tenant_id, "shoal-contract") == "v2", "put replaces the value"
        await provider.delete(tenant_id, "shoal-contract")
        assert await provider.get(tenant_id, "shoal-contract") is None, "delete removes the value"


async def check_decision_provider(provider: DecisionProvider, *, tenant_id: str = "contract",
                                  config: dict[str, Any] | None = None) -> None:
    """Every question gets an answer of the right type, with probabilities that sum to about 1."""
    questions = {
        "kind": DecisionQuestion(type="choice", instructions="What kind of report is this?",
                                 options={"bug": "Something is broken", "feature": "A request for something new"}),
        "severity": DecisionQuestion(type="score", instructions="How severe?", levels=["low", "medium", "high"]),
        "actionable": DecisionQuestion(type="boolean", instructions="Can an engineer act on it now?"),
    }
    result = await provider.decide(DecisionRequest(tenant_id, {"title": "Login button crashes the app"}, questions, dict(config or {})))
    assert isinstance(result, DecisionResult), "decide() must return a DecisionResult"
    assert set(result.answers) == set(questions), f"answers for every question, got {sorted(result.answers)}"
    assert result.answers["kind"].value in questions["kind"].options, "choice answers are one of the option keys"
    assert result.answers["severity"].value in questions["severity"].levels, "score answers are one of the levels"
    assert isinstance(result.answers["actionable"].value, bool), "boolean answers are booleans"
    for key, answer in result.answers.items():
        assert 0.0 <= answer.confidence <= 1.0, f"{key}: confidence is in [0, 1]"
        total = sum(answer.probabilities.values())
        assert abs(total - 1.0) < 0.05, f"{key}: probabilities sum to 1 (got {total:.3f})"


def check_eval_sink(sink: EvalSink) -> None:
    """The sink implements the three operations (behaviour is checked against the platform's API in your own tests)."""
    assert isinstance(sink, EvalSink), "eval sinks subclass EvalSink"
    for method in ("export_eval", "export_trace", "import_dataset"):
        assert callable(getattr(sink, method, None)), f"missing {method}"
    schema = getattr(sink, "config_schema", None)
    if schema:
        assert isinstance(schema, dict) and schema.get("type") == "object", "config_schema is a JSON object schema"
