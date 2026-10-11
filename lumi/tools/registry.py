"""Tool registry.

The registry is the catalogue the model is allowed to choose from.  It is also
the place that decides *what the model is told*: tools the user disabled, or
tools whose optional dependency is missing, are never advertised, so the
assistant cannot propose something it cannot do.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Iterable, Optional

from lumi.tools.base import RiskLevel, Tool, ToolContext
from lumi.tools.validation import validate_arguments

log = logging.getLogger("lumi.tools.registry")


class ToolRegistry:
    """Holds the tools available to the assistant."""

    def __init__(self) -> None:
        self._tools: "OrderedDict[str, Tool]" = OrderedDict()
        self._availability_cache: dict[int, tuple[bool, str]] = {}

    # ------------------------------------------------------------ mutation
    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if not tool.name:
            raise ValueError("a tool must define a name")
        if not tool.name.replace("_", "").isalnum() or tool.name.lower() != tool.name:
            raise ValueError(f"tool names must be lowercase snake_case: {tool.name!r}")
        if tool.name in self._tools and not replace:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def register_all(self, tools: Iterable[Tool], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    # ---------------------------------------------------------------- read
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def by_category(self) -> dict[str, list[Tool]]:
        grouped: dict[str, list[Tool]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.category, []).append(tool)
        return grouped

    # ------------------------------------------------------- availability
    def availability(self, tool: Tool, context: ToolContext) -> tuple[bool, str]:
        """Cache the (cheap) availability probe for the lifetime of a context."""
        key = id(context)
        cache = self._availability_cache.setdefault(key, {})  # type: ignore[assignment]
        if tool.name in cache:  # type: ignore[operator]
            return cache[tool.name]  # type: ignore[index]
        try:
            result = tool.is_available(context)
        except Exception as exc:  # pragma: no cover - a broken probe means unavailable
            log.warning("availability probe for %s failed: %s", tool.name, exc)
            result = (False, f"{tool.name} is unavailable ({type(exc).__name__}).")
        cache[tool.name] = result  # type: ignore[index]
        return result

    def forget_context(self, context: ToolContext) -> None:
        self._availability_cache.pop(id(context), None)

    def available(self, context: ToolContext) -> list[Tool]:
        return [tool for tool in self.all() if self.availability(tool, context)[0]]

    def unavailable(self, context: ToolContext) -> list[tuple[Tool, str]]:
        results: list[tuple[Tool, str]] = []
        for tool in self.all():
            ok, reason = self.availability(tool, context)
            if not ok:
                results.append((tool, reason))
        return results

    def permitted(self, context: ToolContext, policy: Any) -> list[Tool]:
        """Tools that are available *and* allowed by the permission policy."""
        allowed: list[Tool] = []
        for tool in self.available(context):
            decision = policy.evaluate(tool, {})
            if decision.allowed:
                allowed.append(tool)
        return allowed

    # ---------------------------------------------------------- descriptors
    def docs_for_prompt(self, context: ToolContext, policy: Any = None) -> list[str]:
        tools = self.permitted(context, policy) if policy is not None else self.available(context)
        return [tool.doc_line() for tool in tools]

    def schemas_for_model(self, context: ToolContext, policy: Any = None) -> list[dict[str, Any]]:
        tools = self.permitted(context, policy) if policy is not None else self.available(context)
        return [tool.to_schema() for tool in tools]

    def planner_tools(self, context: ToolContext, policy: Any = None) -> list[dict[str, Any]]:
        """Compact tool descriptions used by the constrained JSON planner."""
        tools = self.permitted(context, policy) if policy is not None else self.available(context)
        return [
            {
                "name": tool.name,
                "description": " ".join(tool.description.split()),
                "parameters": tool.parameters,
                "risk": tool.risk.value,
            }
            for tool in tools
        ]

    def validate_call(self, name: str, arguments: dict[str, Any]) -> list[str]:
        tool = self.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)[:12])
            return [f"unknown tool '{name}'. Available tools: {known}"]
        return validate_arguments(tool.parameters, arguments)

    def describe(self) -> list[dict[str, Any]]:
        return [tool.to_dict() for tool in self.all()]

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tool in self.all():
            counts[tool.risk.value] = counts.get(tool.risk.value, 0) + 1
        counts["total"] = len(self._tools)
        return counts

    # ------------------------------------------------------------- builders
    @classmethod
    def build_default(cls) -> "ToolRegistry":
        """Create the registry with every built-in tool registered.

        Import errors for individual built-ins are tolerated: a broken optional
        dependency must not stop LUMI from starting.
        """
        registry = cls()
        for loader in _BUILTIN_LOADERS:
            try:
                for tool in loader():
                    registry.register(tool, replace=True)
            except Exception:  # pragma: no cover - optional modules
                log.exception("could not load tool group: %s", getattr(loader, "__module__", loader))
        log.info("tool registry ready with %s tools", len(registry))
        return registry


def _load(module_path: str, function_name: str = "build_tools"):
    """Lazily import a builtin module and call its factory.

    Indirection keeps imports of optional third-party packages (pyautogui,
    pywinauto, winsdk) inside the modules that need them.
    """

    def loader():
        from importlib import import_module

        module = import_module(module_path)
        factory = getattr(module, function_name)
        return list(factory())

    return loader


#: Order defines the order tools appear in the prompt and the UI.
_BUILTIN_LOADERS = (
    _load("lumi.tools.builtin.info"),
    _load("lumi.tools.builtin.tasks"),
    _load("lumi.tools.builtin.notes"),
    _load("lumi.tools.builtin.reminders"),
    _load("lumi.tools.builtin.memory_tools"),
    _load("lumi.tools.builtin.productivity"),
    _load("lumi.tools.builtin.files"),
    _load("lumi.tools.builtin.computer"),
    _load("lumi.tools.builtin.browser"),
    _load("lumi.tools.builtin.system"),
)


__all__ = ["ToolRegistry", "RiskLevel", "Tool", "ToolContext"]
