from __future__ import annotations

"""Shared workflow state model and helpers."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkflowState(BaseModel):
    """Shared mutable state passed between nodes."""

    run_id: Optional[str] = None
    graph_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True
        orm_mode = True

    def with_updates(
        self, data_updates: Dict[str, Any], error: Optional[str] = None
    ) -> "WorkflowState":
        """Return a new WorkflowState with merged updates and optional error."""
        updated_data = {**self.data, **data_updates}
        updated_errors = self.errors + ([error] if error else [])
        return self.copy(update={"data": updated_data, "errors": updated_errors})


def compute_state_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute a minimal diff between two state dictionaries.
    """
    diff = {"added": {}, "updated": {}, "removed": []}  # type: ignore[var-annotated]

    for key, value in after.items():
        if key not in before:
            diff["added"][key] = value
        elif before[key] != value:
            diff["updated"][key] = {"from": before[key], "to": value}

    for key in before:
        if key not in after:
            diff["removed"].append(key)

    return diff
