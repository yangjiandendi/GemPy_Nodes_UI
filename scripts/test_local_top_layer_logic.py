"""Small dependency-light logic test for Remove Local Top Layer.

Run from the project root:
    python scripts/test_local_top_layer_logic.py

This test uses a lightweight fake mesh, so it does not require PyVista. It
verifies that two cells at the same absolute Z can receive different results
because the local DEM elevation differs.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.local_top_layer_nodes import _remove_local_top_layer


class _Centers:
    def __init__(self, points):
        self.points = np.asarray(points, dtype=float)


class _FakeMesh:
    def __init__(self, points, cells):
        self.points = np.asarray(points, dtype=float)
        self.cells = [np.asarray(cell, dtype=int) for cell in cells]
        self.point_data = {}
        self.cell_data = {}

    @property
    def n_points(self):
        return len(self.points)

    @property
    def n_cells(self):
        return len(self.cells)

    @property
    def bounds(self):
        p = self.points
        return (
            p[:, 0].min(), p[:, 0].max(), p[:, 1].min(),
            p[:, 1].max(), p[:, 2].min(), p[:, 2].max(),
        )

    def copy(self, deep=True):
        return copy.deepcopy(self)

    def extract_surface(self):
        return self.copy()

    def triangulate(self):
        return self.copy()

    def cell_centers(self):
        return _Centers([self.points[cell].mean(axis=0) for cell in self.cells])

    def threshold(self, value, scalars, preference, continuous=False, method=None, all_scalars=False):
        lower = float(value[0] if isinstance(value, (list, tuple, np.ndarray)) else value)
        if preference == "cell":
            keep = np.asarray(self.cell_data[scalars]) >= lower
        else:
            values = np.asarray(self.point_data[scalars])
            reducer = np.all if all_scalars else np.any
            keep = np.asarray([reducer(values[cell] >= lower) for cell in self.cells])
        return self.extract_cells(np.flatnonzero(keep))

    def extract_cells(self, ids):
        ids = np.asarray(ids, dtype=int)
        out = _FakeMesh(self.points.copy(), [self.cells[i].copy() for i in ids])
        out.point_data = {k: np.asarray(v).copy() for k, v in self.point_data.items()}
        out.cell_data = {k: np.asarray(v)[ids].copy() for k, v in self.cell_data.items()}
        return out

    def clean(self, **kwargs):
        return self


def main():
    # Sloping DEM: Z = 100 + 10 X.
    dem_points = [[x, y, 100 + 10 * x] for y in [0, 1] for x in [0, 1, 2]]
    dem = _FakeMesh(dem_points, [[0, 1, 4], [0, 4, 3], [1, 2, 5], [1, 5, 4]])
    dem.point_data["elevation"] = dem.points[:, 2].copy()

    # Both cells have center Z=90. At X=.5 depth is 15 m -> remove.
    # At X=1.5 depth is 25 m -> retain.
    volume_points = []
    cells = []
    for x0 in [0, 1]:
        start = len(volume_points)
        for z in [85, 95]:
            for y in [0, 1]:
                for x in [x0, x0 + 1]:
                    volume_points.append([x, y, z])
        cells.append(list(range(start, start + 8)))
    volume = _FakeMesh(volume_points, cells)
    volume.cell_data["MaterialIDs"] = np.asarray([10, 20])

    result, lowered_dem, report = _remove_local_top_layer(
        volume,
        dem,
        thickness=20,
        sampling_method="auto",
        selection_mode="cell_center",
        crop_to_dem_xy=True,
        clean_output=True,
    )

    assert result.n_cells == 1
    assert int(result.cell_data["MaterialIDs"][0]) == 20
    assert np.allclose(lowered_dem.points[:, 2], dem.points[:, 2] - 20)
    assert report["dem_sampling"]["sampling_method"] == "regular_grid_linear"
    print("PASS: local DEM reference removed only the shallow cell.")


if __name__ == "__main__":
    main()
