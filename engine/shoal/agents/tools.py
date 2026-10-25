"""Tools an agent can call: sandbox built-ins, integration actions, delegation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shoal.agents.providers.base import ToolDef
from shoal.integrations.base import IntegrationError

if TYPE_CHECKING:
    from shoal.agents.runtime import AgentInvocation

MAX_TOOL_OUTPUT = 30000
BUILTIN_TOOLS = ("shell", "read_file", "write_file", "edit_file", "list_files")


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n[... {len(text) - limit} characters omitted ...]\n" + text[-half:]


@dataclass
class Tool:
    definition: ToolDef
    handler: Callable[[dict[str, Any]], Awaitable[str]]


class ToolError(Exception):
    pass


class Toolbox:
    def __init__(self, invocation: AgentInvocation):
        self.inv = invocation
        self.tools: dict[str, Tool] = {}

    @property
    def definitions(self) -> list[ToolDef]:
        return [t.definition for t in self.tools.values()]

    def add(self, name: str, description: str, schema: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[str]]) -> None:
        self.tools[name] = Tool(ToolDef(name=name, description=description, input_schema=schema), handler)

    async def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", True
        try:
            return _clip(await tool.handler(arguments)), False
        except (ToolError, IntegrationError, ValueError, KeyError, FileNotFoundError, IsADirectoryError) as exc:
            return f"Error: {exc}", True

    # ------------------------------------------------------------------ builders

    async def build(self) -> Toolbox:
        requested = list(dict.fromkeys(self.inv.tools))
        for name in requested:
            if name in BUILTIN_TOOLS:
                getattr(self, f"_add_{name}")()
            elif name.startswith("mcp:"):  # mcp:<connection> or mcp:<connection>.<tool>
                connection, _, tool = name[4:].partition(".")
                await self._add_integration_tools(f"mcp.{tool or '*'}", connection=connection, prefix=connection)
            elif "." in name:
                await self._add_integration_tools(name)
            else:
                raise ValueError(f"unknown tool '{name}'")
        return self

    def _add_shell(self) -> None:
        sandbox = self.inv.sandbox

        async def run(args: dict[str, Any]) -> str:
            timeout = min(float(args.get("timeout_seconds") or 600), 3600)
            result = await sandbox.exec(args["command"], cwd=args.get("cwd"), env=self.inv.env, timeout=timeout)
            return json.dumps(result.to_dict(limit=MAX_TOOL_OUTPUT // 2))

        self.add(
            "shell",
            "Run a shell command in the sandbox workspace (bash -lc). Use for git, builds, tests and inspection. "
            "Returns exit_code, stdout and stderr.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "Working directory (default: workspace)."},
                    "timeout_seconds": {"type": "integer", "description": "Default 600, max 3600."},
                },
                "required": ["command"],
            },
            run,
        )

    def _add_read_file(self) -> None:
        sandbox = self.inv.sandbox

        async def run(args: dict[str, Any]) -> str:
            text = (await sandbox.read_file(args["path"])).decode(errors="replace")
            lines = text.splitlines()
            offset = max(int(args.get("offset") or 1), 1)
            limit = int(args.get("limit") or 2000)
            chunk = lines[offset - 1 : offset - 1 + limit]
            numbered = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start=offset))
            more = f"\n[{len(lines) - (offset - 1 + len(chunk))} more lines]" if offset - 1 + len(chunk) < len(lines) else ""
            return numbered + more

        self.add(
            "read_file",
            "Read a text file from the sandbox with line numbers.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-based first line."},
                    "limit": {"type": "integer", "description": "Max lines (default 2000)."},
                },
                "required": ["path"],
            },
            run,
        )

    def _add_write_file(self) -> None:
        sandbox = self.inv.sandbox

        async def run(args: dict[str, Any]) -> str:
            await sandbox.write_file(args["path"], args["content"])
            return f"Wrote {len(args['content'])} characters to {args['path']}"

        self.add(
            "write_file",
            "Create or overwrite a file in the sandbox.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            run,
        )

    def _add_edit_file(self) -> None:
        sandbox = self.inv.sandbox

        async def run(args: dict[str, Any]) -> str:
            text = (await sandbox.read_file(args["path"])).decode()
            old, new = args["old_string"], args["new_string"]
            count = text.count(old)
            if count == 0:
                raise ToolError("old_string not found in file")
            if count > 1 and not args.get("replace_all"):
                raise ToolError(f"old_string occurs {count} times; add context or set replace_all")
            text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
            await sandbox.write_file(args["path"], text)
            return f"Replaced {count if args.get('replace_all') else 1} occurrence(s) in {args['path']}"

        self.add(
            "edit_file",
            "Replace an exact string in a file. old_string must be unique unless replace_all is true.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            run,
        )

    def _add_list_files(self) -> None:
        sandbox = self.inv.sandbox

        async def run(args: dict[str, Any]) -> str:
            files = await sandbox.list_files(args.get("path") or ".", int(args.get("depth") or 2))
            return "\n".join(files) or "(empty)"

        self.add(
            "list_files",
            "List files under a directory (default depth 2, .git excluded).",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}},
            },
            run,
        )

    async def _add_integration_tools(self, pattern: str, *, connection: str | None = None, prefix: str | None = None) -> None:
        integration_key, _, action_name = pattern.partition(".")
        engine, run_ctx = self.inv.ctx.engine, self.inv.ctx
        integration = engine.integrations.get(integration_key)
        actions = integration.actions
        if not actions:  # dynamic integrations (remote, MCP) list their actions per connection
            name = await engine.integrations.resolve_name(
                run_ctx.tenant_id, integration_key, connection or run_ctx.connection_for(integration_key)
            )
            conn = await engine.integrations.connection(run_ctx.tenant_id, name, initiator=run_ctx.run.initiator, run_id=run_ctx.run.id)
            actions = await integration.actions_for(conn)
        names = list(actions) if action_name in ("*", "") else [action_name]
        for name in names:
            definition = actions.get(name)
            if definition is None:
                raise ValueError(f"{integration_key} has no action '{name}'")
            full = f"{integration_key}.{name}"

            def make(full: str = full):  # bind loop variable
                async def run(args: dict[str, Any]) -> str:
                    ctx = self.inv.ctx
                    connection_name = connection or ctx.connection_for(full.split(".")[0])
                    await ctx.emit("agent.action", {"action": full, "params": args})
                    result = await ctx.engine.integrations.call(
                        ctx.tenant_id, full, args, connection=connection_name, initiator=ctx.run.initiator, run_id=ctx.run.id
                    )
                    return json.dumps(result, default=str)

                return run

            tool_prefix = re.sub(r"[^A-Za-z0-9_]", "_", prefix) if prefix else integration_key
            self.add(f"{tool_prefix}__{name}"[:64], definition.description, definition.tool_schema(), make())

    def add_finish(self, schema: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[str]]) -> None:
        self.add(
            "finish",
            "Call exactly once when the task is complete, with the required outputs.",
            schema,
            handler,
        )

    def add_delegate(self, agents: list[str], handler: Callable[[dict[str, Any]], Awaitable[str]]) -> None:
        self.add(
            "delegate",
            "Delegate a well-scoped sub-task to a specialist agent that shares this workspace. "
            "Returns the sub-agent's summary and outputs.",
            {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": agents},
                    "task": {"type": "string", "description": "Complete, self-contained instructions."},
                },
                "required": ["agent", "task"],
            },
            handler,
        )
