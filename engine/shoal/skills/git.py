"""Skills from a git repository in the Agent Skills layout.

Every directory under ``path`` that holds a ``SKILL.md`` is a skill; its id is
the directory relative to ``path`` (``skills/pdf``), and the other files in it
are the skill's files. That covers single-skill repositories, ``skills/<name>/``
collections and plugin marketplaces (``plugins/<p>/skills/<name>/``) alike.

The source is pinned to ``ref`` (a branch, tag or commit). Branches are
re-fetched at most every ``refresh_seconds``; imports record the commit they
were read from. Fetching a skill at a ``version`` reads it at that ref instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator

from shoal.skills.base import (
    MAX_SKILL_BYTES,
    SkillBundle,
    SkillPage,
    SkillSource,
    SkillSourceContext,
    SkillSourceError,
    SkillSummary,
    parse_skill_md,
    skill_tags,
    skill_version,
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
SHA = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
REMOTE = re.compile(r"^(https?|ssh)://|^[\w.-]+@[\w.-]+:")


class GitSourceConfig(BaseModel):
    url: str = Field(description="Repository URL (https:// or ssh).")
    ref: str = Field("main", description="Branch, tag or commit the source is pinned to.")
    path: str = Field("", description="Subdirectory to look for skills in.")
    token: SecretStr | None = Field(None, description="Token for private repositories over HTTPS.")
    username: str = Field("x-access-token", description="User name sent with the token.")
    refresh_seconds: int = Field(300, ge=0, description="How long a fetched branch is reused before fetching again.")

    @field_validator("ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not REF.match(value) or ".." in value:
            raise ValueError("not a valid git ref")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        value = value.strip("/")
        if value.startswith(".") or "/." in value or "\\" in value:
            raise ValueError("path must be a plain relative directory")
        return value


class _Checkout:
    def __init__(self, directory: Path, sha: str, fetched_at: float):
        self.directory = directory
        self.sha = sha
        self.fetched_at = fetched_at


class GitSkillSource(SkillSource):
    key = "git"
    title = "Git repository"
    description = "Skills in the Agent Skills layout (a SKILL.md per directory) from a git repository, pinned to a ref."
    config_model = GitSourceConfig

    def __init__(self) -> None:
        self._checkouts: dict[tuple[str, str], _Checkout] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------ catalogue

    async def list(self, ctx: SkillSourceContext, cursor: str | None = None, limit: int = 50) -> SkillPage:
        checkout = await self._checkout(ctx, ctx.config.ref)
        skills = [self._summary(skill_id, d, checkout.sha) for skill_id, d in sorted(self._index(checkout, ctx.config).items())]
        start = int(cursor or 0)
        end = start + limit
        return SkillPage(items=skills[start:end], next_cursor=str(end) if end < len(skills) else None)

    async def fetch(self, ctx: SkillSourceContext, skill_id: str, version: str | None = None) -> SkillBundle:
        checkout = await self._checkout(ctx, version or ctx.config.ref)
        directory = self._index(checkout, ctx.config).get(skill_id)
        if directory is None:
            raise SkillSourceError(f"no skill '{skill_id}' at {version or ctx.config.ref}", status=404)
        meta, body = parse_skill_md((directory / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        files, skipped = _read_files(directory)
        return SkillBundle(
            id=skill_id,
            name=str(meta.get("name") or directory.name),
            description=str(meta.get("description") or ""),
            version=skill_version(meta) or checkout.sha[:12],
            ref=checkout.sha,
            instructions=body,
            files=files,
            metadata=meta,
            skipped=skipped,
        )

    async def test(self, ctx: SkillSourceContext) -> dict[str, Any]:
        checkout = await self._checkout(ctx, ctx.config.ref, force=True)
        return {"ok": True, "commit": checkout.sha, "skills": len(self._index(checkout, ctx.config))}

    # ------------------------------------------------------------------ discovery

    def _index(self, checkout: _Checkout, config: GitSourceConfig) -> dict[str, Path]:
        root = (checkout.directory / config.path).resolve()
        if not root.is_relative_to(checkout.directory.resolve()) or not root.is_dir():
            raise SkillSourceError(f"path '{config.path}' is not a directory in the repository", status=404)
        out: dict[str, Path] = {}
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            if "SKILL.md" in files:
                directory = Path(current)
                rel = directory.relative_to(root).as_posix()
                out[rel if rel != "." else _root_id(directory)] = directory
        return out

    def _summary(self, skill_id: str, directory: Path, sha: str) -> SkillSummary:
        try:
            meta, _ = parse_skill_md((directory / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        except SkillSourceError:
            meta = {}
        return SkillSummary(
            id=skill_id,
            name=str(meta.get("name") or directory.name),
            description=str(meta.get("description") or ""),
            version=skill_version(meta) or sha[:12],
            tags=skill_tags(meta),
        )

    # ------------------------------------------------------------------ git

    def _cache_root(self, ctx: SkillSourceContext) -> Path:
        configured = getattr(getattr(ctx.engine, "settings", None), "skill_source_cache_dir", None)
        return Path(configured or Path(tempfile.gettempdir()) / "shoal-skill-sources")

    def _check_url(self, ctx: SkillSourceContext, url: str) -> None:
        allow_local = getattr(getattr(ctx.engine, "settings", None), "skill_source_allow_local", False)
        if REMOTE.match(url):
            return
        if allow_local and (url.startswith("file://") or url.startswith("/")):
            return
        raise SkillSourceError("git skill sources need an https:// or ssh URL", status=400)

    async def _checkout(self, ctx: SkillSourceContext, ref: str, *, force: bool = False) -> _Checkout:
        config: GitSourceConfig = ctx.config
        if not REF.match(ref) or ".." in ref:
            raise SkillSourceError(f"'{ref}' is not a valid git ref", status=400)
        self._check_url(ctx, config.url)
        key = (config.url, ref)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._checkouts.get(key)
            fresh = cached and cached.directory.exists() and (
                SHA.match(ref) or time.monotonic() - cached.fetched_at < config.refresh_seconds
            )
            if cached and fresh and not force:
                return cached
            digest = hashlib.sha256(f"{config.url}\n{ref}".encode()).hexdigest()[:24]
            directory = self._cache_root(ctx) / digest
            checkout = await self._fetch(config, ref, directory)
            self._checkouts[key] = checkout
            return checkout

    async def _fetch(self, config: GitSourceConfig, ref: str, directory: Path) -> _Checkout:
        staging = directory.with_name(directory.name + f".tmp{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        env = _git_env(config)
        try:
            await _git(env, "init", "-q", str(staging))
            await _git(env, "-C", str(staging), "fetch", "-q", "--depth", "1", "--no-tags", config.url, ref)
            await _git(env, "-C", str(staging), "checkout", "-q", "--detach", "FETCH_HEAD")
            sha = (await _git(env, "-C", str(staging), "rev-parse", "HEAD")).strip()
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(directory, ignore_errors=True)
        staging.rename(directory)
        return _Checkout(directory, sha, time.monotonic())


def _root_id(directory: Path) -> str:
    """A skill at the repository root is named after its SKILL.md (or the directory)."""
    try:
        meta, _ = parse_skill_md((directory / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    except SkillSourceError:
        meta = {}
    return str(meta.get("name") or directory.name)


def _read_files(directory: Path) -> tuple[dict[str, str], list[str]]:
    files: dict[str, str] = {}
    skipped: list[str] = []
    total = (directory / "SKILL.md").stat().st_size
    for current, dirs, names in os.walk(directory):
        here = Path(current)
        # A nested directory with its own SKILL.md is another skill, not part of this one.
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (here / d / "SKILL.md").exists())
        for name in sorted(names):
            path = here / name
            rel = path.relative_to(directory).as_posix()
            if rel == "SKILL.md" or path.is_symlink():
                if path.is_symlink():
                    skipped.append(rel)
                continue
            size = path.stat().st_size
            if total + size > MAX_SKILL_BYTES:
                skipped.append(rel)
                continue
            try:
                files[rel] = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(rel)
                continue
            total += size
    return files, skipped


def _git_env(config: GitSourceConfig) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:http:ssh:file",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
                 "GIT_SSL_CAINFO", "SSL_CERT_FILE", "GIT_SSH_COMMAND"):
        if name in os.environ:
            env[name] = os.environ[name]
    if config.token is not None:
        # Through the environment, not argv or .git/config, so the token never lands on disk or in `ps`.
        basic = base64.b64encode(f"{config.username}:{config.token.get_secret_value()}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


async def _git(env: dict[str, str], *args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise SkillSourceError("git is not installed on this server") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except TimeoutError as exc:
        proc.kill()
        raise SkillSourceError(f"git {args[0] if args[0] != '-C' else args[2]} timed out", retryable=True) from exc
    if proc.returncode != 0:
        message = err.decode(errors="replace").strip().splitlines()
        raise SkillSourceError(f"git failed: {message[-1] if message else proc.returncode}", status=502, retryable=True)
    return out.decode()
