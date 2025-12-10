import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.engine.registry import node_registry
from app.engine.state import WorkflowState

client = TestClient(app)


@node_registry.register("api_echo")
def api_echo(state: WorkflowState) -> WorkflowState:
    return state.with_updates({"echo": state.data.get("echo", "hello")})


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_graph_create_and_run():
    # Create a simple graph through the API
    payload = {
        "nodes": ["api_echo"],
        "edges": {"api_echo": [None]},
        "start_node": "api_echo",
    }
    create_resp = client.post("/graph/create", json=payload)
    assert create_resp.status_code == 200
    graph_id = create_resp.json()["graph_id"]

    # Run the graph
    run_payload = {"graph_id": graph_id, "initial_state": {"echo": "world"}}
    run_resp = client.post("/graph/run", json=run_payload)
    assert run_resp.status_code == 200
    data = run_resp.json()
    assert data["status"] in ("running", "completed")
    assert data["run_id"]
    # Poll state to ensure run is tracked
    state_resp = client.get(f"/graph/state/{data['run_id']}")
    assert state_resp.status_code == 200
    state_body = state_resp.json()
    assert state_body["run_id"] == data["run_id"]


def test_get_graph_state():
    run_resp = client.post(
        "/graph/run", json={"graph_id": "code_review_agent", "initial_state": {"code": "def a(): pass"}}
    )
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    state_resp = client.get(f"/graph/state/{run_id}")
    assert state_resp.status_code == 200
    body = state_resp.json()
    assert body["run_id"] == run_id
    assert isinstance(body["execution_log"], list)


def test_run_missing_graph():
    resp = client.post("/graph/run", json={"graph_id": "missing", "initial_state": {}})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
