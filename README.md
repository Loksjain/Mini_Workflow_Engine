# Mini Workflow Engine (LangGraph Style)

A FastAPI project that implements a minimal workflow/graph engine with shared state, nodes, edges, conditional branching, looping, tool registry, execution logs, and an end-to-end code-review example workflow — enhanced with background execution, optional SQLite persistence, and safe condition parsing.

## Features
### Workflow / Graph Engine
- Nodes as Python functions with automatic registration
- Shared state propagation between nodes
- Edge-based flow control: linear, branching, conditional, loops
- Terminal node detection when no outgoing edges

### Conditional Routing & Loop Support
- Conditions evaluated against live state
- Safe AST parsing (no raw `eval`) prevents unsafe expressions
- Looping via back-edges (e.g., `quality_gate` -> `detect_basic_issues`)

### Tool Registry
- Reusable Python callables registered as tools
- Nodes invoke tools directly through the registry

### Execution Engine
- Auto-generated `run_id` and `graph_id`
- Node-level try/except isolation
- Optional async node support
- Optional per-node timeouts
- Per-step state diffs to track incremental state changes

### Detailed Execution Logs
- Timestamp per node execution and duration in milliseconds
- Node name, success flag, failure reason
- Incremental state diffs
- Stored in memory and optionally persisted to SQLite

### FastAPI Endpoints
- `POST /graph/create` — create/define a workflow graph
- `POST /graph/run` — start a workflow run (runs in background)
- `GET /graph/state/{run_id}` — inspect state + logs
- `GET /graphs` — list graphs
- `GET /graphs/{graph_id}` — graph detail
- `GET /runs` — list workflow runs
- `GET /runs/{run_id}` — run detail
- `WS /graph/stream/{run_id}` — live log streaming
- `GET /health` — health check

### Example Workflow: Code Review Agent
Extract -> analyze -> detect issues -> suggest -> loop until quality threshold -> finalize.

## Newly Added Enhancements
1) Background Execution  
   - `POST /graph/run` starts the workflow in a background asyncio task  
   - Returns immediately with `{ run_id, status: "running" }`  
   - Progress via API or WebSocket

2) SQLite Persistence (Optional)  
   - Stored in `app/workflow.db`: graph definitions, workflow runs, per-step execution logs, run timestamps and metadata  
   - Falls back to in-memory if DB is unavailable

3) Safer Condition Evaluation  
   - Replaces `eval()` with a mini DSL using AST whitelisting  
   - Only safe constructs allowed (Compare, BoolOp, Name, Subscript, Call for `get`, etc.)  
   - Invalid conditions are treated as `False`

4) Richer Run Metadata  
   - Enum-based `RunStatus` (running, completed, failed, timeout)  
   - `error_type` field in step logs  
   - Start and finish timestamps for every run  
   - Per-node timing and structured diffs

## Project Structure
```
app/
  engine/
    graph.py         # Graph structure & edge definitions
    node.py          # Node definition helper
    state.py         # WorkflowState & diff utilities
    executor.py      # Background runner, logs, statuses
    registry.py      # Registries for nodes/tools
    persistence.py   # SQLite adapter (optional)
  workflows/
    codereview.py    # Code review example workflow
  api/
    routes.py        # REST + WebSocket endpoints
  main.py            # FastAPI bootstrap
tests/
  test_api.py
  test_engine.py
README.md
```

## How to Run
Install dependencies:
```bash
pip install -r requirements.txt
```

Start the API:
```bash
uvicorn app.main:app --reload
```
Open docs: http://localhost:8000/docs

Run tests:
```bash
pytest
```

SQLite DB is created automatically at `app/workflow.db`. Delete it any time for a clean slate.

## Stream Logs Over WebSocket
```
ws://localhost:8000/graph/stream/<run_id>
```
You will receive:
- `{"type": "log", ...}` — step-by-step logs
- `{"type": "status", ...}` — status transitions with state

## Example Workflow (Code Review Agent)
The workflow auto-registers on startup with:
```
graph_id = "code_review_agent"
```

Execute it:
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

Response includes:
- `run_id`
- current `status`
- `final_state` (if completed)
- complete execution log

## Engine Behavior
- Nodes return either a dict or `WorkflowState`.
- State updates merge into `state.data`.
- Edges evaluated top-to-bottom; first passing condition wins; last unconditional edge is fallback.
- No outgoing edges = end of workflow.
- Loops occur naturally via back-edges.
- Timeout support via `timeout_seconds` decorator.
