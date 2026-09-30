"""A project-wide voxel lattice, independent of any individual mesh bounds."""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np


def make_shared_grid(origin: Sequence[Any], spacing: Sequence[Any]) -> dict[str, list[float]]:
    if len(origin) != 3 or len(spacing) != 3:
        raise ValueError("Shared grid needs three origin and three voxel-size values.")
    origin_values = [float(value) for value in origin]
    spacing_values = [float(value) for value in spacing]
    if not all(math.isfinite(value) for value in origin_values + spacing_values):
        raise ValueError("Shared grid values must be finite.")
    if any(value <= 0 for value in spacing_values):
        raise ValueError("Shared grid voxel sizes must be positive.")
    return {"origin": origin_values, "spacing": spacing_values}


def snap_bounds(bounds: Sequence[float], grid: dict[str, Any]) -> tuple[list[float], list[int]]:
    """Expand bounds to cell edges on the shared lattice, including negative coordinates."""
    origin = grid["origin"]
    spacing = grid["spacing"]
    extent: list[float] = []
    resolution: list[int] = []
    for axis in range(3):
        lo, hi = float(bounds[axis * 2]), float(bounds[axis * 2 + 1])
        step, anchor = float(spacing[axis]), float(origin[axis])
        if not math.isfinite(lo) or not math.isfinite(hi) or hi < lo:
            raise ValueError("Shared grid received invalid model bounds.")
        qlo, qhi = (lo - anchor) / step, (hi - anchor) / step
        # Avoid adding a cell when an input edge differs from a lattice edge
        # only by floating-point roundoff.
        if abs(qlo - round(qlo)) < 1e-9:
            qlo = float(round(qlo))
        if abs(qhi - round(qhi)) < 1e-9:
            qhi = float(round(qhi))
        first = math.floor(qlo)
        last = max(math.ceil(qhi), first + 1)
        extent.extend([anchor + first * step, anchor + last * step])
        resolution.append(last - first)
    return extent, resolution


def centers_are_aligned(centers: Any, grid: dict[str, Any], tolerance: float = 1e-6) -> bool:
    values = np.asarray(centers, dtype=float).reshape((-1, 3))
    if values.size == 0:
        return True
    origin = np.asarray(grid["origin"], dtype=float)
    spacing = np.asarray(grid["spacing"], dtype=float)
    indices = (values - origin) / spacing - 0.5
    return bool(np.all(np.isfinite(indices)) and np.all(np.abs(indices - np.rint(indices)) <= tolerance))


def voxel_cells_are_aligned(mesh: Any, grid: dict[str, Any], tolerance: float = 1e-6) -> bool:
    """Check every cell's size and corners without VTK per-cell calls."""
    try:
        types = np.asarray(mesh.celltypes)
        connectivity = np.asarray(mesh.cells)
        points = np.asarray(mesh.points, dtype=float)
        count = int(mesh.n_cells)
    except (AttributeError, TypeError, ValueError):
        return False
    if count == 0:
        return True
    # VTK_VOXEL and VTK_HEXAHEDRON both have eight axis-aligned corners here.
    if types.size != count or not np.all(np.isin(types, (11, 12))):
        return False
    if connectivity.size != count * 9:
        return False
    cells = connectivity.reshape((-1, 9))
    if not np.all(cells[:, 0] == 8):
        return False
    origin = np.asarray(grid["origin"], dtype=float)
    spacing = np.asarray(grid["spacing"], dtype=float)
    for start in range(0, count, 20_000):
        ids = cells[start:start + 20_000, 1:]
        if np.any(ids < 0) or np.any(ids >= len(points)):
            return False
        corners = points[ids]
        low = np.min(corners, axis=1)
        high = np.max(corners, axis=1)
        if not np.all(np.isfinite(low)) or not np.all(np.isfinite(high)):
            return False
        if not np.all(np.abs((high - low) / spacing - 1) <= tolerance):
            return False
        corner_positions = (corners - low[:, None, :]) / spacing
        if not np.all(np.minimum(np.abs(corner_positions), np.abs(corner_positions - 1)) <= tolerance):
            return False
        indices = (low - origin) / spacing
        if not np.all(np.abs(indices - np.rint(indices)) <= tolerance):
            return False
    return True
