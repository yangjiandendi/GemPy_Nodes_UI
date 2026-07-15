from __future__ import annotations

import copy
import hashlib
import json
import re
import traceback
import time
import threading
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple

from .models import GraphEdge, GraphNode, GraphRequest, NodeExecutionError, RuntimeValue
from .manifest import compact_node_summary, graph_fingerprint, utc_now_iso
from .node_registry import NODE_REGISTRY, NODE_TYPES
from .runtime_store import put_runtime_object


class GraphExecutor:
    """Execute node graphs and keep an in-memory per-node result cache.

    The editor is used as a local desktop-style workflow tool. Some nodes,
    especially GemPy computation nodes, can be expensive. The cache lets a later
    run reuse unchanged upstream results and execute only newly added or changed
    downstream nodes.
    """

    def __init__(self) -> None:
        self.node_meta = {n["type"]: n for n in NODE_TYPES}
        # (node_id, signature) -> {"outputs": Dict[str, RuntimeValue], "result": descriptor}
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._progress_lock = threading.Lock()
        self._progress: Dict[str, Any] = {
            "state": "idle",
            "run_id": "",
            "current_node_id": "",
            "current_node_title": "",
            "current_index": 0,
            "total": 0,
            "node_statuses": {},
            "message": "",
            "cancel_requested": False,
        }
        self._cancel_requested = False

    def _replace_runtime_token_in_obj(self, obj: Any, old_token: str, new_token: str) -> Any:
        """Recursively replace stale runtime tokens in descriptors/previews."""
        if isinstance(obj, dict):
            return {
                key: self._replace_runtime_token_in_obj(value, old_token, new_token)
                for key, value in obj.items()
            }
        if isinstance(obj, list):
            return [self._replace_runtime_token_in_obj(value, old_token, new_token) for value in obj]
        if isinstance(obj, tuple):
            return tuple(self._replace_runtime_token_in_obj(value, old_token, new_token) for value in obj)
        if isinstance(obj, str):
            out = obj.replace(old_token, new_token) if old_token else obj
            # Also handle a preview URL that contains a token but no runtime_token
            # field was stored in the preview dict.
            out = re.sub(
                r"(/api/pyvista/gempy-(?:model|clipped-layers)/)([0-9a-f]{32})(\b|\?)",
                lambda m: f"{m.group(1)}{new_token}{m.group(3)}",
                out,
            )
            return out
        return obj

    def _refresh_cached_runtime_value(self, rv: RuntimeValue) -> RuntimeValue:
        """Re-register in-memory objects when cached node results are reused.

        Cached RuntimeValue objects can contain preview URLs such as
        /api/pyvista/gempy-model/<runtime_token>. The token is stored only in
        server memory and can disappear after server restart or runtime cleanup.
        When a node result is reused from the executor cache, the node's run()
        method is not called again, so those URLs would keep pointing to stale
        tokens. This helper creates a fresh token and rewrites the descriptor.
        """
        if rv.kind not in {"geo_model"}:
            return rv

        new_token = put_runtime_object(rv.value, kind=rv.kind, name=rv.name)
        old_token = ""
        if isinstance(rv.preview, dict):
            old_token = str(rv.preview.get("runtime_token") or "")
        if not old_token and isinstance(rv.metadata, dict):
            old_token = str(rv.metadata.get("runtime_token") or "")

        refreshed_preview = copy.deepcopy(rv.preview)
        refreshed_metadata = copy.deepcopy(rv.metadata)

        if isinstance(refreshed_preview, dict):
            refreshed_preview["runtime_token"] = new_token
        if isinstance(refreshed_metadata, dict):
            refreshed_metadata["runtime_token"] = new_token

        refreshed_preview = self._replace_runtime_token_in_obj(refreshed_preview, old_token, new_token)
        refreshed_metadata = self._replace_runtime_token_in_obj(refreshed_metadata, old_token, new_token)

        return RuntimeValue(
            kind=rv.kind,
            value=rv.value,
            name=rv.name,
            preview=refreshed_preview,
            metadata=refreshed_metadata,
        )

    def _refresh_cached_outputs(self, outputs: Dict[str, RuntimeValue]) -> Dict[str, RuntimeValue]:
        return {
            name: self._refresh_cached_runtime_value(rv)
            for name, rv in outputs.items()
        }

    def get_progress(self) -> Dict[str, Any]:
        with self._progress_lock:
            return copy.deepcopy(self._progress)

    def request_cancel(self) -> Dict[str, Any]:
        with self._progress_lock:
            self._cancel_requested = True
            self._progress["cancel_requested"] = True
            if self._progress.get("state") in {"running", "starting"}:
                self._progress["state"] = "cancelling"
                self._progress["message"] = "Stop requested. Waiting for the current node to finish."
            elif self._progress.get("state") == "idle":
                self._progress["message"] = "No active run to stop."
            return copy.deepcopy(self._progress)

    def _set_progress(self, **updates: Any) -> None:
        with self._progress_lock:
            self._progress.update(updates)

    def _init_progress(self, run_id: str, order: List[str], node_statuses: Dict[str, Dict[str, Any]]) -> None:
        with self._progress_lock:
            self._cancel_requested = False
            self._progress = {
                "state": "starting",
                "run_id": run_id,
                "current_node_id": "",
                "current_node_title": "",
                "current_index": 0,
                "total": len(order),
                "order": list(order),
                "node_statuses": copy.deepcopy(node_statuses),
                "message": f"Starting run with {len(order)} node(s).",
                "cancel_requested": False,
            }

    def _update_progress_node_status(self, node_id: str, status: str, **extra: Any) -> None:
        with self._progress_lock:
            ns = self._progress.setdefault("node_statuses", {})
            row = dict(ns.get(node_id, {}))
            row["status"] = status
            row.update(extra)
            ns[node_id] = row

    def _is_cancel_requested(self) -> bool:
        with self._progress_lock:
            return bool(self._cancel_requested)

    def clear_cache(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        return count

    def execute(self, graph: GraphRequest) -> Dict[str, Any]:
        run_id = uuid.uuid4().hex[:12]
        started_at = utc_now_iso()
        graph_hash = graph_fingerprint(graph.nodes, graph.edges)
        run_start = time.perf_counter()
        nodes = {node.id: node for node in graph.nodes}
        if len(nodes) != len(graph.nodes):
            raise NodeExecutionError("Duplicate node ids are not allowed.")
        for node in graph.nodes:
            if node.type not in NODE_REGISTRY:
                raise NodeExecutionError(f"Unknown node type: {node.type}")

        order = self._topological_order(graph.nodes, graph.edges)
        runtime: Dict[Tuple[str, str], RuntimeValue] = {}
        results: Dict[str, Any] = {}
        signatures: Dict[str, str] = {}
        context: Dict[str, Any] = {"run_id": run_id, "graph_hash": graph_hash, "started_at": started_at}

        incoming = defaultdict(list)
        for edge in graph.edges:
            incoming[edge.to_node].append(edge)

        executed_order = []
        cached_order = []
        run_log: List[Dict[str, Any]] = []
        node_statuses: Dict[str, Dict[str, Any]] = {
            node.id: {
                "status": "queued",
                "type": node.type,
                "title": self.node_meta[node.type]["title"],
            }
            for node in graph.nodes
        }

        self._init_progress(run_id, order, node_statuses)

        for progress_index, node_id in enumerate(order, start=1):
            if self._is_cancel_requested():
                self._set_progress(
                    state="cancelled",
                    current_node_id="",
                    current_node_title="",
                    current_index=progress_index - 1,
                    message="Run cancelled before the next node started.",
                    cancel_requested=True,
                )
                finished_at = utc_now_iso()
                manifest = self._manifest(
                    run_id=run_id,
                    graph=graph,
                    graph_hash=graph_hash,
                    started_at=started_at,
                    finished_at=finished_at,
                    ok=False,
                    order=order,
                    executed_order=executed_order,
                    cached_order=cached_order,
                    run_log=run_log,
                    node_statuses=node_statuses,
                    failed_node_id=None,
                    duration_ms=round((time.perf_counter() - run_start) * 1000.0, 2),
                )
                return {
                    "ok": False,
                    "cancelled": True,
                    "run_id": run_id,
                    "graph_hash": graph_hash,
                    "results": results,
                    "order": order,
                    "executed_order": executed_order,
                    "cached_order": cached_order,
                    "run_log": run_log,
                    "node_statuses": node_statuses,
                    "manifest": manifest,
                    "error": "Run cancelled by user.",
                }
            node = nodes[node_id]
            node_impl = NODE_REGISTRY[node.type]
            input_map: Dict[str, Any] = {}
            input_fingerprints = []
            for input_order, edge in enumerate(incoming[node_id]):
                key = (edge.from_node, edge.from_port)
                if key not in runtime:
                    raise NodeExecutionError(f"Edge {edge.id} references missing output {edge.from_node}.{edge.from_port}")
                spec = self._input_spec(node.type, edge.to_port)
                if spec and spec.get("multiple"):
                    input_map.setdefault(edge.to_port, []).append(runtime[key])
                else:
                    input_map[edge.to_port] = runtime[key]
                input_fingerprints.append({
                    "input_order": input_order,
                    "edge_id": edge.id,
                    "from_node": edge.from_node,
                    "from_port": edge.from_port,
                    "to_port": edge.to_port,
                    "from_signature": signatures.get(edge.from_node, ""),
                })

            signature = self._node_signature(node, input_fingerprints)
            signatures[node_id] = signature
            cache_key = (node_id, signature)
            cached = self._cache.get(cache_key)
            node_start = time.perf_counter()
            self._set_progress(
                state="running",
                current_node_id=node.id,
                current_node_title=self.node_meta[node.type]["title"],
                current_index=progress_index,
                total=len(order),
                message=f"Running {self.node_meta[node.type]['title']} ({progress_index}/{len(order)})",
            )
            node_statuses[node.id].update({
                "status": "running",
                "cached": False,
                "signature": signature,
            })
            self._update_progress_node_status(
                node.id,
                "running",
                cached=False,
                signature=signature,
                title=self.node_meta[node.type]["title"],
                type=node.type,
            )

            if cached is not None:
                outputs: Dict[str, RuntimeValue] = self._refresh_cached_outputs(cached["outputs"])
                for port_name, rv in outputs.items():
                    runtime[(node.id, port_name)] = rv
                res = copy.deepcopy(cached["result"])
                res["outputs"] = {name: rv.descriptor() for name, rv in outputs.items()}
                res["cached"] = True
                res["duration_ms"] = 0.0
                results[node.id] = res
                cached_order.append(node.id)
                node_statuses[node.id].update({
                    "status": "cached",
                    "cached": True,
                    "duration_ms": 0.0,
                    "signature": signature,
                })
                self._update_progress_node_status(
                    node.id,
                    "cached",
                    cached=True,
                    duration_ms=0.0,
                    signature=signature,
                    title=self.node_meta[node.type]["title"],
                    type=node.type,
                )
                run_log.append({
                    "node_id": node.id,
                    "title": self.node_meta[node.type]["title"],
                    "type": node.type,
                    "status": "cached",
                    "duration_ms": 0.0,
                    "signature": signature,
                })
                continue

            try:
                node_statuses[node.id]["status"] = "running"
                outputs = node_impl.run(input_map, node.params, context)
                duration_ms = round((time.perf_counter() - node_start) * 1000.0, 2)
                for port_name, rv in outputs.items():
                    runtime[(node.id, port_name)] = rv
                executed_order.append(node.id)
                output_descriptors = {name: rv.descriptor() for name, rv in outputs.items()}
                result = {
                    "ok": True,
                    "cached": False,
                    "duration_ms": duration_ms,
                    "signature": signature,
                    "type": node.type,
                    "title": self.node_meta[node.type]["title"],
                    "outputs": output_descriptors,
                }
                results[node.id] = result
                self._cache[cache_key] = {"outputs": outputs, "result": copy.deepcopy(result)}
                node_statuses[node.id].update({
                    "status": "done",
                    "cached": False,
                    "duration_ms": duration_ms,
                    "signature": signature,
                })
                self._update_progress_node_status(
                    node.id,
                    "done",
                    cached=False,
                    duration_ms=duration_ms,
                    signature=signature,
                    title=self.node_meta[node.type]["title"],
                    type=node.type,
                )
                run_log.append({
                    "node_id": node.id,
                    "title": self.node_meta[node.type]["title"],
                    "type": node.type,
                    "status": "done",
                    "duration_ms": duration_ms,
                    "signature": signature,
                })
            except Exception as exc:
                duration_ms = round((time.perf_counter() - node_start) * 1000.0, 2)
                executed_order.append(node.id)
                results[node.id] = {
                    "ok": False,
                    "cached": False,
                    "duration_ms": duration_ms,
                    "signature": signature,
                    "type": node.type,
                    "title": self.node_meta[node.type]["title"],
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
                node_statuses[node.id].update({
                    "status": "failed",
                    "cached": False,
                    "duration_ms": duration_ms,
                    "signature": signature,
                    "error": str(exc),
                })
                self._update_progress_node_status(
                    node.id,
                    "failed",
                    cached=False,
                    duration_ms=duration_ms,
                    signature=signature,
                    error=str(exc),
                    title=self.node_meta[node.type]["title"],
                    type=node.type,
                )
                self._set_progress(
                    state="failed",
                    current_node_id=node.id,
                    current_node_title=self.node_meta[node.type]["title"],
                    current_index=progress_index,
                    message=f"Failed at {self.node_meta[node.type]['title']}: {exc}",
                )
                run_log.append({
                    "node_id": node.id,
                    "title": self.node_meta[node.type]["title"],
                    "type": node.type,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "signature": signature,
                    "error": str(exc),
                })
                finished_at = utc_now_iso()
                manifest = self._manifest(
                    run_id=run_id,
                    graph=graph,
                    graph_hash=graph_hash,
                    started_at=started_at,
                    finished_at=finished_at,
                    ok=False,
                    order=order,
                    executed_order=executed_order,
                    cached_order=cached_order,
                    run_log=run_log,
                    node_statuses=node_statuses,
                    failed_node_id=node.id,
                    duration_ms=round((time.perf_counter() - run_start) * 1000.0, 2),
                )
                return {
                    "ok": False,
                    "run_id": run_id,
                    "graph_hash": graph_hash,
                    "failed_node_id": node.id,
                    "results": results,
                    "order": order,
                    "executed_order": executed_order,
                    "cached_order": cached_order,
                    "run_log": run_log,
                    "node_statuses": node_statuses,
                    "manifest": manifest,
                }

        finished_at = utc_now_iso()
        manifest = self._manifest(
            run_id=run_id,
            graph=graph,
            graph_hash=graph_hash,
            started_at=started_at,
            finished_at=finished_at,
            ok=True,
            order=order,
            executed_order=executed_order,
            cached_order=cached_order,
            run_log=run_log,
            node_statuses=node_statuses,
            failed_node_id=None,
            duration_ms=round((time.perf_counter() - run_start) * 1000.0, 2),
        )
        self._set_progress(
            state="done",
            current_node_id="",
            current_node_title="",
            current_index=len(order),
            total=len(order),
            message=f"Run finished: {len(order)} node(s).",
            cancel_requested=False,
        )
        return {
            "ok": True,
            "run_id": run_id,
            "graph_hash": graph_hash,
            "results": results,
            "order": order,
            "executed_order": executed_order,
            "cached_order": cached_order,
            "run_log": run_log,
            "node_statuses": node_statuses,
            "manifest": manifest,
        }

    def _manifest(
        self,
        *,
        run_id: str,
        graph: GraphRequest,
        graph_hash: str,
        started_at: str,
        finished_at: str,
        ok: bool,
        order: List[str],
        executed_order: List[str],
        cached_order: List[str],
        run_log: List[Dict[str, Any]],
        node_statuses: Dict[str, Dict[str, Any]],
        failed_node_id: str | None,
        duration_ms: float,
    ) -> Dict[str, Any]:
        return {
            "schema_version": "v18-run-manifest-1",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "ok": ok,
            "failed_node_id": failed_node_id,
            "graph_hash": graph_hash,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "order": order,
            "executed_order": executed_order,
            "cached_order": cached_order,
            "nodes": [compact_node_summary(n) for n in graph.nodes],
            "edges": [e.model_dump() for e in graph.edges],
            "node_statuses": node_statuses,
            "run_log": run_log,
        }

    def _node_signature(self, node: GraphNode, input_fingerprints: List[Dict[str, Any]]) -> str:
        payload = {
            "node_id": node.id,
            "type": node.type,
            "params": self._normalize_for_json(node.params),
            # Keep the real incoming-edge order.  This is critical for nodes with
            # multiple inputs such as MergeTables: changing the concat order must
            # invalidate the cache, otherwise an old merged table can be reused.
            "inputs": input_fingerprints,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _normalize_for_json(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self._normalize_for_json(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, list):
            return [self._normalize_for_json(v) for v in value]
        return value

    def _input_spec(self, node_type: str, port_name: str) -> Dict[str, Any] | None:
        meta = self.node_meta.get(node_type, {})
        for inp in meta.get("inputs", []):
            if inp["name"] == port_name:
                return inp
        return None

    def _topological_order(self, nodes: List[GraphNode], edges: List[GraphEdge]) -> List[str]:
        node_ids = {n.id for n in nodes}
        indeg = {n.id: 0 for n in nodes}
        adj = defaultdict(list)
        for edge in edges:
            if edge.from_node not in node_ids or edge.to_node not in node_ids:
                raise NodeExecutionError(f"Edge {edge.id} references a missing node.")
            adj[edge.from_node].append(edge.to_node)
            indeg[edge.to_node] += 1
        q = deque([n.id for n in nodes if indeg[n.id] == 0])
        order = []
        while q:
            cur = q.popleft()
            order.append(cur)
            for nb in adj[cur]:
                indeg[nb] -= 1
                if indeg[nb] == 0:
                    q.append(nb)
        if len(order) != len(nodes):
            raise NodeExecutionError("Graph contains a cycle. GemPy workflows must be a directed acyclic graph.")
        return order
