from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyvista as pv


def choose_scalar(mesh, requested: str = "") -> str:
    scalar_name = requested.strip()
    if scalar_name and scalar_name not in mesh.cell_data and scalar_name not in mesh.point_data:
        scalar_name = ""
    if not scalar_name:
        # Prefer categorical/material fields over internal ids.
        priority = ["MaterialIDs", "MaterialID", "formation_id", "id", "lithology", "lith_block", "BoundaryID"]
        for key in priority:
            if key in mesh.cell_data or key in mesh.point_data:
                return key
        if len(mesh.cell_data.keys()) > 0:
            return list(mesh.cell_data.keys())[0]
        if len(mesh.point_data.keys()) > 0:
            return list(mesh.point_data.keys())[0]
    return scalar_name


def add_orientation_arrows(plotter, mesh, vector_name: str) -> bool:
    vector_name = (vector_name or "").strip()
    if not vector_name:
        # Auto-detect the vector field used by spatial table previews.
        if "orientation_vector" in mesh.point_data:
            vector_name = "orientation_vector"
        else:
            return False

    if vector_name not in mesh.point_data:
        return False

    vectors = np.asarray(mesh.point_data[vector_name])
    if vectors.ndim != 2 or vectors.shape[1] != 3 or mesh.n_points == 0:
        return False

    lengths = np.linalg.norm(vectors, axis=1)
    finite = lengths[np.isfinite(lengths) & (lengths > 0)]
    if finite.size == 0:
        return False

    # Scale relative to model size so arrows are visible but not enormous.
    bounds = np.asarray(mesh.bounds, dtype=float)
    diag = np.linalg.norm([bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]])
    scale = float(diag / 30.0) if diag > 0 else 1.0
    mean_len = float(np.nanmean(finite))
    factor = scale / mean_len if mean_len > 0 else scale

    try:
        glyphs = mesh.glyph(orient=vector_name, scale=False, factor=factor, geom=pv.Arrow())
    except TypeError:
        glyphs = mesh.glyph(orient=vector_name, factor=factor)

    plotter.add_mesh(glyphs, color="black", opacity=0.85)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a PyVista-readable mesh in a local 3D window.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--scalars", default="")
    parser.add_argument("--show-edges", action="store_true")
    parser.add_argument("--vector", default="", help="Optional point-data vector field to render as orientation arrows.")
    args = parser.parse_args()

    mesh = pv.read(str(args.path))
    scalar_name = choose_scalar(mesh, args.scalars)

    p = pv.Plotter()
    kwargs = {"show_edges": args.show_edges}

    # Make point-cloud previews visible.
    if mesh.n_cells == mesh.n_points:
        kwargs["point_size"] = 10
        kwargs["render_points_as_spheres"] = True

    if scalar_name:
        kwargs["scalars"] = scalar_name

    p.add_mesh(mesh, **kwargs)
    arrows_added = add_orientation_arrows(p, mesh, args.vector)

    p.add_axes()
    p.show_grid()
    arrow_label = " | arrows=orientation_vector" if arrows_added else ""
    p.add_title(f"{args.path.name} | cells={mesh.n_cells} | points={mesh.n_points}{arrow_label}")
    p.show()


if __name__ == "__main__":
    main()
