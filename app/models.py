from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    id: str
    type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    position: Dict[str, float] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    from_node: str
    from_port: str
    to_node: str
    to_port: str


class GraphRequest(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge] = Field(default_factory=list)


@dataclass
class RuntimeValue:
    kind: str
    value: Any
    name: str = ""
    preview: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def descriptor(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "preview": self.preview,
            "metadata": self.metadata,
        }


class NodeExecutionError(Exception):
    pass
