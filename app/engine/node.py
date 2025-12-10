from __future__ import annotations

"""Node definitions for the workflow engine."""

from dataclasses import dataclass
from typing import Any, Callable, Optional
import inspect

NodeCallable = Callable[..., Any]


@dataclass
class NodeDefinition:
    """
    Represents a registered node.

    A node is simply a callable that receives a WorkflowState and returns either:
    - a WorkflowState instance
    - a dict of state updates
    - None (state remains unchanged)
    """

    name: str
    func: NodeCallable
    description: Optional[str] = None
    timeout_seconds: Optional[float] = None
    is_async: bool = False

    @classmethod
    def build(
        cls,
        name: str,
        func: NodeCallable,
        description: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> "NodeDefinition":
        return cls(
            name=name,
            func=func,
            description=description,
            timeout_seconds=timeout_seconds,
            is_async=inspect.iscoroutinefunction(func),
        )
