from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyvista as pv


def main() -> None:
    parser = argparse.ArgumentParser(description="Open selected voxel boundary meshes in a local PyVista 3D window.")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    title = manifest.get("title") or "Voxel boundaries"
    show_edges = bool(manifest.get("show_edges", True))
    items = manifest.get("items", [])

    p = pv.Plotter()
    for item in items:
        path = Path(item["path"])
        if not path.exists():
            continue
        mesh = pv.read(str(path))
        name = item.get("name", path.stem)
        color = item.get("color") or "white"
        p.add_mesh(mesh, color=color, show_edges=show_edges, label=name)

    p.add_axes()
    p.show_grid()
    try:
        p.add_legend()
    except Exception:
        pass
    p.add_title(title)
    p.show()


if __name__ == "__main__":
    main()
