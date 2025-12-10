from __future__ import annotations

"""Workflow execution engine with logging, diffs, and error isolation."""

import ast
import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .graph import EdgeDefinition, Graph, graph_manager
from .persistence import persistence
from .state import WorkflowState, compute_state_diff

logger = logging.getLogger(__name__)


class StepLog(BaseModel):
    timestamp: datetime
    node: str
    success: bool
    error: Optional[str] = None
    error_type: Optional[str] = None
    state_diff: Dict[str, Any]
    duration_ms: float

    model_config = ConfigDict(from_attributes=True)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_MISSING_NODE = "failed_missing_node"
    HALTED_MAX_STEPS = "halted_max_steps"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ExecutionEngine:
    """Executes graphs while recording detailed logs and state transitions."""

    def __init__(self, max_steps: int = 500) -> None:
        self.max_steps = max_steps
        self._run_states: Dict[str, WorkflowState] = {}
        self._run_logs: Dict[str, List[StepLog]] = {}
        self._run_status: Dict[str, RunStatus] = {}
        self._run_errors: Dict[str, Optional[str]] = {}
        self._run_started_at: Dict[str, datetime] = {}
        self._run_finished_at: Dict[str, Optional[datetime]] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._run_tasks: Dict[str, asyncio.Task] = {}
        self._lock = Lock()

    def _prepare_run(
        self, graph_id: str, initial_state: Dict[str, Any], run_id: Optional[str] = None
    ) -> tuple[Graph, WorkflowState, str]:
        graph = graph_manager.get_graph(graph_id)
        rid = run_id or str(uuid4())
        now = datetime.now(timezone.utc)
        state = WorkflowState(run_id=rid, graph_id=graph_id, data=initial_state or {})
        with self._lock:
            self._run_states[rid] = state
            self._run_logs[rid] = []
            self._run_status[rid] = RunStatus.RUNNING
            self._run_errors[rid] = None
            self._run_started_at[rid] = now
            self._run_finished_at[rid] = None
            self._subscribers.setdefault(rid, [])
        if persistence:
            try:
                persistence.record_run_start(
                    run_id=rid,
                    graph_id=graph_id,
                    started_at=now,
                    initial_state=state.data,
                    status=RunStatus.RUNNING.value,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to persist run start '%s': %s", rid, exc)
        return graph, state, rid

    async def run_graph(
        self, graph_id: str, initial_state: Dict[str, Any], run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Execute a registered graph from its start node."""
        graph, state, rid = self._prepare_run(graph_id, initial_state, run_id)
        await self._execute(graph, state, rid)
        status_value = self._run_status.get(rid, RunStatus.UNKNOWN)
        status_str = status_value.value if isinstance(status_value, Enum) else str(status_value)
        return {
            "run_id": rid,
            "state": self._run_states.get(rid, state),
            "log": self._run_logs.get(rid, []),
            "status": status_str,
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
                self._update_status(
                    run_id, RunStatus.HALTED_MAX_STEPS, state, error="Max steps exceeded"
                )
                logger.warning(
                    "Run %s halted after reaching max steps (%s)", run_id, self.max_steps
                )
                break

            node_def = graph.nodes.get(current_node)
            if node_def is None:
                error_msg = f"Node '{current_node}' not found in graph"
                self._append_log(
                    run_id=run_id,
                    node=current_node,
                    success=False,
                    error=error_msg,
                    error_type="MissingNode",
                    state_diff={},
                    duration_ms=0.0,
                    state=state,
                )
                self._update_status(
                    run_id, RunStatus.FAILED_MISSING_NODE, state, error=error_msg
                )
                break

            before_state = state
            before_snapshot = dict(state.data)
            started_at = datetime.now(timezone.utc)
            try:
                state = await self._execute_node(node_def, state)
                success = True
                error = None
                error_type = None
            except Exception as exc:
                success = False
                error = str(exc)
                logger.exception("Node '%s' failed: %s", current_node, error)
                state = before_state.with_updates({}, error=error)
                error_type = exc.__class__.__name__

            duration_ms = (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
            state_diff = compute_state_diff(before_snapshot, state.data)
            self._append_log(
                run_id=run_id,
                node=current_node,
                success=success,
                error=error,
                error_type=error_type,
                state_diff=state_diff,
                duration_ms=duration_ms,
                state=state,
            )

            if not success:
                self._update_status(run_id, RunStatus.FAILED, state, error=error)
                break

            next_node = self._determine_next_node(graph, current_node, state)
            if not next_node:
                self._update_status(run_id, RunStatus.COMPLETED, state)
                break

            current_node = next_node
            steps += 1

        with self._lock:
            self._run_states[run_id] = state
        # Broadcast terminal status to subscribers.
        status_value = self._run_status.get(run_id, RunStatus.UNKNOWN)
        status_str = status_value.value if isinstance(status_value, Enum) else str(status_value)
        self._broadcast(
            run_id,
            {
                "type": "status",
                "data": status_str,
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
        """Safely evaluate a condition against the current state using a minimal AST whitelist."""

        allowed_names = {"state", "data"}
        allowed_calls = {"get", "len", "min", "max", "sum"}

        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            logger.warning("Rejected condition '%s': %s", expression, exc)
            return False

        def _validate(node: ast.AST) -> bool:
            if isinstance(node, ast.Expression):
                return _validate(node.body)
            if isinstance(node, ast.BoolOp):
                return all(_validate(value) for value in node.values)
            if isinstance(node, ast.UnaryOp):
                return isinstance(node.op, (ast.Not,)) and _validate(node.operand)
            if isinstance(node, ast.Compare):
                return _validate(node.left) and all(_validate(comparator) for comparator in node.comparators)
            if isinstance(node, ast.BinOp):
                return isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)) and _validate(node.left) and _validate(
                    node.right
                )
            if isinstance(node, ast.Name):
                return node.id in allowed_names and "__" not in node.id
            if isinstance(node, ast.Constant):
                return True
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("__"):
                    return False
                return _validate(node.value)
            if isinstance(node, ast.Subscript):
                return _validate(node.value) and _validate(node.slice)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr not in allowed_calls or node.func.attr.startswith("__"):
                        return False
                elif isinstance(node.func, ast.Name):
                    if node.func.id not in allowed_calls:
                        return False
                else:
                    return False
                return all(_validate(arg) for arg in node.args) and all(_validate(kw.value) for kw in node.keywords)
            return False

        if not _validate(tree):
            logger.warning("Rejected unsafe condition: %s", expression)
            return False

        safe_globals: Dict[str, Any] = {"__builtins__": {}}
        safe_locals: Dict[str, Any] = {"state": state, "data": state.data, "len": len, "min": min, "max": max, "sum": sum}
        try:
            return bool(eval(compile(tree, "<condition>", "eval"), safe_globals, safe_locals))
        except Exception as exc:
            logger.warning("Failed to evaluate condition '%s': %s", expression, exc)
            return False

    def _update_status(
        self,
        run_id: str,
        status: RunStatus,
        state: WorkflowState,
        error: Optional[str] = None,
    ) -> None:
        finished_at: Optional[datetime] = None
        if status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.FAILED_MISSING_NODE,
            RunStatus.HALTED_MAX_STEPS,
            RunStatus.TIMEOUT,
        ):
            finished_at = datetime.now(timezone.utc)

        with self._lock:
            self._run_status[run_id] = status
            self._run_states[run_id] = state
            self._run_finished_at[run_id] = finished_at
            if error:
                self._run_errors[run_id] = error

        if persistence and finished_at:
            try:
                persistence.record_run_finish(
                    run_id=run_id,
                    status=status.value,
                    finished_at=finished_at,
                    final_state=state.data,
                    last_error=error,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to persist run '%s' completion: %s", run_id, exc)

    def _append_log(
        self,
        run_id: str,
        node: str,
        success: bool,
        error: Optional[str],
        error_type: Optional[str],
        state_diff: Dict[str, Any],
        duration_ms: float,
        state: WorkflowState,
    ) -> None:
        entry = StepLog(
            timestamp=datetime.now(timezone.utc),
            node=node,
            success=success,
            error=error,
            error_type=error_type,
            state_diff=state_diff,
            duration_ms=duration_ms,
        )
        with self._lock:
            self._run_logs.setdefault(run_id, []).append(entry)
            self._run_states[run_id] = state
            step_no = len(self._run_logs[run_id])
        if persistence:
            try:
                persistence.append_step(
                    run_id=run_id,
                    step_no=step_no,
                    node=node,
                    timestamp=entry.timestamp,
                    success=success,
                    error=error,
                    error_type=error_type,
                    state_diff=state_diff,
                    duration_ms=duration_ms,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to persist step for run '%s': %s", run_id, exc)
        self._broadcast(run_id, {"type": "log", "data": entry.model_dump()})

    def get_state(self, run_id: str) -> WorkflowState:
        with self._lock:
            if run_id not in self._run_states:
                raise KeyError(f"Run '{run_id}' not found")
            return self._run_states[run_id]

    def get_timestamps(self, run_id: str) -> tuple[Optional[datetime], Optional[datetime]]:
        with self._lock:
            if run_id not in self._run_states:
                raise KeyError(f"Run '{run_id}' not found")
            return self._run_started_at.get(run_id), self._run_finished_at.get(run_id)

    def get_log(self, run_id: str) -> List[StepLog]:
        with self._lock:
            return list(self._run_logs.get(run_id, []))

    def get_status(self, run_id: str) -> RunStatus:
        with self._lock:
            return self._run_status.get(run_id, RunStatus.UNKNOWN)

    def get_run_error(self, run_id: str) -> Optional[str]:
        with self._lock:
            return self._run_errors.get(run_id)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        if persistence:
            try:
                return persistence.list_runs(limit=limit)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to list persisted runs: %s", exc)
        with self._lock:
            result: List[Dict[str, Any]] = []
            for run_id, status in list(self._run_status.items())[:limit]:
                result.append(
                    {
                        "run_id": run_id,
                        "graph_id": self._run_states.get(run_id, WorkflowState()).graph_id,
                        "status": status.value if isinstance(status, Enum) else status,
                        "started_at": self._run_started_at.get(run_id),
                        "finished_at": self._run_finished_at.get(run_id),
                        "last_error": self._run_errors.get(run_id),
                    }
                )
            return result

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
