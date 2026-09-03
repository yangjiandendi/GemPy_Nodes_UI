"""Regression test for Visualization's display-only scalar-value filter.

Run from the node-editor project root:
    python scripts/test_visualization_scalar_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyvista as pv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.nodes import _filter_mesh_by_scalar_values, _parse_scalar_filter_values
from app.notebook_standalone_runtime import _filter_visualization_mesh


def main() -> None:
    mesh = pv.ImageData(dimensions=(4, 2, 2))
    mesh.cell_data["MaterialIDs"] = np.asarray([1, 2, 2], dtype=np.int32)

    filtered, report = _filter_mesh_by_scalar_values(mesh, "MaterialIDs", "2")
    assert filtered.n_cells == 2
    assert np.all(np.asarray(filtered.cell_data["MaterialIDs"]) == 2)
    assert report["display_only"] is True
    assert mesh.n_cells == 3

    multiple, _ = _filter_mesh_by_scalar_values(mesh, "materialids", "1, 2")
    assert multiple.n_cells == 3
    assert _parse_scalar_filter_values("2;5,7") == [2.0, 5.0, 7.0]

    notebook_filtered, notebook_report = _filter_visualization_mesh(mesh, "MaterialIDs", "2")
    assert notebook_filtered.n_cells == 2
    assert notebook_report["display_cells"] == 2
    print("Visualization scalar filter test passed.")


if __name__ == "__main__":
    main()
