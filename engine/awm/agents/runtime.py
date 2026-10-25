"""Agent runtime contract and shared helpers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from awm.core.models import AgentSpec, ParamSpec, SkillSpec, params_json_schema

if TYPE_CHECKING:
    from awm.runtime.context import StepContext
    from awm.sandbox.base import Sandbox

SKILLS_DIR = ".awm/skills"


@dataclass
class AgentInvocation:
    ctx: StepContext
    agent_name: str
    agent: AgentSpec
    prompt: str
    outputs: dict[str, ParamSpec]
    sandbox: Sandbox
    skills: dict[str, SkillSpec] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    provider_config: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    depth: int = 0

    def finish_schema(self) -> dict[str, Any]:
        schema = params_json_schema(self.outputs)
        schema["properties"]["summary"] = {
            "type": "string",
            "description": "Short summary of what you did and the outcome.",
        }
        schema["required"] = [*schema["required"], "summary"]
        return schema


@dataclass
class AgentResult:
    outputs: dict[str, Any]
    summary: str = ""
    usage: dict[str, float] = field(default_factory=dict)
    turns: int = 0


class AgentRuntime(abc.ABC):
    name: str

    @abc.abstractmethod
    async def run(self, invocation: AgentInvocation) -> AgentResult: ...


async def install_skills(invocation: AgentInvocation) -> list[tuple[str, str, str]]:
    """Write skills into the sandbox. Returns (name, description, path) for the prompt."""
    listed = []
    for name, skill in invocation.skills.items():
        base = f"{SKILLS_DIR}/{name}"
        body = f"---\nname: {name}\ndescription: {skill.description}\n---\n\n{skill.instructions}\n"
        await invocation.sandbox.write_file(f"{base}/SKILL.md", body)
        for rel, content in skill.files.items():
            await invocation.sandbox.write_file(f"{base}/{rel.lstrip('/')}", content)
        listed.append((name, skill.description, invocation.sandbox.resolve(f"{base}/SKILL.md")))
    return listed


def outputs_description(outputs: dict[str, ParamSpec]) -> str:
    if not outputs:
        return "- summary (string): what you did and the outcome"
    lines = [f"- {name} ({spec.type}){': ' + spec.description if spec.description else ''}" for name, spec in outputs.items()]
    lines.append("- summary (string): what you did and the outcome")
    return "\n".join(lines)
