from __future__ import annotations

"""Registries for nodes and tools."""

import logging
from threading import Lock
from typing import Any, Callable, Dict, Optional

from .node import NodeDefinition

logger = logging.getLogger(__name__)


class NodeRegistry:
    """Tracks all node definitions. Nodes register automatically via decorator."""

    def __init__(self) -> None:
        self._nodes: Dict[str, NodeDefinition] = {}
        self._lock = Lock()

    def register(
        self,
        name: str,
        *,
        timeout_seconds: Optional[float] = None,
        description: Optional[str] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorator used to register a node.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            node_def = NodeDefinition.build(
                name=name,
                func=func,
                description=description,
                timeout_seconds=timeout_seconds,
            )
            with self._lock:
                self._nodes[name] = node_def
            logger.info(
                "Registered node '%s' (async=%s, timeout=%s)",
                name,
                node_def.is_async,
                timeout_seconds,
            )
            return func

        return decorator

    def get(self, name: str) -> NodeDefinition:
        if name not in self._nodes:
            raise KeyError(f"Node '{name}' is not registered")
        return self._nodes[name]

    def all(self) -> Dict[str, NodeDefinition]:
        return dict(self._nodes)


class ToolRegistry:
    """Simple dictionary-backed tool registry."""

    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}
        self._lock = Lock()

    def register(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            with self._lock:
                self._tools[name] = func
            logger.info("Registered tool '%s'", name)
            return func

        return decorator

    def get(self, name: str) -> Callable[..., Any]:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered")
        return self._tools[name]

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.get(name)(*args, **kwargs)

    def all(self) -> Dict[str, Callable[..., Any]]:
        return dict(self._tools)


node_registry = NodeRegistry()
tool_registry = ToolRegistry()
