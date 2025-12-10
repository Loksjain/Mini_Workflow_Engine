from __future__ import annotations

"""FastAPI routes for graph creation, execution, and run state retrieval."""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, status, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.engine.executor import RunStatus, execution_engine
from app.engine.graph import EdgeDefinition, graph_manager
from app.engine.persistence import persistence

router = APIRouter()


class EdgeInput(BaseModel):
    target: Optional[str]
    condition: Optional[str] = None
    description: Optional[str] = None


class GraphCreateRequest(BaseModel):
    nodes: List[str] = Field(..., description="List of registered node names")
    edges: Dict[str, Union[str, List[Union[str, EdgeInput]]]]
    start_node: Optional[str] = None
    description: Optional[str] = None
    graph_id: Optional[str] = None


class GraphCreateResponse(BaseModel):
    graph_id: str


class GraphRunRequest(BaseModel):
    graph_id: str
    initial_state: Dict[str, Any] = Field(default_factory=dict)


class GraphRunResponse(BaseModel):
    run_id: str
    final_state: Dict[str, Any]
    execution_log: List[Dict[str, Any]]
    status: RunStatus


class GraphStateResponse(BaseModel):
    run_id: str
    state: Dict[str, Any]
    execution_log: List[Dict[str, Any]]
    status: RunStatus
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None


class GraphSummaryResponse(BaseModel):
    graph_id: str
    description: Optional[str] = None
    start_node: Optional[str] = None
    node_count: int


class GraphDetailResponse(BaseModel):
    graph_id: str
    description: Optional[str] = None
    start_node: Optional[str] = None
    nodes: List[str]
    edges: Dict[str, List[EdgeInput]]


class RunSummaryResponse(BaseModel):
    run_id: str
    graph_id: Optional[str]
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None


class RunListResponse(BaseModel):
    runs: List[RunSummaryResponse]


@router.post("/graph/create", response_model=GraphCreateResponse)
def create_graph(payload: GraphCreateRequest) -> GraphCreateResponse:
    """Create and register a new graph from existing nodes."""
    try:
        # Normalize edges: convert Pydantic models to plain dicts for the manager.
        normalized_edges: Dict[str, Union[str, List[Union[str, Dict[str, str]]]]] = {}
        for origin, raw_value in payload.edges.items():
            if isinstance(raw_value, list):
                processed_list: List[Union[str, Dict[str, str]]] = []
                for item in raw_value:
                    if isinstance(item, EdgeInput):
                        processed_list.append(item.model_dump())
                    else:
                        processed_list.append(item)  # type: ignore[arg-type]
                normalized_edges[origin] = processed_list
            else:
                if isinstance(raw_value, EdgeInput):
                    normalized_edges[origin] = raw_value.model_dump()
                else:
                    normalized_edges[origin] = raw_value

        graph = graph_manager.create_graph(
            node_names=payload.nodes,
            edges_raw=normalized_edges,
            start_node=payload.start_node,
            graph_id=payload.graph_id,
            description=payload.description,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return GraphCreateResponse(graph_id=graph.id)


@router.post("/graph/run", response_model=GraphRunResponse)
async def run_graph(payload: GraphRunRequest) -> GraphRunResponse:
    """Execute a graph using the provided initial state (backgrounded)."""
    try:
        run_id = execution_engine.start_run_background(
            graph_id=payload.graph_id, initial_state=payload.initial_state
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return GraphRunResponse(
        run_id=run_id,
        final_state=payload.initial_state,
        execution_log=[],
        status=RunStatus.RUNNING,
    )


@router.get("/graph/state/{run_id}", response_model=GraphStateResponse)
def get_graph_state(run_id: str) -> GraphStateResponse:
    """Retrieve state and logs for a given run."""
    try:
        state = execution_engine.get_state(run_id)
        log = execution_engine.get_log(run_id)
        status_value = execution_engine.get_status(run_id)
        started_at, finished_at = execution_engine.get_timestamps(run_id)
        last_error = execution_engine.get_run_error(run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return GraphStateResponse(
        run_id=run_id,
        state=state.data,
        execution_log=[entry.model_dump() for entry in log],
        status=status_value,
        started_at=started_at.isoformat() if started_at else None,
        finished_at=finished_at.isoformat() if finished_at else None,
        last_error=last_error,
    )


@router.get("/graphs", response_model=List[GraphSummaryResponse])
def list_graphs() -> List[GraphSummaryResponse]:
    """List registered graph summaries."""
    records = graph_manager.list_graph_records()
    return [
        GraphSummaryResponse(
            graph_id=record["id"],
            description=record.get("description"),
            start_node=record.get("start_node"),
            node_count=len(record.get("nodes", [])),
        )
        for record in records
    ]


@router.get("/graphs/{graph_id}", response_model=GraphDetailResponse)
def get_graph(graph_id: str) -> GraphDetailResponse:
    """Retrieve a full graph definition."""
    records = graph_manager.list_graph_records()
    selected = next((g for g in records if g["id"] == graph_id), None)
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Graph '{graph_id}' not found"
        )
    # Normalize edges into EdgeInput for response typing
    edges: Dict[str, List[EdgeInput]] = {}
    for origin, edge_list in selected.get("edges", {}).items():
        edges[origin] = [
            EdgeInput(
                target=edge.get("target"),
                condition=edge.get("condition"),
                description=edge.get("description"),
            )
            for edge in edge_list
        ]
    return GraphDetailResponse(
        graph_id=selected["id"],
        description=selected.get("description"),
        start_node=selected.get("start_node"),
        nodes=selected.get("nodes", []),
        edges=edges,
    )


@router.get("/runs", response_model=RunListResponse)
def list_runs(limit: int = 50) -> RunListResponse:
    """List recent runs with status."""
    runs = execution_engine.list_runs(limit=limit)
    normalized: List[RunSummaryResponse] = []
    for entry in runs:
        normalized.append(
            RunSummaryResponse(
                run_id=entry.get("run_id") or entry.get("id"),
                graph_id=entry.get("graph_id"),
                status=str(entry.get("status")),
                started_at=entry.get("started_at"),
                finished_at=entry.get("finished_at"),
                last_error=entry.get("last_error"),
            )
        )
    return RunListResponse(runs=normalized)


@router.get("/runs/{run_id}", response_model=GraphStateResponse)
def get_run(run_id: str) -> GraphStateResponse:
    """Retrieve run details (final state, log, metadata)."""
    if persistence:
        db_run = persistence.get_run(run_id)
        if db_run:
            log_entries = persistence.get_run_steps(run_id)
            try:
                # If engine still has live state, prefer it for current snapshot.
                state_obj = execution_engine.get_state(run_id)
                state_data = state_obj.data
            except KeyError:
                state_data = db_run.get("final_state") or db_run.get("initial_state") or {}
            return GraphStateResponse(
                run_id=db_run["run_id"],
                state=state_data,
                execution_log=log_entries,
                status=db_run["status"],
                started_at=db_run.get("started_at"),
                finished_at=db_run.get("finished_at"),
                last_error=db_run.get("last_error"),
            )
    # Fallback to in-memory data
    try:
        state = execution_engine.get_state(run_id)
        log = execution_engine.get_log(run_id)
        status_value = execution_engine.get_status(run_id)
        started_at, finished_at = execution_engine.get_timestamps(run_id)
        last_error = execution_engine.get_run_error(run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return GraphStateResponse(
        run_id=run_id,
        state=state.data,
        execution_log=[entry.model_dump() for entry in log],
        status=status_value,
        started_at=started_at.isoformat() if started_at else None,
        finished_at=finished_at.isoformat() if finished_at else None,
        last_error=last_error,
    )


@router.websocket("/graph/stream/{run_id}")
async def stream_logs(websocket: WebSocket, run_id: str) -> None:
    """WebSocket endpoint to stream log events for a run."""
    await websocket.accept()
    try:
        queue = execution_engine.subscribe(run_id)
        # Send historical log entries first
        for entry in execution_engine.get_log(run_id):
            await websocket.send_json({"type": "log", "data": entry.model_dump()})
        await websocket.send_json(
            {
                "type": "status",
                "data": execution_engine.get_status(run_id).value,
                "state": execution_engine.get_state(run_id).data,
            }
        )
    except KeyError:
        await websocket.close(code=4404, reason="Run not found")
        return

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        execution_engine.unsubscribe(run_id, queue)
        try:
            await websocket.close()
        except RuntimeError:
            # Already closed
            pass
