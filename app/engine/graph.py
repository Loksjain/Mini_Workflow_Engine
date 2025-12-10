from __future__ import annotations

"""Graph definitions and manager."""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Union
from uuid import uuid4

from .node import NodeDefinition
from .registry import node_registry

logger = logging.getLogger(__name__)


@dataclass
class EdgeDefinition:
    target: Optional[str]
    condition: Optional[str] = None
    description: Optional[str] = None


@dataclass
class Graph:
    id: str
    nodes: Dict[str, NodeDefinition]
    edges: Dict[str, List[EdgeDefinition]]
    start_node: str
    description: Optional[str] = None


class GraphManager:
    """In-memory registry for graphs."""

    def __init__(self) -> None:
        self._graphs: Dict[str, Graph] = {}

    def register_graph(self, graph: Graph) -> Graph:
        self._graphs[graph.id] = graph
        logger.info("Registered graph '%s' with %d nodes", graph.id, len(graph.nodes))
        return graph

    def create_graph(
        self,
        node_names: List[str],
        edges_raw: Dict[str, Union[str, List[Union[str, Dict[str, str]]]]],
        start_node: Optional[str] = None,
        graph_id: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Graph:
        edges = self._normalize_edges(edges_raw)
        nodes: Dict[str, NodeDefinition] = {}
        for name in node_names:
            nodes[name] = node_registry.get(name)
        start = start_node or (node_names[0] if node_names else "")
        graph = Graph(
            id=graph_id or str(uuid4()),
            nodes=nodes,
            edges=edges,
            start_node=start,
            description=description,
        )
        return self.register_graph(graph)

    def get_graph(self, graph_id: str) -> Graph:
        if graph_id not in self._graphs:
            raise KeyError(f"Graph '{graph_id}' not found")
        return self._graphs[graph_id]

    def all_graphs(self) -> Dict[str, Graph]:
        return dict(self._graphs)

    @staticmethod
    def _normalize_edges(
        edges_raw: Dict[str, Union[str, List[Union[str, Dict[str, str]]]]]
    ) -> Dict[str, List[EdgeDefinition]]:
        normalized: Dict[str, List[EdgeDefinition]] = {}
        for origin, raw_value in edges_raw.items():
            targets: List[EdgeDefinition] = []
            if isinstance(raw_value, str) or raw_value is None:
                targets.append(EdgeDefinition(target=raw_value, condition=None))
            elif isinstance(raw_value, dict):
                targets.append(
                    EdgeDefinition(
                        target=raw_value.get("target"),
                        condition=raw_value.get("condition"),
                        description=raw_value.get("description"),
                    )
                )
            elif isinstance(raw_value, list):
                for entry in raw_value:
                    if isinstance(entry, str) or entry is None:
                        targets.append(EdgeDefinition(target=entry, condition=None))
                    elif isinstance(entry, dict):
                        targets.append(
                            EdgeDefinition(
                                target=entry.get("target"),
                                condition=entry.get("condition"),
                                description=entry.get("description"),
                            )
                        )
                    else:
                        raise ValueError(f"Unsupported edge entry: {entry}")
            else:
                raise ValueError(f"Unsupported edge type for '{origin}': {raw_value}")
            normalized[origin] = targets
        return normalized


graph_manager = GraphManager()
