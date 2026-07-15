#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to popup JSON config")
    args = parser.parse_args()

    import pyvista as pv

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    p = pv.Plotter(
        notebook=False,
        title=cfg.get("title", "PyVista Viewer"),
        window_size=cfg.get("window_size", [1200, 850]),
    )

    for item in cfg.get("meshes", []):
        path = item.get("path")
        if not path:
            continue
        mesh = pv.read(path)
        scalars = item.get("scalars") or None
        color = item.get("color") or None
        kwargs = {
            "opacity": item.get("opacity", 1.0),
            "show_edges": bool(item.get("show_edges", False)),
        }
        if scalars and scalars in mesh.cell_data:
            kwargs["scalars"] = scalars
        elif scalars and scalars in mesh.point_data:
            kwargs["scalars"] = scalars
        elif color:
            kwargs["color"] = color
        p.add_mesh(mesh, **kwargs)

    if cfg.get("show_axes", True):
        p.show_axes()
    if cfg.get("show_grid", False):
        p.show_grid()

    p.show()


if __name__ == "__main__":
    main()
