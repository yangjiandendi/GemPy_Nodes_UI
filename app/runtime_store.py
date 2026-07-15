from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class StoredObject:
    kind: str
    value: Any
    name: str
    created_at: float


_OBJECTS: Dict[str, StoredObject] = {}


def put_runtime_object(value: Any, kind: str, name: str = "") -> str:
    """Store a runtime object in memory and return a short-lived token.

    The node editor is a local desktop-style tool. This cache lets a later UI
    button open a PyVista/GemPy viewer for a model that was produced during the
    most recent FastAPI execution.
    """
    token = uuid.uuid4().hex
    _OBJECTS[token] = StoredObject(kind=kind, value=value, name=name, created_at=time.time())
    return token


def get_runtime_object(token: str, expected_kind: str | None = None) -> StoredObject:
    if token not in _OBJECTS:
        raise KeyError(f"Runtime object token not found: {token}. Re-run the node chain and try again.")
    obj = _OBJECTS[token]
    if expected_kind is not None and obj.kind != expected_kind:
        raise TypeError(f"Runtime object {token} has kind {obj.kind}, expected {expected_kind}.")
    return obj


def cleanup_runtime_objects(max_age_seconds: int = 3600) -> None:
    now = time.time()
    for token, obj in list(_OBJECTS.items()):
        if now - obj.created_at > max_age_seconds:
            _OBJECTS.pop(token, None)
