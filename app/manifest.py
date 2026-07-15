from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return sha256_text(text)[:length]


def graph_fingerprint(nodes: Iterable[Any], edges: Iterable[Any]) -> str:
    payload = {
        "nodes": [
            {
                "id": getattr(n, "id", ""),
                "type": getattr(n, "type", ""),
                "params": getattr(n, "params", {}),
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": getattr(e, "id", ""),
                "from_node": getattr(e, "from_node", ""),
                "from_port": getattr(e, "from_port", ""),
                "to_node": getattr(e, "to_node", ""),
                "to_port": getattr(e, "to_port", ""),
            }
            for e in edges
        ],
    }
    return sha256_text(stable_json_dumps(payload))


def compact_node_summary(node: Any) -> Dict[str, Any]:
    return {
        "id": getattr(node, "id", ""),
        "type": getattr(node, "type", ""),
        "params": getattr(node, "params", {}),
    }
