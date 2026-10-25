"""Create a new integration project from the template (``awm integrations new linear``).

The template (``templates/integration`` in the repository, bundled into the
wheel) is itself a working, tested integration called "starter"; scaffolding
copies it and renames every "starter" token::

    python -m awm.sdk.scaffold linear --title Linear --dest ./awm-linear
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

_SKIP = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "uv.lock"}


def template_dir() -> Path:
    bundled = Path(__file__).with_name("_template")
    if bundled.is_dir():
        return bundled
    source = Path(__file__).resolve().parents[3] / "templates" / "integration"
    if source.is_dir():
        return source
    raise FileNotFoundError("integration template not found (expected awm/sdk/_template or templates/integration)")


def names(name: str, title: str | None = None) -> dict[str, str]:
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", name.strip()) if w]
    if not words or not words[0][0].isalpha():
        raise ValueError(f"integration name {name!r} must start with a letter, e.g. 'linear' or 'service-now'")
    snake = "_".join(w.lower() for w in words)
    return {
        "awm_starter": f"awm_{snake}",
        "awm-starter": "awm-" + "-".join(w.lower() for w in words),
        "STARTER": snake.upper(),
        "Starter": title or "".join(w[:1].upper() + w[1:] for w in words),
        "starter": snake,
    }


def _rename(text: str, mapping: dict[str, str]) -> str:
    pattern = re.compile("|".join(re.escape(k) for k in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def new_integration(name: str, dest: str | Path | None = None, *, title: str | None = None, force: bool = False) -> Path:
    """Write a new integration project; returns its directory."""
    mapping = names(name, title)
    # The class name must be an identifier even when the title has spaces ("Service Now").
    class_prefix = re.sub(r"\W", "", mapping["Starter"]) or mapping["starter"].title()
    target = Path(dest or mapping["awm-starter"]).resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(f"{target} exists and is not empty")
    source = template_dir()
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part in _SKIP for part in rel.parts) or path.suffix == ".pyc":
            continue
        out = target / _rename(str(rel), mapping)
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text()
        text = text.replace("StarterIntegration", f"{class_prefix}Integration")
        text = text.replace("StarterConnection", f"{class_prefix}Connection")
        text = text.replace("TestStarterContract", f"Test{class_prefix}Contract")
        out.write_text(_rename(text, mapping))
        shutil.copymode(path, out)
    return target


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - CLI wrapper
    parser = argparse.ArgumentParser(description="Create a new AWM integration project.")
    parser.add_argument("name", help="Integration key, e.g. linear")
    parser.add_argument("--title")
    parser.add_argument("--dest")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    path = new_integration(args.name, args.dest, title=args.title, force=args.force)
    print(f"Created {path}\n  cd {path} && uv sync && uv run pytest")


if __name__ == "__main__":  # pragma: no cover
    main()
