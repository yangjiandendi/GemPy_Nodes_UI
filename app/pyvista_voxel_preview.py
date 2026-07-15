from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyvista as pv


def parse_json_list(value: str | None):
    if not value:
        return None
    try:
        out = json.loads(value)
    except Exception:
        return None
    return out if isinstance(out, list) else None


def infer_cube_shape(n: int):
    r = round(n ** (1 / 3))
    if r ** 3 == n:
        return [r, r, r]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a voxel model from a NumPy array in PyVista.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--reshape", default="")
    parser.add_argument("--extent", default="")
    parser.add_argument("--scalars", default="values")
    parser.add_argument("--show-edges", action="store_true")
    parser.add_argument("--threshold-background", action="store_true")
    args = parser.parse_args()

    arr = np.load(args.path)
    shape = parse_json_list(args.reshape)
    if shape:
        arr = arr.reshape(tuple(int(x) for x in shape))
    elif arr.ndim == 1:
        cube = infer_cube_shape(arr.size)
        if cube:
            arr = arr.reshape(tuple(cube))

    if arr.ndim != 3:
        raise SystemExit(f"Voxel preview needs a 3D array. Got shape {arr.shape}.")

    nx, ny, nz = arr.shape
    extent = parse_json_list(args.extent)
    if extent and len(extent) == 6:
        x_min, x_max, y_min, y_max, z_min, z_max = [float(v) for v in extent]
        spacing = ((x_max - x_min) / nx, (y_max - y_min) / ny, (z_max - z_min) / nz)
        origin = (x_min, y_min, z_min)
    else:
        spacing = (1.0, 1.0, 1.0)
        origin = (0.0, 0.0, 0.0)

    # Store the lithology/voxel values as cell data. ImageData dimensions are
    # point dimensions, therefore cell dimensions are one less in each direction.
    grid = pv.ImageData(dimensions=(nx + 1, ny + 1, nz + 1), spacing=spacing, origin=origin)
    vals = np.asarray(arr).ravel(order="F")
    grid.cell_data[args.scalars] = vals

    mesh = grid
    if args.threshold_background:
        finite = vals[np.isfinite(vals)]
        if finite.size:
            min_val = float(np.nanmin(finite))
            try:
                mesh = grid.threshold(value=min_val + 1e-12, scalars=args.scalars)
            except Exception:
                mesh = grid

    p = pv.Plotter()
    p.add_mesh(mesh, scalars=args.scalars, show_edges=args.show_edges, opacity=1.0)
    p.add_axes()
    p.show_grid()
    p.add_title(f"Voxel model: {args.path.name} | shape={arr.shape}")
    p.show()


if __name__ == "__main__":
    main()
