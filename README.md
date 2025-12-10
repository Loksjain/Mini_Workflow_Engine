# Mini Workflow Engine (LangGraph Style)

A complete FastAPI project that implements a minimal workflow/graph engine with shared state, nodes, edges, conditional branching, looping, tool registry, execution logs, and an end-to-end code-review example workflow.

## Features

- Graph execution with nodes as Python functions, automatic node registration, shared state propagation, edges for linear/branching/loop flows, and terminal node detection.
- Conditional routing and loop support via edge conditions evaluated against the live state.
- Tool registry for reusable callables; nodes can invoke tools directly.
- Execution engine with run_id/graph_id auto-generation, node-level try/except isolation, optional async nodes, optional per-node timeouts, and per-step state diffs.
- Detailed execution log entries with timestamps, durations, node names, success flags, errors, and state diffs; state/error tracking stored in-memory.
- FastAPI endpoints: `POST /graph/create`, `POST /graph/run`, `GET /graph/state/{run_id}`, WebSocket `ws://.../graph/stream/{run_id}`, plus `GET /health`.
- Example code-review workflow (extract -> complexity -> issues -> suggestions -> quality gate loop -> finalize) showing tools, conditionals, and loops.

## Project Structure

```
app/
  engine/
    graph.py
    node.py
    state.py
    executor.py
    registry.py
  workflows/
    codereview.py
  api/
    routes.py
  main.py
README.md
```

## How to Run

1) Install dependencies:
```bash
pip install fastapi uvicorn pydantic
```

2) Start the API:
```bash
uvicorn app.main:app --reload
```

3) Open docs at `http://localhost:8000/docs`.

### Stream Logs Over WebSocket
```
ws://localhost:8000/graph/stream/<run_id>
```
Connect after starting a run to receive log events (`type=log`) and status updates (`type=status`) as they are produced.

## Example Workflow (Code Review Agent)

1) The workflow registers automatically on startup with id `code_review_agent`.
2) Execute it:
```bash
curl -X POST http://localhost:8000/graph/run \
  -H "Content-Type: application/json" \
  -d '{
        "graph_id": "code_review_agent",
        "initial_state": {
          "code": "def example(x):\n    # TODO optimize\n    return x * 2",
          "quality_threshold": 0.75,
          "max_iterations": 3
        }
      }'
```
3) Response returns `run_id`, `final_state`, `execution_log`, and `status`. The log includes timestamps, state diffs per node, and any errors.

## Engine Behavior

- Nodes receive and return `WorkflowState`; updates are merged into `state.data`.
- Edges are evaluated in order. The first condition that evaluates to true is taken; otherwise the last unconditional edge is used. Missing edges end execution.
- Loops are achieved by pointing an edge back to an earlier node. `quality_gate` in the example loops until the score crosses the threshold or max iterations are reached.
- Timeouts per node are supported via the `timeout_seconds` decorator argument.

## What Can Be Improved

- Persist graphs, runs, and logs to a database (SQLite/Postgres) instead of in-memory stores.
- Harden condition evaluation with a DSL instead of `eval`.
- Provide OpenAPI schemas for dynamic tool registration and graph listing.
