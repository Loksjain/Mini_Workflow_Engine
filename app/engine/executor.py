from __future__ import annotations

"""Workflow execution engine with logging, diffs, and error isolation."""

import asyncio
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .graph import EdgeDefinition, Graph, graph_manager
from .state import WorkflowState, compute_state_diff

logger = logging.getLogger(__name__)


class StepLog(BaseModel):
    timestamp: datetime
    node: str
    success: bool
    error: Optional[str] = None
    state_diff: Dict[str, Any]
    duration_ms: float

    model_config = ConfigDict(from_attributes=True)


class ExecutionEngine:
    """Executes graphs while recording detailed logs and state transitions."""

    def __init__(self, max_steps: int = 500) -> None:
        self.max_steps = max_steps
        self._run_states: Dict[str, WorkflowState] = {}
        self._run_logs: Dict[str, List[StepLog]] = {}
        self._run_status: Dict[str, str] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._run_tasks: Dict[str, asyncio.Task] = {}
        self._lock = Lock()

    def _prepare_run(
        self, graph_id: str, initial_state: Dict[str, Any], run_id: Optional[str] = None
    ) -> tuple[Graph, WorkflowState, str]:
        graph = graph_manager.get_graph(graph_id)
        rid = run_id or str(uuid4())
        state = WorkflowState(run_id=rid, graph_id=graph_id, data=initial_state or {})
        with self._lock:
            self._run_states[rid] = state
            self._run_logs[rid] = []
            self._run_status[rid] = "running"
            self._subscribers.setdefault(rid, [])
        return graph, state, rid

    async def run_graph(
        self, graph_id: str, initial_state: Dict[str, Any], run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Execute a registered graph from its start node."""
        graph, state, rid = self._prepare_run(graph_id, initial_state, run_id)
        await self._execute(graph, state, rid)
        return {
            "run_id": rid,
            "state": self._run_states.get(rid, state),
            "log": self._run_logs.get(rid, []),
            "status": self._run_status.get(rid, "unknown"),
        }

    def start_run_background(
        self, graph_id: str, initial_state: Dict[str, Any]
    ) -> str:
        """Kick off a run in the background and return its run_id immediately."""
        graph, state, rid = self._prepare_run(graph_id, initial_state)

        async def runner() -> None:
            await self._execute(graph, state, rid)

        task = asyncio.create_task(runner())
        with self._lock:
            self._run_tasks[rid] = task
        task.add_done_callback(lambda _: self._run_tasks.pop(rid, None))
        return rid

    async def _execute(self, graph: Graph, state: WorkflowState, run_id: str) -> None:
        """Main execution loop shared by synchronous and background runs."""
        current_node = graph.start_node
        steps = 0

        while current_node:
            if steps >= self.max_steps:
                self._run_status[run_id] = "halted_max_steps"
                logger.warning(
                    "Run %s halted after reaching max steps (%s)", run_id, self.max_steps
                )
                break

            node_def = graph.nodes.get(current_node)
            if node_def is None:
                self._run_status[run_id] = "failed_missing_node"
                error_msg = f"Node '{current_node}' not found in graph"
                self._append_log(
                    run_id=run_id,
                    node=current_node,
                    success=False,
                    error=error_msg,
                    state_diff={},
                    duration_ms=0.0,
                    state=state,
                )
                break

            before_state = state
            before_snapshot = dict(state.data)
            started_at = datetime.now(timezone.utc)
            try:
                state = await self._execute_node(node_def, state)
                success = True
                error = None
            except Exception as exc:
                success = False
                error = str(exc)
                logger.exception("Node '%s' failed: %s", current_node, error)
                state = before_state.with_updates({}, error=error)

            duration_ms = (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
            state_diff = compute_state_diff(before_snapshot, state.data)
            self._append_log(
                run_id=run_id,
                node=current_node,
                success=success,
                error=error,
                state_diff=state_diff,
                duration_ms=duration_ms,
                state=state,
            )

            if not success:
                self._run_status[run_id] = "failed"
                break

            next_node = self._determine_next_node(graph, current_node, state)
            if not next_node:
                self._run_status[run_id] = "completed"
                break

            current_node = next_node
            steps += 1

        with self._lock:
            self._run_states[run_id] = state
        # Broadcast terminal status to subscribers.
        self._broadcast(
            run_id,
            {
                "type": "status",
                "data": self._run_status.get(run_id, "unknown"),
                "state": state.data,
            },
        )
        self._broadcast(run_id, None)  # Sentinel to close streams.

    async def _execute_node(self, node_def, state: WorkflowState) -> WorkflowState:
        """
        Execute a node with optional timeout and async support.
        """
        if node_def.is_async:
            coroutine = node_def.func(state)
        else:
            # Run sync functions in a thread to avoid blocking event loop.
            coroutine = asyncio.to_thread(node_def.func, state)

        if node_def.timeout_seconds:
            result = await asyncio.wait_for(coroutine, timeout=node_def.timeout_seconds)
        else:
            result = await coroutine

        if isinstance(result, WorkflowState):
            return result
        if isinstance(result, dict):
            return state.with_updates(result)
        return state

    def _determine_next_node(
        self, graph: Graph, current_node: str, state: WorkflowState
    ) -> Optional[str]:
        edges: List[EdgeDefinition] = graph.edges.get(current_node, [])
        fallback: Optional[str] = None
        for edge in edges:
            if edge.condition:
                if self._evaluate_condition(edge.condition, state):
                    return edge.target
            else:
                fallback = edge.target
        return fallback

    def _evaluate_condition(self, expression: str, state: WorkflowState) -> bool:
        """Evaluate a simple condition string against the current state.

        Guard against dangerous patterns by disallowing dunder access and limiting
        globals. This stays intentionally constrained while still using eval.
        """
        if "__" in expression:
            logger.warning("Rejected condition with dunder access: %s", expression)
            return False
        safe_globals = {
            "__builtins__": {"len": len, "min": min, "max": max, "sum": sum}
        }
        safe_locals = {"state": state}
        try:
            return bool(eval(expression, safe_globals, safe_locals))
        except Exception as exc:
            logger.warning("Failed to evaluate condition '%s': %s", expression, exc)
            return False

    def _append_log(
        self,
        run_id: str,
        node: str,
        success: bool,
        error: Optional[str],
        state_diff: Dict[str, Any],
        duration_ms: float,
        state: WorkflowState,
    ) -> None:
        entry = StepLog(
            timestamp=datetime.now(timezone.utc),
            node=node,
            success=success,
            error=error,
            state_diff=state_diff,
            duration_ms=duration_ms,
        )
        with self._lock:
            self._run_logs.setdefault(run_id, []).append(entry)
            self._run_states[run_id] = state
        self._broadcast(run_id, {"type": "log", "data": entry.model_dump()})

    def get_state(self, run_id: str) -> WorkflowState:
        with self._lock:
            if run_id not in self._run_states:
                raise KeyError(f"Run '{run_id}' not found")
            return self._run_states[run_id]

    def get_log(self, run_id: str) -> List[StepLog]:
        with self._lock:
            return list(self._run_logs.get(run_id, []))

    def get_status(self, run_id: str) -> str:
        with self._lock:
            return self._run_status.get(run_id, "unknown")

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """Subscribe to log events for a given run."""
        with self._lock:
            if run_id not in self._run_logs:
                raise KeyError(f"Run '{run_id}' not found")
            queue: asyncio.Queue = asyncio.Queue()
            self._subscribers.setdefault(run_id, []).append(queue)
            return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            if run_id in self._subscribers:
                self._subscribers[run_id] = [
                    q for q in self._subscribers[run_id] if q is not queue
                ]
                if not self._subscribers[run_id]:
                    self._subscribers.pop(run_id, None)

    def _broadcast(self, run_id: str, event: Any) -> None:
        """Push an event to all subscribers."""
        with self._lock:
            queues = list(self._subscribers.get(run_id, []))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:
                logger.debug("Failed to publish event to subscriber", exc_info=True)


execution_engine = ExecutionEngine()
