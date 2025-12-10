import asyncio
from uuid import uuid4

import pytest

from app.engine.executor import execution_engine
from app.engine.graph import graph_manager
from app.engine.registry import node_registry
from app.engine.state import WorkflowState


# Test nodes ---------------------------------------------------------------


@node_registry.register("test_increment")
def node_test_increment(state: WorkflowState) -> WorkflowState:
    value = state.data.get("value", 0) + 1
    return state.with_updates({"value": value})


@node_registry.register("test_double")
def node_test_double(state: WorkflowState) -> WorkflowState:
    return state.with_updates({"value": state.data.get("value", 1) * 2})


@node_registry.register("test_fail")
def node_test_fail(state: WorkflowState) -> WorkflowState:
    raise RuntimeError("intentional failure")


# Helpers ------------------------------------------------------------------


async def run_graph(node_names, edges, start_node=None):
    graph = graph_manager.create_graph(
        node_names=node_names,
        edges_raw=edges,
        start_node=start_node or node_names[0],
        graph_id=str(uuid4()),
    )
    result = await execution_engine.run_graph(graph.id, initial_state={})
    return result


# Tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_execution():
    result = await run_graph(
        node_names=["test_increment", "test_double"],
        edges={
            "test_increment": ["test_double"],
            "test_double": [None],
        },
        start_node="test_increment",
    )
    assert result["status"] == "completed"
    assert result["state"].data["value"] == 2
    assert len(result["log"]) == 2
    assert result["log"][0].node == "test_increment"


@pytest.mark.asyncio
async def test_conditional_branching():
    result = await run_graph(
        node_names=["test_increment", "test_double"],
        edges={
            "test_increment": [
                {
                    "target": "test_double",
                    "condition": "state.data.get('value', 0) >= 1",
                },
            ],
            "test_double": [None],
        },
        start_node="test_increment",
    )
    assert result["status"] == "completed"
    assert result["state"].data["value"] == 2
    assert result["log"][1].node == "test_double"


@pytest.mark.asyncio
async def test_looping_until_condition():
    result = await run_graph(
        node_names=["test_increment"],
        edges={
            "test_increment": [
                {
                    "target": "test_increment",
                    "condition": "state.data.get('value', 0) < 3",
                }
            ]
        },
        start_node="test_increment",
    )
    assert result["status"] in ("completed", "halted_max_steps")
    assert result["state"].data["value"] >= 3
    assert len(result["log"]) >= 3


@pytest.mark.asyncio
async def test_error_capture():
    result = await run_graph(
        node_names=["test_fail"],
        edges={"test_fail": [None]},
        start_node="test_fail",
    )
    assert result["status"] == "failed"
    assert result["state"].errors
    assert result["log"][0].success is False
    assert "intentional failure" in result["log"][0].error


@pytest.mark.asyncio
async def test_state_diff_logging():
    result = await run_graph(
        node_names=["test_increment"],
        edges={"test_increment": [None]},
        start_node="test_increment",
    )
    log = result["log"][0]
    diff = log.state_diff
    assert "value" in diff["added"] or "value" in diff["updated"]
    assert log.timestamp is not None
    assert log.duration_ms >= 0
