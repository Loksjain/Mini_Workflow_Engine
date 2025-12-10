from __future__ import annotations

"""FastAPI application entrypoint."""

import logging

from fastapi import FastAPI

from app.api import router
from app.workflows.codereview import register_workflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Mini Workflow Engine",
    description="Simplified LangGraph-style engine with stateful nodes, loops, and FastAPI endpoints.",
    version="1.0.0",
)
app.include_router(router)


@app.on_event("startup")
async def startup_event() -> None:
    register_workflow()


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
