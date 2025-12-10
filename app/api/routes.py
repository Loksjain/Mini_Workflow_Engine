from __future__ import annotations

"""FastAPI routes for graph creation, execution, and run state retrieval."""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, status, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.engine.executor import execution_engine
from app.engine.graph import EdgeDefinition, graph_manager

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
    status: str


class GraphStateResponse(BaseModel):
    run_id: str
    state: Dict[str, Any]
    execution_log: List[Dict[str, Any]]
    status: str


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
        status="running",
    )


@router.get("/graph/state/{run_id}", response_model=GraphStateResponse)
def get_graph_state(run_id: str) -> GraphStateResponse:
    """Retrieve state and logs for a given run."""
    try:
        state = execution_engine.get_state(run_id)
        log = execution_engine.get_log(run_id)
        status_str = execution_engine.get_status(run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return GraphStateResponse(
        run_id=run_id,
        state=state.data,
        execution_log=[entry.model_dump() for entry in log],
        status=status_str,
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
            {"type": "status", "data": execution_engine.get_status(run_id)}
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
