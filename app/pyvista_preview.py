from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a PyVista 3D preview for a geological table.")
    parser.add_argument("csv_path")
    parser.add_argument("--x", default="X")
    parser.add_argument("--y", default="Y")
    parser.add_argument("--z", default="Z")
    parser.add_argument("--formation", default="formation")
    parser.add_argument("--max-points", type=int, default=8000)
    args = parser.parse_args()

    try:
        import pyvista as pv
    except Exception as exc:
        print(f"PyVista is not installed/importable: {exc}", file=sys.stderr)
        return 2

    path = Path(args.csv_path)
    df = pd.read_csv(path)
    required = [args.x, args.y, args.z]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"Missing coordinate columns: {missing}", file=sys.stderr)
        return 3

    coords = df[required].apply(pd.to_numeric, errors="coerce")
    mask = ~coords.isna().any(axis=1)
    df = df.loc[mask].copy()
    coords = coords.loc[mask]
    if df.empty:
        print("No valid XYZ rows to display.", file=sys.stderr)
        return 4

    if len(df) > args.max_points:
        df = df.sample(n=args.max_points, random_state=42)
        coords = coords.loc[df.index]

    points = coords.to_numpy(float)
    cloud = pv.PolyData(points)

    scalars_name = None
    labels = None
    if args.formation in df.columns:
        labels = df[args.formation].astype(str).fillna("<NA>")
        cats = sorted(labels.unique().tolist())
        lookup = {name: i for i, name in enumerate(cats)}
        cloud.point_data["formation_id"] = labels.map(lookup).to_numpy(np.int32)
        scalars_name = "formation_id"

    plotter = pv.Plotter(title=f"PyVista Table Preview - {path.name}")
    if scalars_name:
        plotter.add_mesh(
            cloud,
            scalars=scalars_name,
            render_points_as_spheres=True,
            point_size=9,
            show_scalar_bar=True,
            scalar_bar_args={"title": "formation id"},
        )
        text = "Formation IDs:\n" + "\n".join(f"{i}: {name}" for name, i in lookup.items())
        plotter.add_text(text, position="upper_left", font_size=8)
    else:
        plotter.add_mesh(cloud, render_points_as_spheres=True, point_size=9)

    grad_cols = ["G_x", "G_y", "G_z"]
    if all(c in df.columns for c in grad_cols):
        vectors = df[grad_cols].apply(pd.to_numeric, errors="coerce").fillna(0).to_numpy(float)
        if np.linalg.norm(vectors, axis=1).max(initial=0) > 0:
            cloud.point_data["orientation"] = vectors
            ranges = points.max(axis=0) - points.min(axis=0)
            diag = float(np.linalg.norm(ranges)) or 1.0
            factor = diag * 0.025
            try:
                arrows = cloud.glyph(orient="orientation", scale=False, factor=factor)
                plotter.add_mesh(arrows)
            except Exception as exc:
                print(f"Could not add orientation glyphs: {exc}", file=sys.stderr)

    plotter.add_axes()
    try:
        plotter.show_grid()
    except Exception:
        pass
    plotter.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
