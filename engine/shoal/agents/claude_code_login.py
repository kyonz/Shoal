"""Link a person's Claude subscription from the browser, through the unmodified Claude Code CLI.

``claude setup-token`` is Claude Code's own sign-in for headless use: it prints
an Anthropic sign-in URL, the person signs in on claude.ai and gets a code, and
pasting the code back makes the CLI print a long-lived token. Shoal runs that
command on the API server in a pseudo-terminal, shows the URL in the UI,
passes the person's code to the CLI and stores the printed token as the
person's own credential (encrypted, write-only). Sign-in itself completes on
Anthropic's pages; Shoal never sees a password.

Logins are kept in memory on the API replica that started them for a few
minutes, so with several replicas the UI needs sticky sessions (or people
paste a token they made with ``claude setup-token`` on their own machine).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import shutil
import struct
import tempfile
import termios
import time
from dataclasses import dataclass, field

from shoal.core.errors import InvalidRequest, NotFound
from shoal.core.ids import new_id

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
URL = re.compile(r"https://[^\s\x07\x1b]+/oauth/authorize\?[^\s\x07\x1b]+")
TOKEN = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_\-]{20,}")
FAILURE = re.compile(r"(OAuth error[^\r\n]*|Invalid code[^\r\n]*|Login failed[^\r\n]*)")
TTL = 600.0
ENTER_DELAY = 0.3


def plain(raw: str) -> str:
    """Terminal output as text: escape sequences (including cursor moves between words) become spaces."""
    return ANSI.sub(" ", raw)


@dataclass
class Login:
    id: str
    tenant_id: str
    owner: str
    gateway: str
    proc: asyncio.subprocess.Process
    fd: int
    home: str
    url: str = ""
    output: str = ""
    started: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.started > TTL


class ClaudeLogins:
    """Pending ``claude setup-token`` sessions, one per person and gateway."""

    def __init__(self, cli: str = "claude", timeout: float = 30.0):
        self.cli = cli
        self.timeout = timeout
        self._logins: dict[str, Login] = {}

    async def start(self, tenant_id: str, owner: str, gateway: str) -> Login:
        self._sweep()
        for login in [lg for lg in self._logins.values() if (lg.tenant_id, lg.owner, lg.gateway) == (tenant_id, owner, gateway)]:
            self._end(login)
        if shutil.which(self.cli) is None:
            raise InvalidRequest(
                f"the Claude Code CLI ('{self.cli}') is not installed on the Shoal API server; install it there "
                "(or set SHOAL_CLAUDE_CODE_CLI), or paste a token made with `claude setup-token`"
            )
        home = tempfile.mkdtemp(prefix="shoal-claude-login-")
        # A throwaway HOME, so nothing is written to the server's own profile, and no inherited credentials.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
        env.update({"HOME": home, "TERM": "xterm-256color", "BROWSER": "true"})
        fd, tty = pty.openpty()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))  # wide: URLs stay on one line
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli, "setup-token", stdin=tty, stdout=tty, stderr=tty, env=env, cwd=home, start_new_session=True
            )
        finally:
            os.close(tty)
        os.set_blocking(fd, False)
        login = Login(new_id("clogin"), tenant_id, owner, gateway, proc, fd, home)
        self._logins[login.id] = login
        try:
            text = await self._read_until(login, lambda out: URL.search(out) is not None and "aste" in out)
        except Exception:
            self._end(login)
            raise
        match = URL.search(text)
        if match is None:
            self._end(login)
            raise InvalidRequest("Claude Code did not show a sign-in URL", details={"output": plain(text)[-1000:]})
        login.url = match.group(0)
        return login

    async def finish(self, tenant_id: str, owner: str, login_id: str, code: str) -> tuple[str, str]:
        """Pass the code to the CLI. Returns (gateway, token); the login ends either way."""
        login = self._logins.get(login_id)
        if login is None or (login.tenant_id, login.owner) != (tenant_id, owner) or login.expired():
            raise NotFound("that sign-in has expired; start again")
        code = code.strip()
        if not code or any(c in code for c in "\r\n\x03\x04\x1b"):
            raise InvalidRequest("paste the code exactly as claude.ai shows it")
        try:
            # The CLI reads a burst of input as a paste, where Enter is a newline, so Enter goes separately.
            os.write(login.fd, code.encode())
            await asyncio.sleep(ENTER_DELAY)
            os.write(login.fd, b"\r")
            text = await self._read_until(login, lambda out: TOKEN.search(plain(out)) is not None or FAILURE.search(plain(out)) is not None)
            if (token := TOKEN.search(plain(text))) is None:
                failure = FAILURE.search(plain(text))
                raise InvalidRequest(f"Claude sign-in failed: {failure.group(0).strip() if failure else 'no token'}; start again")
            return login.gateway, token.group(0)
        finally:
            self._end(login)

    def cancel(self, tenant_id: str, owner: str, login_id: str) -> None:
        login = self._logins.get(login_id)
        if login is not None and (login.tenant_id, login.owner) == (tenant_id, owner):
            self._end(login)

    def close(self) -> None:
        for login in list(self._logins.values()):
            self._end(login)

    async def _read_until(self, login: Login, done: object) -> str:
        deadline = time.monotonic() + self.timeout
        start = len(login.output)
        while time.monotonic() < deadline:
            try:
                chunk = os.read(login.fd, 65536)
            except BlockingIOError:
                chunk = None
            except OSError:  # the CLI exited
                break
            if chunk == b"":
                break
            if chunk:
                login.output += chunk.decode(errors="replace")
                if done(login.output[start:]):  # type: ignore[operator]
                    return login.output[start:]
                continue
            await asyncio.sleep(0.05)
        return login.output[start:]

    def _end(self, login: Login) -> None:
        self._logins.pop(login.id, None)
        with contextlib.suppress(ProcessLookupError):
            login.proc.kill()  # asyncio reaps it
        with contextlib.suppress(OSError):
            os.close(login.fd)
        shutil.rmtree(login.home, ignore_errors=True)

    def _sweep(self) -> None:
        for login in [lg for lg in self._logins.values() if lg.expired()]:
            self._end(login)
