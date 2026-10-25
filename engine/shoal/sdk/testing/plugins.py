"""Contract checks for every plugin type (integrations have their own in :mod:`.contract`).

Each ``check_*`` exercises the behaviour the engine relies on and raises
``AssertionError`` with a readable message on the first violation. Run them from
your plugin's tests::

    async def test_contract(tmp_path):
        await check_sandbox_backend(MyBackend(), workdir=str(tmp_path / "ws"))
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from importlib.metadata import entry_points
from typing import Any

from shoal.core import clock
from shoal.core.models import DecisionQuestion
from shoal.sdk.plugins import (
    PLUGIN_GROUPS,
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
    Delivery,
    DispatchQueue,
    EvalSink,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    SandboxBackend,
    SandboxSpec,
    SkillPage,
    SkillSource,
    SkillSourceContext,
    ToolDef,
    Wakeup,
)
from shoal.skills.base import SkillBundle, SkillSourceError

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


async def check_skill_source(source: SkillSource, ctx: SkillSourceContext) -> None:
    """``list``, ``search`` and ``fetch`` agree with each other, and unknown skills raise ``SkillSourceError``.

    ``ctx`` points at a source with at least one skill (for example a test repository or registry).
    """
    assert isinstance(source, SkillSource), "skill sources subclass SkillSource"
    catalog = type(source).catalog()
    assert catalog["config_schema"].get("type") == "object", "config_model is a pydantic model (a JSON object schema)"
    page = await source.list(ctx, None, 50)
    assert isinstance(page, SkillPage), "list returns a SkillPage"
    assert page.items, "list returned no skills; point the contract at a source that has some"
    ids = [s.id for s in page.items]
    assert len(ids) == len(set(ids)), f"skill ids must be unique within a page: {ids}"
    first = page.items[0]
    assert first.id and first.name, "every summary has an id and a name"
    found = await source.search(ctx, first.name, None, 50)
    assert isinstance(found, SkillPage), "search returns a SkillPage"
    assert first.id in [s.id for s in found.items], f"searching for '{first.name}' does not find {first.id}"
    if page.next_cursor:
        more = await source.list(ctx, page.next_cursor, 50)
        assert isinstance(more, SkillPage), "the next page is a SkillPage"
    bundle = await source.fetch(ctx, first.id)
    assert isinstance(bundle, SkillBundle), "fetch returns a SkillBundle"
    assert bundle.id == first.id, f"fetch('{first.id}') returned skill '{bundle.id}'"
    assert bundle.version, "a bundle names its version"
    assert isinstance(bundle.instructions, str) and not bundle.instructions.startswith("---"), (
        "instructions are the SKILL.md body, without frontmatter"
    )
    assert all(isinstance(k, str) and isinstance(v, str) and not k.startswith("/") for k, v in bundle.files.items()), (
        "files map relative paths to text"
    )
    assert "SKILL.md" not in bundle.files, "SKILL.md is the instructions, not one of the files"
    try:
        await source.fetch(ctx, "shoal-contract/does-not-exist")
    except SkillSourceError:
        pass
    else:
        raise AssertionError("fetching an unknown skill should raise SkillSourceError (status 404)")



async def check_dispatch_queue(queue: DispatchQueue, *, lane: str = "standard", wait: float = 2.0) -> None:
    """Publish, receive, hand back and ack wake-ups on a started queue (use a fresh, empty one).

    Leaves one delayed message behind (the wake-up two minutes out).
    """
    assert queue.external, "only external queues carry messages (the database queue is the runs table)"
    assert lane in sum((list(g) for g in queue.groups([lane])), []), "groups() must cover every lane it is given"
    tag = uuid.uuid4().hex[:12]
    now = clock.now()
    due = Wakeup(f"run_contract_{tag}", "tenant_contract", lane, now)
    later = Wakeup(f"run_contract_later_{tag}", "tenant_contract", lane, now + timedelta(minutes=2))
    await queue.publish([due, later])

    async def receive_due() -> Delivery:
        seen: list[Delivery] = []
        for _ in range(5):
            batch = await queue.receive([lane], queue.batch_size, wait)
            assert all(d.wakeup.run_id != later.run_id for d in batch), "a wake-up for later arrived straight away"
            seen += batch
            found = [d for d in seen if d.wakeup.run_id == due.run_id]
            if found:
                others = [d for d in seen if d.wakeup.run_id != due.run_id]
                if others:
                    await queue.retry(others, 0)
                return found[0]
        raise AssertionError("a wake-up that is due was not received")

    first = await receive_due()
    assert first.wakeup == due, f"wake-ups must round-trip unchanged: {first.wakeup} != {due}"
    assert not [d for d in await queue.receive([lane], queue.batch_size, 0) if d.wakeup.run_id == due.run_id], \
        "a received message must stay hidden until it is acked or handed back"
    await queue.retry([first], 0)
    again = await receive_due()
    assert again.attempts >= first.attempts, "attempts never go down"
    await queue.ack([again])
    left = await queue.receive([lane], queue.batch_size, min(wait, 1.0))
    assert all(d.wakeup.run_id != due.run_id for d in left), "an acked message came back"
    if left:
        await queue.retry(left, 0)
