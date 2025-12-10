from __future__ import annotations

"""Code review mini-agent workflow using the engine."""

import asyncio
import logging
import re
from statistics import mean
from typing import Any, Dict, List

from app.engine.graph import graph_manager
from app.engine.registry import node_registry, tool_registry
from app.engine.state import WorkflowState

logger = logging.getLogger(__name__)


# Tool implementations -----------------------------------------------------


@tool_registry.register("detect_smells")
def detect_smells(code: str) -> Dict[str, Any]:
    """Detect very basic smells in the code string."""
    todos = code.count("TODO") + code.count("FIXME")
    long_lines = sum(1 for line in code.splitlines() if len(line) > 100)
    return {"issues": todos + long_lines, "todos": todos, "long_lines": long_lines}


@tool_registry.register("estimate_complexity")
def estimate_complexity(code: str) -> Dict[str, Any]:
    """Estimate a rough complexity score based on branching keywords."""
    branches = len(re.findall(r"\b(if|for|while|try|except|with)\b", code))
    functions = len(re.findall(r"def\s+[A-Za-z_]\w*", code))
    score = branches * 1.5 + max(functions, 1)
    return {"complexity_score": round(score, 2), "branch_points": branches}


@tool_registry.register("suggest_improvements")
def suggest_improvements(code: str, issues: int, complexity: float) -> List[str]:
    """Generate lightweight improvement suggestions."""
    suggestions: List[str] = []
    if issues:
        suggestions.append("Resolve TODO/FIXME markers and shorten long lines.")
    if complexity > 8:
        suggestions.append("Break large functions into smaller units.")
    if "print(" in code:
        suggestions.append("Replace print statements with structured logging.")
    if not suggestions:
        suggestions.append("Code looks clean; consider adding docstrings and tests.")
    return suggestions


# Nodes --------------------------------------------------------------------


@node_registry.register(
    "extract_functions",
    description="Extract function names and initialize quality defaults.",
)
def extract_functions(state: WorkflowState) -> WorkflowState:
    code = state.data.get("code", "")
    functions = re.findall(r"def\s+([A-Za-z_]\w*)", code)
    defaults = {
        "quality_threshold": state.data.get("quality_threshold", 0.8),
        "max_iterations": state.data.get("max_iterations", 5),
        "iteration": state.data.get("iteration", 0),
    }
    updates = {
        "functions": functions,
        "function_count": len(functions),
        "quality_score": state.data.get("quality_score", 0.3),
        **defaults,
    }
    return state.with_updates(updates)


@node_registry.register(
    "check_complexity",
    description="Compute a simple cyclomatic-like complexity estimate.",
)
def check_complexity(state: WorkflowState) -> WorkflowState:
    code = state.data.get("code", "")
    result = tool_registry.call("estimate_complexity", code)
    quality_delta = max(0.0, 0.2 - (result["complexity_score"] / 50))
    updates = {
        **result,
        "quality_score": min(1.0, state.data.get("quality_score", 0) + quality_delta),
    }
    return state.with_updates(updates)


@node_registry.register(
    "detect_basic_issues",
    description="Detect simple smells and issues.",
)
async def detect_basic_issues(state: WorkflowState) -> WorkflowState:
    code = state.data.get("code", "")
    # Async-friendly even if the underlying work is sync.
    await asyncio.sleep(0)
    result = tool_registry.call("detect_smells", code)
    quality_delta = max(0.0, 0.25 - (result["issues"] * 0.05))
    updates = {
        **result,
        "quality_score": min(1.0, state.data.get("quality_score", 0) + quality_delta),
    }
    return state.with_updates(updates)


@node_registry.register(
    "suggest_improvements",
    description="Generate improvement tips and nudge quality score upward.",
)
def suggest_improvements_node(state: WorkflowState) -> WorkflowState:
    code = state.data.get("code", "")
    issues = state.data.get("issues", 0)
    complexity = state.data.get("complexity_score", 0.0)
    suggestions = tool_registry.call("suggest_improvements", code, issues, complexity)
    quality_delta = min(0.15, max(0.05, 0.25 - issues * 0.03))
    updates = {
        "suggestions": suggestions,
        "quality_score": min(1.0, state.data.get("quality_score", 0) + quality_delta),
        "iteration": state.data.get("iteration", 0) + 1,
    }
    return state.with_updates(updates)


@node_registry.register(
    "quality_gate",
    description="Decide whether to loop or finish based on quality threshold.",
)
def quality_gate(state: WorkflowState) -> WorkflowState:
    threshold = state.data.get("quality_threshold", 0.8)
    iteration = state.data.get("iteration", 0)
    max_iterations = state.data.get("max_iterations", 5)
    score = state.data.get("quality_score", 0)
    passed = score >= threshold or iteration >= max_iterations
    updates = {"quality_passed": passed}
    return state.with_updates(updates)


@node_registry.register(
    "finalize_review",
    description="Finalize output and compute aggregate stats.",
)
def finalize_review(state: WorkflowState) -> WorkflowState:
    suggestions: List[str] = state.data.get("suggestions", [])
    avg_suggestion_length = mean(len(s) for s in suggestions) if suggestions else 0
    updates = {
        "summary": {
            "functions": state.data.get("function_count", 0),
            "issues": state.data.get("issues", 0),
            "quality_score": round(state.data.get("quality_score", 0), 3),
            "iterations": state.data.get("iteration", 0),
        },
        "avg_suggestion_length": avg_suggestion_length,
    }
    return state.with_updates(updates)


# Graph wiring -------------------------------------------------------------


def register_workflow() -> None:
    """Register the Code Review workflow graph."""
    nodes = [
        "extract_functions",
        "check_complexity",
        "detect_basic_issues",
        "suggest_improvements",
        "quality_gate",
        "finalize_review",
    ]
    edges = {
        "extract_functions": ["check_complexity"],
        "check_complexity": ["detect_basic_issues"],
        "detect_basic_issues": ["suggest_improvements"],
        "suggest_improvements": ["quality_gate"],
        "quality_gate": [
            {
                "target": "finalize_review",
                "condition": "state.data.get('quality_passed') is True",
            },
            {
                "target": "detect_basic_issues",
                "description": "Loop until the score clears the threshold",
            },
        ],
        "finalize_review": [],
    }
    graph_manager.create_graph(
        node_names=nodes,
        edges_raw=edges,
        start_node="extract_functions",
        graph_id="code_review_agent",
        description="Minimal code review agent with looping quality gate.",
    )
    logger.info("Code review workflow registered as 'code_review_agent'")
