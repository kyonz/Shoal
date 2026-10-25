"""Plugin registry.

Plugins are discovered from Python entry points (``shoal.sandboxes``,
``shoal.integrations``, ``shoal.providers``, ``shoal.agent_runtimes``,
``shoal.credentials``, ``shoal.secrets``, ``shoal.steps``,
``shoal.decision_providers``, ``shoal.eval_sinks``, ``shoal.skill_sources``) and can also be
registered programmatically, which is how tests inject fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from shoal.core.errors import PluginError

log = logging.getLogger(__name__)

GROUPS = (
    "sandboxes",
    "integrations",
    "providers",
    "agent_runtimes",
    "credentials",
    "secrets",
    "steps",
    "decision_providers",
    "eval_sinks",
    "skill_sources",
    "orchestrators",
)


class Registry:
    def __init__(self, *, load_entry_points: bool = True):
        self._factories: dict[str, dict[str, Any]] = {g: {} for g in GROUPS}
        self._instances: dict[tuple[str, str], Any] = {}
        self._errors: dict[tuple[str, str], str] = {}
        if load_entry_points:
            self._discover()

    def _discover(self) -> None:
        for group in GROUPS:
            for ep in entry_points(group=f"shoal.{group}"):
                self._factories[group].setdefault(ep.name, ep)

    def register(self, group: str, name: str, factory: Any, *, replace: bool = True) -> None:
        """Register a class, factory callable or ready instance."""
        if group not in self._factories:
            raise PluginError(f"unknown plugin group '{group}'")
        if not replace and name in self._factories[group]:
            return
        self._factories[group][name] = factory
        self._instances.pop((group, name), None)

    def names(self, group: str) -> list[str]:
        return sorted(self._factories[group])

    def get(self, group: str, name: str) -> Any:
        key = (group, name)
        if key in self._instances:
            return self._instances[key]
        factory = self._factories.get(group, {}).get(name)
        if factory is None:
            available = ", ".join(self.names(group)) or "none"
            raise PluginError(f"no {group[:-1]} plugin named '{name}' (available: {available})")
        try:
            if hasattr(factory, "load") and hasattr(factory, "group"):  # EntryPoint
                factory = factory.load()
            instance = factory() if isinstance(factory, type) or _is_factory(factory) else factory
        except Exception as exc:  # pragma: no cover - surfaced to the caller
            self._errors[key] = str(exc)
            raise PluginError(f"failed to load {group} plugin '{name}': {exc}") from exc
        self._instances[key] = instance
        return instance

    def available(self, group: str) -> dict[str, Any]:
        """Instances of every loadable plugin in a group; broken ones are logged and skipped."""
        out: dict[str, Any] = {}
        for name in self.names(group):
            try:
                out[name] = self.get(group, name)
            except PluginError as exc:
                log.warning("%s", exc)
        return out


def _is_factory(obj: Any) -> bool:
    return isinstance(obj, Callable) and getattr(obj, "__shoal_factory__", False)  # type: ignore[arg-type]


def factory(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Mark a zero-argument function as a plugin factory."""
    fn.__shoal_factory__ = True  # type: ignore[attr-defined]
    return fn
