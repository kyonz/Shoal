"""Helpers for backends whose exec channel cannot separate stdout/stderr/exit code."""

from __future__ import annotations

import base64

from awm.sandbox.base import ExecResult, shell_quote

MARKER = "__AWM_RESULT__"


def wrapped_command(command: str | list[str], *, cwd: str, env: dict[str, str] | None, timeout: float | None) -> list[str]:
    """A ``sh -c`` command that prints ``MARKER rc b64(stdout) b64(stderr)`` on one line."""
    cmd = command if isinstance(command, str) else " ".join(shell_quote(c) for c in command)
    exports = "".join(f"export {k}={shell_quote(v)}; " for k, v in (env or {}).items())
    inner = f"cd {shell_quote(cwd)} && {exports}{cmd}"
    runner = f"timeout {int(timeout)} sh -lc {shell_quote(inner)}" if timeout else f"sh -lc {shell_quote(inner)}"
    script = (
        'O=$(mktemp); E=$(mktemp); '
        f'{runner} >"$O" 2>"$E"; RC=$?; '
        f'printf "{MARKER} %s %s %s\\n" "$RC" "$(base64 <"$O" | tr -d \'\\n\')" "$(base64 <"$E" | tr -d \'\\n\')"; '
        'rm -f "$O" "$E"'
    )
    return ["sh", "-c", script]


def parse_wrapped(output: str, duration: float) -> ExecResult:
    for line in reversed(output.splitlines()):
        if line.startswith(MARKER):
            parts = line.split(" ")
            rc = int(parts[1])
            out = base64.b64decode(parts[2]).decode(errors="replace") if len(parts) > 2 and parts[2] else ""
            err = base64.b64decode(parts[3]).decode(errors="replace") if len(parts) > 3 and parts[3] else ""
            return ExecResult(exit_code=rc, stdout=out, stderr=err, timed_out=rc == 124, duration_seconds=duration)
    return ExecResult(exit_code=-1, stdout=output, stderr="sandbox exec returned no result marker", duration_seconds=duration)


def write_chunks(path: str, data: bytes, chunk: int = 48 * 1024) -> list[list[str]]:
    """Commands that write ``data`` to ``path`` via base64 chunks in argv (no stdin needed)."""
    encoded = base64.b64encode(data).decode()
    directory = path.rsplit("/", 1)[0] or "/"
    commands = [["sh", "-c", f"mkdir -p {shell_quote(directory)} && : > {shell_quote(path)}"]]
    for i in range(0, len(encoded), chunk):
        piece = encoded[i : i + chunk]
        commands.append(["sh", "-c", f"printf %s {shell_quote(piece)} | base64 -d >> {shell_quote(path)}"])
    return commands
