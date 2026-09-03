from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import numpy as np

from .models import NodeExecutionError, RuntimeValue
from .storage import make_runtime_path, register_output_file
from .nodes import (
    BaseNode,
    _as_bool,
    _as_float,
    _as_int,
    _attach_web_surface_preview,
    _choose_mesh_scalar,
    _resolve_mesh_or_file_input,
)

pv = None
binary_fill_holes = None
cKDTree = None


def _ensure_repair_dependencies() -> None:
    global pv, binary_fill_holes, cKDTree
    if pv is None:
        try:
            import pyvista as _pv
            from scipy.ndimage import binary_fill_holes as _binary_fill_holes
            from scipy.spatial import cKDTree as _cKDTree
        except Exception as exc:
            raise NodeExecutionError(
                "Repair and Reorder Voxel Mesh requires pyvista and scipy. "
                f"Import failed: {exc}"
            ) from exc
        pv = _pv
        binary_fill_holes = _binary_fill_holes
        cKDTree = _cKDTree


def _ensure_ogs_dependencies() -> None:
    global pv
    if pv is None:
        try:
            import pyvista as _pv
        except Exception as exc:
            raise NodeExecutionError(
                f"OGS Identify Full Mesh requires pyvista: {exc}"
            ) from exc
        pv = _pv



# Notebook Part 1A helper functions
STALE_POINT_ARRAYS = {
    "bulk_node_ids",
    "vtkOriginalPointIds",
    "PointMergeMap",
}

STALE_CELL_ARRAYS = {
    "bulk_element_ids",
    "vtkOriginalCellIds",
    "merge_source_index",
    "merge_source_cell",
    "number_bulk_elements",
}


def get_uniform_voxel_cell_type(
    mesh: pv.UnstructuredGrid,
) -> pv.CellType:
    """
    Return the single supported 8-node volume cell type used by the mesh.

    VTK cell type 11 = VOXEL
    VTK cell type 12 = HEXAHEDRON
    """
    unique_types = np.unique(np.asarray(mesh.celltypes))

    if unique_types.size != 1:
        unique_values, counts = np.unique(
            np.asarray(mesh.celltypes),
            return_counts=True,
        )
        raise ValueError(
            "The mesh must use one uniform 8-node cell type. Found: "
            f"{dict(zip(unique_values.tolist(), counts.tolist()))}"
        )

    cell_type_value = int(unique_types[0])

    if cell_type_value == int(pv.CellType.VOXEL):
        return pv.CellType.VOXEL

    if cell_type_value == int(pv.CellType.HEXAHEDRON):
        return pv.CellType.HEXAHEDRON

    raise ValueError(
        "Only VTK_VOXEL (11) and VTK_HEXAHEDRON (12) are supported. "
        f"Found cell type: {cell_type_value}"
    )


def infer_regular_spacing(values: np.ndarray, decimals: int = 6) -> float:
    """Infer nominal regular-grid spacing from cell-center coordinates."""
    unique_values = np.unique(np.round(np.asarray(values), decimals))
    differences = np.diff(unique_values)
    differences = differences[differences > 10 ** (-decimals + 1)]

    if differences.size == 0:
        raise ValueError("Could not infer a positive grid spacing.")

    return float(np.median(differences))


def remove_stale_mapping_arrays(
    mesh: pv.UnstructuredGrid,
) -> pv.UnstructuredGrid:
    """Remove mappings that become invalid after repair or reordering."""
    cleaned = mesh.copy(deep=True)

    for name in list(cleaned.point_data.keys()):
        if name in STALE_POINT_ARRAYS:
            del cleaned.point_data[name]

    for name in list(cleaned.cell_data.keys()):
        if name in STALE_CELL_ARRAYS:
            del cleaned.cell_data[name]

    for name in list(cleaned.field_data.keys()):
        if name == "bulk_element_ids":
            del cleaned.field_data[name]

    return cleaned


def remove_voxels_below_bottom(
    mesh: pv.UnstructuredGrid,
    bottom_z: float,
    dz: float,
    tolerance: float = 1e-6,
) -> pv.UnstructuredGrid:
    """Keep cells whose lower face is at or above bottom_z."""
    centers_z = np.asarray(mesh.cell_centers().points)[:, 2]
    lower_face_z = centers_z - dz / 2.0
    keep = lower_face_z >= bottom_z - tolerance
    return mesh.extract_cells(np.flatnonzero(keep))


def infer_axis_grid_reference(
    values: np.ndarray,
    spacing: float,
) -> tuple[float, np.ndarray, float]:
    """Infer the phase/reference of one regular center-coordinate axis.

    A mesh can be perfectly regular while its center coordinates are shifted by
    a constant offset relative to a configured origin or bottom. The returned
    integer indices always start at zero for the minimum observed coordinate.
    """
    coordinates = np.asarray(values, dtype=float).reshape(-1)

    if coordinates.size == 0:
        raise ValueError(
            "Cannot infer a grid reference from an empty coordinate axis."
        )

    spacing = float(spacing)
    if not np.isfinite(spacing) or spacing <= 0:
        raise ValueError(
            f"Grid spacing must be positive, got {spacing!r}."
        )

    anchor = float(np.min(coordinates))
    provisional_indices = np.rint(
        (coordinates - anchor) / spacing
    ).astype(np.int64)

    candidate_references = (
        coordinates
        - provisional_indices.astype(float) * spacing
    )
    reference = float(
        np.median(candidate_references)
    )

    indices = np.rint(
        (coordinates - reference) / spacing
    ).astype(np.int64)

    minimum_index = int(indices.min())
    reference = float(
        reference + minimum_index * spacing
    )
    indices = indices - minimum_index

    reconstructed = (
        reference
        + indices.astype(float) * spacing
    )
    maximum_residual = float(
        np.max(
            np.abs(
                reconstructed - coordinates
            )
        )
    )

    return reference, indices, maximum_residual


def build_voxel_index(
    mesh: pv.UnstructuredGrid,
    dx: float,
    dy: float,
    dz: float,
    bottom_z: float,
    tolerance: float = 1e-4,
    reference_mode: str = "auto_from_centers",
) -> dict:
    """Quantize cell centers onto a nominal regular voxel grid.

    ``auto_from_centers`` infers the actual X/Y/Z center-lattice phase from the
    input mesh. This is the recommended mode and accepts a regular grid whose
    bottom is shifted relative to ``bottom_z``.

    ``configured_bottom_strict`` preserves the original behavior and forces the
    first Z center to ``bottom_z + dz/2``.
    """
    centers = np.asarray(
        mesh.cell_centers().points,
        dtype=float,
    )

    if centers.size == 0:
        raise ValueError(
            "The voxel mesh contains no cells."
        )

    reference_mode = str(
        reference_mode or "auto_from_centers"
    ).strip().lower()

    x_reference, i, residual_x = infer_axis_grid_reference(
        centers[:, 0],
        dx,
    )
    y_reference, j, residual_y = infer_axis_grid_reference(
        centers[:, 1],
        dy,
    )

    if reference_mode == "configured_bottom_strict":
        z_reference = float(
            bottom_z + dz / 2.0
        )
        k = np.rint(
            (centers[:, 2] - z_reference) / dz
        ).astype(np.int64)

        reconstructed_z = (
            z_reference
            + k.astype(float) * dz
        )
        residual_z = float(
            np.max(
                np.abs(
                    reconstructed_z
                    - centers[:, 2]
                )
            )
        )

        if np.min(k) < 0:
            raise ValueError(
                "Cells below the configured bottom remain in strict "
                "grid-reference mode."
            )

    elif reference_mode == "auto_from_centers":
        z_reference, k, residual_z = infer_axis_grid_reference(
            centers[:, 2],
            dz,
        )

    else:
        raise ValueError(
            f"Unknown grid reference mode {reference_mode!r}. "
            "Use 'auto_from_centers' or 'configured_bottom_strict'."
        )

    residuals = {
        "x": float(residual_x),
        "y": float(residual_y),
        "z": float(residual_z),
    }
    maximum_residual = float(
        max(residuals.values())
    )

    if maximum_residual > float(tolerance):
        raise ValueError(
            "Cell centers do not fit a regular grid after reference "
            f"inference. Maximum residual={maximum_residual:.9g}; "
            f"per-axis residuals={residuals}; "
            f"spacing={[float(dx), float(dy), float(dz)]}; "
            f"references={[x_reference, y_reference, z_reference]}; "
            f"tolerance={float(tolerance):.9g}."
        )

    nx = int(i.max()) + 1
    ny = int(j.max()) + 1
    nz = int(k.max()) + 1

    occupancy = np.zeros(
        (nx, ny, nz),
        dtype=bool,
    )
    cell_ids = np.full(
        (nx, ny, nz),
        -1,
        dtype=np.int64,
    )

    duplicate_count = 0

    for cell_id, (ii, jj, kk) in enumerate(
        zip(i, j, k)
    ):
        if occupancy[ii, jj, kk]:
            duplicate_count += 1
            continue

        occupancy[ii, jj, kk] = True
        cell_ids[ii, jj, kk] = cell_id

    if duplicate_count:
        raise ValueError(
            f"Found {duplicate_count} duplicate voxel locations "
            "after regular-grid quantization."
        )

    effective_grid_bottom_z = float(
        z_reference - dz / 2.0
    )

    return {
        "centers": centers,
        "x_reference": float(x_reference),
        "y_reference": float(y_reference),
        "z_reference": float(z_reference),
        "dx": float(dx),
        "dy": float(dy),
        "dz": float(dz),
        "nx": nx,
        "ny": ny,
        "nz": nz,
        "occupancy": occupancy,
        "cell_ids": cell_ids,
        "i": i,
        "j": j,
        "k": k,
        "reference_mode": reference_mode,
        "grid_references": [
            float(x_reference),
            float(y_reference),
            float(z_reference),
        ],
        "axis_residuals": residuals,
        "maximum_grid_residual": maximum_residual,
        "configured_bottom_z": float(bottom_z),
        "effective_grid_bottom_z": effective_grid_bottom_z,
        "grid_bottom_offset_from_configured": float(
            effective_grid_bottom_z - bottom_z
        ),
    }


def find_source_cell(
    i: int,
    j: int,
    k: int,
    occupancy: np.ndarray,
    cell_ids: np.ndarray,
    maximum_xy_radius: int = 20,
) -> int:
    """
    Find a nearby source cell for MaterialIDs and other cell-data arrays.

    Priority:
    1. closest voxel in the same vertical column;
    2. voxel at the same k in a nearby column;
    3. vertically closest voxel in a nearby column.
    """
    same_column_k = np.flatnonzero(occupancy[i, j, :])

    if same_column_k.size:
        nearest_k = same_column_k[np.argmin(np.abs(same_column_k - k))]
        return int(cell_ids[i, j, nearest_k])

    nx, ny, _ = occupancy.shape

    for radius in range(1, maximum_xy_radius + 1):
        candidates = []

        for di in range(-radius, radius + 1):
            for dj in range(-radius, radius + 1):
                if max(abs(di), abs(dj)) != radius:
                    continue

                ni, nj = i + di, j + dj

                if 0 <= ni < nx and 0 <= nj < ny:
                    candidates.append((ni, nj))

        for ni, nj in candidates:
            if occupancy[ni, nj, k]:
                return int(cell_ids[ni, nj, k])

        best_source = None
        best_vertical_distance = None

        for ni, nj in candidates:
            neighbor_k = np.flatnonzero(occupancy[ni, nj, :])

            if neighbor_k.size == 0:
                continue

            nearest_k = neighbor_k[np.argmin(np.abs(neighbor_k - k))]
            vertical_distance = abs(int(nearest_k) - k)

            if (
                best_vertical_distance is None
                or vertical_distance < best_vertical_distance
            ):
                best_vertical_distance = vertical_distance
                best_source = int(cell_ids[ni, nj, nearest_k])

        if best_source is not None:
            return best_source

    raise RuntimeError(
        f"No source cell found near voxel index ({i}, {j}, {k})."
    )


def detect_bottom_repair_voxels(
    info: dict,
    lowest_layers: int,
) -> list[tuple[int, int, int]]:
    """
    Detect missing voxels near the bottom.

    Empty bottom columns connected to the outside are not filled.
    Enclosed bottom holes are filled continuously.
    """
    occupancy = info["occupancy"]
    nx, ny, nz = occupancy.shape

    number_of_low_layers = min(lowest_layers, nz)
    bottom_occupied = occupancy[:, :, 0]

    filled_bottom_footprint = binary_fill_holes(bottom_occupied)
    enclosed_bottom_holes = (
        filled_bottom_footprint & (~bottom_occupied)
    )

    missing = []

    for i in range(nx):
        for j in range(ny):
            existing_k = np.flatnonzero(occupancy[i, j, :])

            if bottom_occupied[i, j]:
                maximum_existing_k = int(existing_k.max())
                upper_k = min(
                    number_of_low_layers - 1,
                    maximum_existing_k,
                )

                for k in range(upper_k + 1):
                    if not occupancy[i, j, k]:
                        missing.append((i, j, k))

                continue

            if not enclosed_bottom_holes[i, j]:
                continue

            if existing_k.size:
                first_existing_k = int(existing_k.min())
                maximum_existing_k = int(existing_k.max())

                inspected_upper = min(
                    number_of_low_layers - 1,
                    maximum_existing_k,
                )
                connection_upper = first_existing_k - 1
                upper_k = max(inspected_upper, connection_upper)
            else:
                upper_k = number_of_low_layers - 1

            for k in range(upper_k + 1):
                if not occupancy[i, j, k]:
                    missing.append((i, j, k))

    return missing


def create_voxel_patch(
    mesh: pv.UnstructuredGrid,
    info: dict,
    missing_voxels: list[tuple[int, int, int]],
    maximum_xy_radius: int = 20,
) -> pv.UnstructuredGrid:
    """Create missing HEX8 cells and inherit data from nearby cells/points."""
    if not missing_voxels:
        return pv.UnstructuredGrid()

    x_ref = info["x_reference"]
    y_ref = info["y_reference"]
    z_ref = info["z_reference"]
    dx = info["dx"]
    dy = info["dy"]
    dz = info["dz"]
    occupancy = info["occupancy"]
    cell_ids = info["cell_ids"]

    points = []
    connectivity = []
    source_cell_ids = []

    mesh_cell_type = get_uniform_voxel_cell_type(mesh)

    for i, j, k in missing_voxels:
        xc = x_ref + i * dx
        yc = y_ref + j * dy
        zc = z_ref + k * dz

        x0, x1 = xc - dx / 2.0, xc + dx / 2.0
        y0, y1 = yc - dy / 2.0, yc + dy / 2.0
        z0, z1 = zc - dz / 2.0, zc + dz / 2.0

        base = len(points)

        if mesh_cell_type == pv.CellType.VOXEL:
            # VTK_VOXEL point order:
            # 0,1 vary in x; 0,2 vary in y; 0,4 vary in z.
            voxel_points = [
                [x0, y0, z0],
                [x1, y0, z0],
                [x0, y1, z0],
                [x1, y1, z0],
                [x0, y0, z1],
                [x1, y0, z1],
                [x0, y1, z1],
                [x1, y1, z1],
            ]
        else:
            # VTK_HEXAHEDRON point order.
            voxel_points = [
                [x0, y0, z0],
                [x1, y0, z0],
                [x1, y1, z0],
                [x0, y1, z0],
                [x0, y0, z1],
                [x1, y0, z1],
                [x1, y1, z1],
                [x0, y1, z1],
            ]

        points.extend(voxel_points)

        connectivity.append(
            [
                base,
                base + 1,
                base + 2,
                base + 3,
                base + 4,
                base + 5,
                base + 6,
                base + 7,
            ]
        )

        source_cell_ids.append(
            find_source_cell(
                i,
                j,
                k,
                occupancy,
                cell_ids,
                maximum_xy_radius=maximum_xy_radius,
            )
        )

    patch_points = np.asarray(points, dtype=float)
    patch_connectivity = np.asarray(connectivity, dtype=np.int64)

    patch = pv.UnstructuredGrid(
        {mesh_cell_type: patch_connectivity},
        patch_points,
    )

    source_cell_ids = np.asarray(source_cell_ids, dtype=np.int64)

    for name in mesh.cell_data.keys():
        if name in STALE_CELL_ARRAYS:
            continue

        patch.cell_data[name] = np.asarray(mesh.cell_data[name])[
            source_cell_ids
        ].copy()

    if mesh.point_data:
        point_tree = cKDTree(np.asarray(mesh.points))
        _, nearest_point_ids = point_tree.query(patch_points, k=1)

        for name in mesh.point_data.keys():
            if name in STALE_POINT_ARRAYS:
                continue

            patch.point_data[name] = np.asarray(mesh.point_data[name])[
                nearest_point_ids
            ].copy()

    return patch


def append_patch(
    mesh: pv.UnstructuredGrid,
    patch: pv.UnstructuredGrid,
    merge_point_tolerance: float = 1e-8,
) -> pv.UnstructuredGrid:
    """Append patch cells and merge coincident grid points."""
    if patch.n_cells == 0:
        return mesh.copy(deep=True)

    merged = mesh.merge(
        patch,
        merge_points=True,
        tolerance=float(merge_point_tolerance),
    )

    for name in mesh.field_data.keys():
        if name not in merged.field_data:
            merged.field_data[name] = np.asarray(
                mesh.field_data[name]
            ).copy()

    return merged


def remove_floating_columns(
    mesh: pv.UnstructuredGrid,
    dx: float,
    dy: float,
    dz: float,
    bottom_z: float,
    tolerance: float = 1e-4,
    reference_mode: str = "auto_from_centers",
) -> tuple[pv.UnstructuredGrid, int, int]:
    """Delete complete columns whose lowest voxel does not rest on bottom_z."""
    info = build_voxel_index(
        mesh,
        dx,
        dy,
        dz,
        bottom_z,
        tolerance=tolerance,
        reference_mode=reference_mode,
    )
    occupancy = info["occupancy"]

    column_has_bottom = occupancy[:, :, 0]
    keep_cells = column_has_bottom[info["i"], info["j"]]

    floating_columns = (
        occupancy.any(axis=2) & (~column_has_bottom)
    )

    cleaned = mesh.extract_cells(np.flatnonzero(keep_cells))

    return (
        cleaned,
        int(np.count_nonzero(~keep_cells)),
        int(np.count_nonzero(floating_columns)),
    )


def remove_one_outer_material_layer(
    mesh: pv.UnstructuredGrid,
    dx: float,
    dy: float,
    dz: float,
    bottom_z: float,
    material_array: str,
    material_id: int,
    tolerance: float = 1e-4,
    reference_mode: str = "auto_from_centers",
) -> tuple[pv.UnstructuredGrid, int, int]:
    """
    Remove one initial outer layer of pure-material columns.

    This is a single pass. Newly exposed columns are not checked again.
    """
    info = build_voxel_index(
        mesh,
        dx,
        dy,
        dz,
        bottom_z,
        tolerance=tolerance,
        reference_mode=reference_mode,
    )
    occupancy = info["occupancy"]
    cell_ids = info["cell_ids"]
    footprint = occupancy.any(axis=2)

    padded = np.pad(footprint, 1, mode="constant", constant_values=False)

    west_missing = ~padded[:-2, 1:-1]
    east_missing = ~padded[2:, 1:-1]
    south_missing = ~padded[1:-1, :-2]
    north_missing = ~padded[1:-1, 2:]

    outer = footprint & (
        west_missing
        | east_missing
        | south_missing
        | north_missing
    )

    materials = np.asarray(mesh.cell_data[material_array]).reshape(-1)
    remove_column = np.zeros_like(footprint, dtype=bool)

    for i, j in np.argwhere(outer):
        ids = cell_ids[i, j, :]
        ids = ids[ids >= 0]

        if ids.size and np.all(materials[ids] == material_id):
            remove_column[i, j] = True

    remove_cells = remove_column[info["i"], info["j"]]
    cleaned = mesh.extract_cells(np.flatnonzero(~remove_cells))

    return (
        cleaned,
        int(np.count_nonzero(remove_cells)),
        int(np.count_nonzero(remove_column)),
    )

# Notebook Part 1B helper functions
def first_occurrence_node_order(
    sorted_connectivity: np.ndarray,
    number_of_points: int,
    chunk_size: int = 2_000_000,
) -> tuple[np.ndarray, np.ndarray]:
    """Order nodes by first occurrence in the reordered cell connectivity."""
    flat = sorted_connectivity.reshape(-1)
    sentinel = np.iinfo(np.int64).max

    first_position = np.full(
        number_of_points,
        sentinel,
        dtype=np.int64,
    )

    for start in range(0, flat.size, chunk_size):
        stop = min(start + chunk_size, flat.size)
        ids = flat[start:stop]
        positions = np.arange(start, stop, dtype=np.int64)
        np.minimum.at(first_position, ids, positions)

    used_nodes = np.flatnonzero(first_position != sentinel)
    used_nodes = used_nodes[
        np.argsort(first_position[used_nodes], kind="stable")
    ]

    unused_nodes = np.flatnonzero(first_position == sentinel)

    return np.concatenate((used_nodes, unused_nodes)), first_position


def reorder_mesh_by_material(
    mesh: pv.UnstructuredGrid,
    material_array: str,
    add_node_material_ids: bool = True,
    keep_original_order_ids: bool = False,
) -> pv.UnstructuredGrid:
    """
    Reorder cells by MaterialID -> z -> y -> x.

    Reorder nodes by first occurrence in those reordered cells.
    No bulk IDs are written in Part 1.
    """
    mesh = remove_stale_mapping_arrays(mesh)

    if material_array not in mesh.cell_data:
        raise KeyError(
            f"Missing cell-data array '{material_array}'."
        )

    mesh_cell_type = get_uniform_voxel_cell_type(mesh)

    connectivity = np.asarray(
        mesh.cell_connectivity,
        dtype=np.int64,
    ).reshape(mesh.n_cells, 8)

    centers = np.asarray(mesh.cell_centers().points)
    materials = np.asarray(mesh.cell_data[material_array]).reshape(-1)

    cell_order = np.lexsort(
        (
            centers[:, 0],
            centers[:, 1],
            centers[:, 2],
            materials,
        )
    )

    sorted_connectivity_old = connectivity[cell_order]
    sorted_materials = materials[cell_order]

    node_order, first_position = first_occurrence_node_order(
        sorted_connectivity_old,
        mesh.n_points,
    )

    old_to_new_node = np.empty(mesh.n_points, dtype=np.int64)
    old_to_new_node[node_order] = np.arange(
        mesh.n_points,
        dtype=np.int64,
    )

    sorted_connectivity_new = old_to_new_node[
        sorted_connectivity_old
    ]
    sorted_points = np.asarray(mesh.points)[node_order]

    reordered = pv.UnstructuredGrid(
        {mesh_cell_type: sorted_connectivity_new},
        sorted_points,
    )

    for name in mesh.point_data.keys():
        if name in STALE_POINT_ARRAYS:
            continue

        reordered.point_data[name] = np.asarray(
            mesh.point_data[name]
        )[node_order]

    for name in mesh.cell_data.keys():
        if name in STALE_CELL_ARRAYS:
            continue

        reordered.cell_data[name] = np.asarray(
            mesh.cell_data[name]
        )[cell_order]

    for name in mesh.field_data.keys():
        reordered.field_data[name] = np.asarray(
            mesh.field_data[name]
        ).copy()

    if add_node_material_ids:
        sentinel = np.iinfo(np.int64).max
        used_old_nodes = np.flatnonzero(first_position != sentinel)

        old_node_material = np.full(
            mesh.n_points,
            -1,
            dtype=sorted_materials.dtype,
        )

        first_cell_position = first_position[used_old_nodes] // 8
        old_node_material[used_old_nodes] = sorted_materials[
            first_cell_position
        ]

        reordered.point_data["NodeMaterialIDs"] = old_node_material[
            node_order
        ]

    if keep_original_order_ids:
        reordered.point_data["OriginalNodeIDs"] = node_order.astype(
            np.uint64
        )
        reordered.cell_data["OriginalElementIDs"] = cell_order.astype(
            np.uint64
        )

    return reordered


def print_material_ranges(mesh: pv.UnstructuredGrid) -> None:
    """Print contiguous cell and node ranges after reordering."""
    materials = np.asarray(mesh.cell_data[MATERIAL_ARRAY]).reshape(-1)

    print("\nCell ranges:")
    for material_id in np.unique(materials):
        ids = np.flatnonzero(materials == material_id)
        print(
            f"  MaterialID {int(material_id)}: "
            f"{int(ids[0]):,} .. {int(ids[-1]):,} "
            f"({ids.size:,} cells)"
        )

    if "NodeMaterialIDs" in mesh.point_data:
        node_materials = np.asarray(
            mesh.point_data["NodeMaterialIDs"]
        ).reshape(-1)

        print("\nNode ranges by first-used material:")
        for material_id in np.unique(node_materials):
            ids = np.flatnonzero(node_materials == material_id)
            print(
                f"  MaterialID {int(material_id)}: "
                f"{int(ids[0]):,} .. {int(ids[-1]):,} "
                f"({ids.size:,} nodes)"
            )

def material_range_report(mesh: Any, material_array: str) -> Dict[str, Any]:
    report: Dict[str, Any] = {"cell_ranges": [], "node_ranges": []}
    materials = np.asarray(mesh.cell_data[material_array]).reshape(-1)

    for material_id in np.unique(materials):
        ids = np.flatnonzero(materials == material_id)
        report["cell_ranges"].append(
            {
                "material_id": int(material_id),
                "first_cell": int(ids[0]),
                "last_cell": int(ids[-1]),
                "count": int(ids.size),
            }
        )

    if "NodeMaterialIDs" in mesh.point_data:
        node_materials = np.asarray(
            mesh.point_data["NodeMaterialIDs"]
        ).reshape(-1)
        for material_id in np.unique(node_materials):
            ids = np.flatnonzero(node_materials == material_id)
            report["node_ranges"].append(
                {
                    "material_id": int(material_id),
                    "first_node": int(ids[0]),
                    "last_node": int(ids[-1]),
                    "count": int(ids.size),
                }
            )

    return report


# Notebook Part 2 helper functions
def find_identify_subdomains_executable(
    ogs_bin_dir: Path | None,
) -> Path:
    """Find identifySubdomains(.exe)."""
    names = (
        ["identifySubdomains.exe", "identifySubdomains"]
        if os.name == "nt"
        else ["identifySubdomains", "identifySubdomains.exe"]
    )

    if ogs_bin_dir is not None:
        folder = Path(ogs_bin_dir).expanduser().resolve()

        for name in names:
            candidate = folder / name
            if candidate.exists():
                return candidate

        raise FileNotFoundError(
            f"identifySubdomains was not found in {folder}"
        )

    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    raise FileNotFoundError(
        "identifySubdomains was not found. "
        "Set OGS_BIN_DIR or add the OGS bin folder to PATH."
    )


def apply_ogs_identify_to_full_mesh(
    repaired_mesh_path: Path,
    final_output_path: Path,
    ogs_bin_dir: Path | None,
    search_length: float = 1e-6,
) -> Path:
    """
    Run OGS using the same full mesh as bulk and full-dimensional subdomain.

    The output is still one full bulk VTU, now containing:
      PointData: bulk_node_ids
      CellData : bulk_element_ids
    """
    repaired_mesh_path = repaired_mesh_path.expanduser().resolve()
    final_output_path = final_output_path.expanduser().resolve()
    final_output_path.parent.mkdir(parents=True, exist_ok=True)

    executable = find_identify_subdomains_executable(ogs_bin_dir)

    # OGS creates and renames intermediate files while it is running.  Keeping
    # those files inside a synchronised project tree (for example sciebo) can
    # race with the sync client: a path returned by glob() may disappear before
    # stat() is called.  Isolate the complete OGS transaction in the operating
    # system's temporary directory and copy back only the completed VTU.
    with tempfile.TemporaryDirectory(
        prefix="gempy_ogs_identify_"
    ) as temporary_directory:
        temporary_directory_path = Path(temporary_directory)
        working_input_path = (
            temporary_directory_path / repaired_mesh_path.name
        )
        shutil.copy2(repaired_mesh_path, working_input_path)
        temporary_prefix = (
            temporary_directory_path
            / f"_identify_tmp_{final_output_path.stem}_"
        )

        command = [
            str(executable),
            "-f",
            "-m",
            str(working_input_path),
            "-s",
            f"{search_length:.16g}",
            "-o",
            str(temporary_prefix),
            "--",
            str(working_input_path),
        ]

        print("Running:")
        print(
            subprocess.list2cmdline(command)
            if os.name == "nt"
            else shlex.join(command)
        )

        subprocess.run(command, check=True)

        generated_candidates = _newest_existing_paths(
            temporary_directory_path.glob(
                f"{temporary_prefix.name}*.vtu"
            )
        )

        if not generated_candidates:
            raise FileNotFoundError(
                "identifySubdomains finished, but no output VTU was found."
            )

        generated_output = generated_candidates[0]

        if final_output_path.exists():
            final_output_path.unlink()

        shutil.copy2(generated_output, final_output_path)

    result = pv.read(final_output_path)

    if "bulk_node_ids" not in result.point_data:
        raise RuntimeError(
            "OGS output does not contain point-data bulk_node_ids."
        )

    if "bulk_element_ids" not in result.cell_data:
        raise RuntimeError(
            "OGS output does not contain cell-data bulk_element_ids."
        )

    bulk_node_ids = np.asarray(
        result.point_data["bulk_node_ids"]
    ).reshape(-1)

    bulk_element_ids = np.asarray(
        result.cell_data["bulk_element_ids"]
    ).reshape(-1)

    if bulk_node_ids.size != result.n_points:
        raise RuntimeError(
            "bulk_node_ids length does not match point count."
        )

    if bulk_element_ids.size != result.n_cells:
        raise RuntimeError(
            "bulk_element_ids length does not match cell count."
        )

    expected_nodes = np.arange(result.n_points, dtype=np.uint64)
    expected_cells = np.arange(result.n_cells, dtype=np.uint64)

    node_identity = np.array_equal(
        bulk_node_ids.astype(np.uint64),
        expected_nodes,
    )

    element_identity = np.array_equal(
        bulk_element_ids.astype(np.uint64),
        expected_cells,
    )

    print("\n=== PART 2 COMPLETE ===")
    print(f"Final output: {final_output_path}")
    print(f"Points: {result.n_points:,}")
    print(f"Cells : {result.n_cells:,}")
    print(
        f"bulk_node_ids range: "
        f"{int(bulk_node_ids.min()):,} .. "
        f"{int(bulk_node_ids.max()):,}"
    )
    print(
        f"bulk_element_ids range: "
        f"{int(bulk_element_ids.min()):,} .. "
        f"{int(bulk_element_ids.max()):,}"
    )
    print(f"Node mapping is identity   : {node_identity}")
    print(f"Element mapping is identity: {element_identity}")

    return final_output_path


def _newest_existing_paths(paths: Iterable[Path]) -> List[Path]:
    """Sort existing paths by mtime while tolerating concurrent disappearance."""
    stamped_paths = []
    for path in paths:
        try:
            stamped_paths.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    stamped_paths.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in stamped_paths]

class RepairReorderVoxelMeshNode(BaseNode):
    """Notebook Part 1: repair, clean, reorder, and optionally shift MaterialIDs."""

    type_name = "RepairReorderVoxelMesh"

    def run(
        self,
        inputs: Dict[str, Any],
        params: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, RuntimeValue]:
        _ensure_repair_dependencies()

        mesh, source_name, source_meta = _resolve_mesh_or_file_input(
            inputs,
            params,
            input_port="mesh",
            file_param="mesh_file_id",
        )

        try:
            if not isinstance(mesh, pv.UnstructuredGrid):
                mesh = mesh.cast_to_unstructured_grid()

            material_array = (
                str(params.get("material_array") or "MaterialIDs").strip()
                or "MaterialIDs"
            )
            bottom_z = float(_as_float(params.get("bottom_z"), -200.0))
            lowest_layers = int(
                _as_int(params.get("lowest_layers_to_repair"), 20) or 20
            )
            spacing_decimals = int(
                _as_int(params.get("spacing_decimals"), 6) or 6
            )
            grid_tolerance = float(
                _as_float(params.get("grid_tolerance"), 1e-4) or 1e-4
            )
            grid_reference_mode = str(
                params.get("grid_reference_mode")
                or "auto_from_centers"
            ).strip().lower()
            bottom_tolerance = float(
                _as_float(params.get("bottom_tolerance"), 1e-6) or 1e-6
            )
            maximum_xy_radius = int(
                _as_int(params.get("maximum_xy_source_radius"), 20) or 20
            )
            merge_point_tolerance = float(
                _as_float(params.get("merge_point_tolerance"), 1e-8) or 1e-8
            )

            repair_bottom_holes = _as_bool(
                params.get("repair_bottom_holes"),
                True,
            )
            remove_floating = _as_bool(
                params.get("remove_floating_columns"),
                True,
            )
            remove_outer = _as_bool(
                params.get("remove_one_outer_material_layer"),
                True,
            )
            outer_material_id = int(
                _as_int(params.get("outer_layer_material_id"), 1) or 1
            )
            add_node_material_ids = _as_bool(
                params.get("add_node_material_ids"),
                True,
            )
            keep_original_order_ids = _as_bool(
                params.get("keep_original_order_ids"),
                False,
            )
            remove_stale = _as_bool(
                params.get("remove_stale_mapping_arrays"),
                True,
            )

            # General zero-based material reindexing.
            # Backward compatibility: old saved workflows may still contain
            # "convert_material_ids"; use it only when the new parameter is absent.
            reindex_material_ids_from_zero = _as_bool(
                params.get(
                    "reindex_material_ids_from_zero",
                    params.get("convert_material_ids", True),
                ),
                True,
            )

            if remove_stale:
                mesh_original = remove_stale_mapping_arrays(mesh)
            else:
                mesh_original = mesh.copy(deep=True)

            if material_array not in mesh_original.cell_data:
                raise NodeExecutionError(
                    f"'{material_array}' not found. Available cell arrays: "
                    f"{list(mesh_original.cell_data.keys())}"
                )

            get_uniform_voxel_cell_type(mesh_original)

            centers_original = np.asarray(
                mesh_original.cell_centers().points
            )
            dx = infer_regular_spacing(
                centers_original[:, 0],
                decimals=spacing_decimals,
            )
            dy = infer_regular_spacing(
                centers_original[:, 1],
                decimals=spacing_decimals,
            )
            dz = infer_regular_spacing(
                centers_original[:, 2],
                decimals=spacing_decimals,
            )

            original_points = int(mesh_original.n_points)
            original_cells = int(mesh_original.n_cells)

            mesh_clipped = remove_voxels_below_bottom(
                mesh_original,
                bottom_z=bottom_z,
                dz=dz,
                tolerance=bottom_tolerance,
            )
            if mesh_clipped.n_cells == 0:
                raise NodeExecutionError(
                    "All cells were removed by the configured bottom clipping."
                )
            removed_below = int(
                original_cells - mesh_clipped.n_cells
            )

            index_before_fill = build_voxel_index(
                mesh_clipped,
                dx=dx,
                dy=dy,
                dz=dz,
                bottom_z=bottom_z,
                tolerance=grid_tolerance,
                reference_mode=grid_reference_mode,
            )

            missing_voxels: List[tuple[int, int, int]] = []
            if repair_bottom_holes:
                missing_voxels = detect_bottom_repair_voxels(
                    index_before_fill,
                    lowest_layers=lowest_layers,
                )

            patch = create_voxel_patch(
                mesh_clipped,
                index_before_fill,
                missing_voxels,
                maximum_xy_radius=maximum_xy_radius,
            )
            mesh_filled = append_patch(
                mesh_clipped,
                patch,
                merge_point_tolerance=merge_point_tolerance,
            )

            removed_floating_cells = 0
            removed_floating_columns = 0
            if remove_floating:
                (
                    mesh_supported,
                    removed_floating_cells,
                    removed_floating_columns,
                ) = remove_floating_columns(
                    mesh_filled,
                    dx=dx,
                    dy=dy,
                    dz=dz,
                    bottom_z=bottom_z,
                    tolerance=grid_tolerance,
                    reference_mode=grid_reference_mode,
                )
            else:
                mesh_supported = mesh_filled

            removed_outer_cells = 0
            removed_outer_columns = 0
            if remove_outer:
                (
                    mesh_cleaned,
                    removed_outer_cells,
                    removed_outer_columns,
                ) = remove_one_outer_material_layer(
                    mesh_supported,
                    dx=dx,
                    dy=dy,
                    dz=dz,
                    bottom_z=bottom_z,
                    material_array=material_array,
                    material_id=outer_material_id,
                    tolerance=grid_tolerance,
                    reference_mode=grid_reference_mode,
                )
            else:
                mesh_cleaned = mesh_supported

            if mesh_cleaned.n_cells == 0:
                raise NodeExecutionError(
                    "The selected repair settings removed all cells."
                )

            mesh_reordered = reorder_mesh_by_material(
                mesh_cleaned,
                material_array=material_array,
                add_node_material_ids=add_node_material_ids,
                keep_original_order_ids=keep_original_order_ids,
            )

            material_values = np.asarray(
                mesh_reordered.cell_data[material_array]
            ).copy()
            unique_before = np.unique(material_values)

            material_reindex_mapping: Dict[str, int] = {}
            if reindex_material_ids_from_zero:
                # Sort the unique original material values and map them
                # consecutively to 0, 1, ..., n-1. This works for arbitrary
                # original IDs, for example:
                # [1, 2, 3, 4, 5, 6] -> [0, 1, 2, 3, 4, 5]
                # [10, 20, 40]       -> [0, 1, 2]
                material_reindex_mapping = {
                    str(original_value.item() if hasattr(original_value, "item") else original_value): int(new_id)
                    for new_id, original_value in enumerate(unique_before)
                }

                # np.searchsorted is valid because unique_before is sorted.
                reindexed_cells = np.searchsorted(
                    unique_before,
                    material_values,
                ).astype(np.int32)
                mesh_reordered.cell_data[material_array] = reindexed_cells

                if "NodeMaterialIDs" in mesh_reordered.point_data:
                    node_values = np.asarray(
                        mesh_reordered.point_data["NodeMaterialIDs"]
                    ).copy()
                    reindexed_nodes = np.full(
                        node_values.shape,
                        -1,
                        dtype=np.int32,
                    )

                    # NodeMaterialIDs may contain -1 for unused nodes.
                    valid = np.isin(node_values, unique_before)
                    if np.any(valid):
                        reindexed_nodes[valid] = np.searchsorted(
                            unique_before,
                            node_values[valid],
                        ).astype(np.int32)

                    mesh_reordered.point_data[
                        "NodeMaterialIDs"
                    ] = reindexed_nodes

            # Remove mappings that became invalid during extraction/reordering.
            if remove_stale:
                mesh_reordered = remove_stale_mapping_arrays(
                    mesh_reordered
                )

            unique_after = np.unique(
                np.asarray(
                    mesh_reordered.cell_data[material_array]
                )
            )

            file_name = (
                str(
                    params.get("file_name")
                    or "repaired_reordered_voxel_model.vtu"
                ).strip()
                or "repaired_reordered_voxel_model.vtu"
            )
            if not file_name.lower().endswith(".vtu"):
                file_name = f"{Path(file_name).stem}.vtu"

            output_path = make_runtime_path(
                Path(file_name).stem,
                ".vtu",
            )
            mesh_reordered.save(output_path, binary=True)
            record = register_output_file(
                output_path,
                display_name=file_name,
            )

            preview_scalar = _choose_mesh_scalar(
                mesh_reordered,
                str(
                    params.get("preview_scalar")
                    or material_array
                ),
            )
            show_edges = _as_bool(
                params.get("show_edges"),
                True,
            )

            report = {
                "operation": "repair_reorder_voxel_mesh",
                "source": source_name,
                **source_meta,
                "material_array": material_array,
                "voxel_spacing": [
                    float(dx),
                    float(dy),
                    float(dz),
                ],
                "bottom_z": float(bottom_z),
                "grid_reference_mode": grid_reference_mode,
                "grid_reference_centers": list(
                    index_before_fill["grid_references"]
                ),
                "grid_axis_residuals": dict(
                    index_before_fill["axis_residuals"]
                ),
                "maximum_grid_residual": float(
                    index_before_fill["maximum_grid_residual"]
                ),
                "effective_grid_bottom_z": float(
                    index_before_fill["effective_grid_bottom_z"]
                ),
                "grid_bottom_offset_from_configured": float(
                    index_before_fill[
                        "grid_bottom_offset_from_configured"
                    ]
                ),
                "lowest_layers_to_repair": int(lowest_layers),
                "original_points": original_points,
                "original_cells": original_cells,
                "removed_cells_below_bottom": removed_below,
                "detected_bottom_missing_voxels": int(
                    len(missing_voxels)
                ),
                "added_patch_cells": int(patch.n_cells),
                "cells_after_filling": int(mesh_filled.n_cells),
                "removed_floating_columns": int(
                    removed_floating_columns
                ),
                "removed_floating_cells": int(
                    removed_floating_cells
                ),
                "removed_outer_columns": int(
                    removed_outer_columns
                ),
                "removed_outer_cells": int(
                    removed_outer_cells
                ),
                "final_points": int(mesh_reordered.n_points),
                "final_cells": int(mesh_reordered.n_cells),
                "materials_before_reindexing": [
                    int(v) for v in unique_before.tolist()
                ],
                "materials_after_reindexing": [
                    int(v) for v in unique_after.tolist()
                ],
                "reindexed_material_ids_from_zero": bool(
                    reindex_material_ids_from_zero
                ),
                "material_reindex_mapping": material_reindex_mapping,
                "cell_data": list(
                    mesh_reordered.cell_data.keys()
                ),
                "point_data": list(
                    mesh_reordered.point_data.keys()
                ),
                "field_data": list(
                    mesh_reordered.field_data.keys()
                ),
                "material_ranges": material_range_report(
                    mesh_reordered,
                    material_array,
                ),
                "bounds": [
                    float(v)
                    for v in mesh_reordered.bounds
                ],
                "file_name": file_name,
                "download_url": (
                    f"/api/download/{record['file_id']}"
                ),
                "pyvista_preview_url": (
                    f"/api/pyvista/mesh/{record['file_id']}"
                    f"?show_edges={str(show_edges).lower()}"
                    f"&scalars={quote(preview_scalar or '')}"
                ),
                "pyvista_button_label": (
                    "Open Repaired/Reordered Voxel Mesh 3D Popup"
                ),
                "web_scalar": preview_scalar or "",
                "web_show_edges": show_edges,
                "web_viewer_label": (
                    "Repaired and reordered voxel mesh"
                ),
                "note": (
                    "Cells are ordered by the original material value -> z -> y -> x. "
                    "When enabled, sorted unique material values are reindexed "
                    "consecutively from 0. Nodes are ordered by first occurrence "
                    "in the reordered connectivity. Existing bulk_* IDs are removed "
                    "because they become invalid after repair and reordering."
                ),
            }
            _attach_web_surface_preview(
                report,
                mesh_reordered,
                file_name,
                preferred_scalar=preview_scalar or "",
                show_edges=show_edges,
            )

            return {
                "mesh": RuntimeValue(
                    "mesh",
                    mesh_reordered,
                    name=file_name,
                    preview=report,
                    metadata={
                        "file_id": record["file_id"],
                        **report,
                    },
                ),
                "voxel_model": RuntimeValue(
                    "mesh",
                    mesh_reordered,
                    name=file_name,
                    preview=report,
                    metadata={
                        "file_id": record["file_id"],
                        **report,
                    },
                ),
                "file": RuntimeValue(
                    "file",
                    Path(record["path"]),
                    name=file_name,
                    preview=report,
                    metadata={
                        "file_id": record["file_id"],
                        **report,
                    },
                ),
                "report": RuntimeValue(
                    "report",
                    report,
                    name="repair_reorder_voxel_mesh_report",
                    preview=report,
                    metadata=report,
                ),
            }

        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError(
                f"Repair and Reorder Voxel Mesh failed: {exc}"
            ) from exc


class OGSIdentifyFullMeshNode(BaseNode):
    """Notebook Part 2: apply identifySubdomains to one full mesh."""

    type_name = "OGSIdentifyFullMesh"

    def run(
        self,
        inputs: Dict[str, Any],
        params: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, RuntimeValue]:
        _ensure_ogs_dependencies()

        mesh, source_name, source_meta = _resolve_mesh_or_file_input(
            inputs,
            params,
            input_port="mesh",
            file_param="mesh_file_id",
        )

        try:
            if not isinstance(mesh, pv.UnstructuredGrid):
                mesh = mesh.cast_to_unstructured_grid()

            file_name = (
                str(
                    params.get("file_name")
                    or "identified_full_mesh.vtu"
                ).strip()
                or "identified_full_mesh.vtu"
            )
            if not file_name.lower().endswith(".vtu"):
                file_name = f"{Path(file_name).stem}.vtu"

            search_length = float(
                _as_float(
                    params.get("search_length"),
                    1e-6,
                )
                or 1e-6
            )
            show_edges = _as_bool(
                params.get("show_edges"),
                True,
            )

            executable_text = str(
                params.get("executable_path") or ""
            ).strip()
            ogs_bin_text = str(
                params.get("ogs_bin_dir") or ""
            ).strip()

            if executable_text:
                executable = Path(
                    executable_text
                ).expanduser().resolve()
                if not executable.exists():
                    raise NodeExecutionError(
                        "identifySubdomains executable does not exist: "
                        f"{executable}"
                    )
                ogs_bin_dir = executable.parent
            else:
                ogs_bin_dir = (
                    Path(ogs_bin_text)
                    if ogs_bin_text
                    else None
                )
                executable = find_identify_subdomains_executable(
                    ogs_bin_dir
                )

            # Save a dedicated VTU input so connected mesh objects and all
            # VTK-family files are handled consistently.
            input_path = make_runtime_path(
                f"_ogs_identify_input_{Path(file_name).stem}",
                ".vtu",
            )
            mesh.save(input_path, binary=True)

            final_output_path = make_runtime_path(
                Path(file_name).stem,
                ".vtu",
            )

            # Use the notebook implementation. The same full mesh is passed as
            # both the bulk mesh and full-dimensional subdomain.
            result_path = apply_ogs_identify_to_full_mesh(
                repaired_mesh_path=input_path,
                final_output_path=final_output_path,
                ogs_bin_dir=ogs_bin_dir,
                search_length=search_length,
            )

            result = pv.read(result_path)
            if not isinstance(result, pv.UnstructuredGrid):
                result = result.cast_to_unstructured_grid()

            bulk_node_ids = np.asarray(
                result.point_data["bulk_node_ids"]
            ).reshape(-1)
            bulk_element_ids = np.asarray(
                result.cell_data["bulk_element_ids"]
            ).reshape(-1)

            expected_nodes = np.arange(
                result.n_points,
                dtype=np.uint64,
            )
            expected_cells = np.arange(
                result.n_cells,
                dtype=np.uint64,
            )

            node_identity = np.array_equal(
                bulk_node_ids.astype(np.uint64),
                expected_nodes,
            )
            element_identity = np.array_equal(
                bulk_element_ids.astype(np.uint64),
                expected_cells,
            )

            record = register_output_file(
                result_path,
                display_name=file_name,
            )
            preview_scalar = _choose_mesh_scalar(
                result,
                str(
                    params.get("preview_scalar")
                    or "MaterialIDs"
                ),
            )

            report = {
                "operation": "ogs_identify_full_mesh",
                "source": source_name,
                **source_meta,
                "executable": str(executable),
                "search_length": float(search_length),
                "bulk_and_subdomain_are_same_full_mesh": True,
                "n_points": int(result.n_points),
                "n_cells": int(result.n_cells),
                "bulk_node_ids_count": int(
                    bulk_node_ids.size
                ),
                "bulk_element_ids_count": int(
                    bulk_element_ids.size
                ),
                "bulk_node_ids_range": [
                    int(bulk_node_ids.min()),
                    int(bulk_node_ids.max()),
                ],
                "bulk_element_ids_range": [
                    int(bulk_element_ids.min()),
                    int(bulk_element_ids.max()),
                ],
                "node_mapping_is_identity": bool(
                    node_identity
                ),
                "element_mapping_is_identity": bool(
                    element_identity
                ),
                "point_data": list(
                    result.point_data.keys()
                ),
                "cell_data": list(
                    result.cell_data.keys()
                ),
                "field_data": list(
                    result.field_data.keys()
                ),
                "bounds": [
                    float(v)
                    for v in result.bounds
                ],
                "file_name": file_name,
                "download_url": (
                    f"/api/download/{record['file_id']}"
                ),
                "pyvista_preview_url": (
                    f"/api/pyvista/mesh/{record['file_id']}"
                    f"?show_edges={str(show_edges).lower()}"
                    f"&scalars={quote(preview_scalar or '')}"
                ),
                "pyvista_button_label": (
                    "Open OGS-Identified Full Mesh 3D Popup"
                ),
                "web_scalar": preview_scalar or "",
                "web_show_edges": show_edges,
                "web_viewer_label": (
                    "OGS identified full mesh"
                ),
                "note": (
                    "The same full-dimensional VTU is supplied as both the "
                    "OGS bulk mesh and subdomain. The output remains a full "
                    "3D mesh and receives bulk_node_ids and "
                    "bulk_element_ids."
                ),
            }
            _attach_web_surface_preview(
                report,
                result,
                file_name,
                preferred_scalar=preview_scalar or "",
                show_edges=show_edges,
            )

            return {
                "mesh": RuntimeValue(
                    "mesh",
                    result,
                    name=file_name,
                    preview=report,
                    metadata={
                        "file_id": record["file_id"],
                        **report,
                    },
                ),
                "file": RuntimeValue(
                    "file",
                    Path(record["path"]),
                    name=file_name,
                    preview=report,
                    metadata={
                        "file_id": record["file_id"],
                        **report,
                    },
                ),
                "report": RuntimeValue(
                    "report",
                    report,
                    name="ogs_identify_full_mesh_report",
                    preview=report,
                    metadata=report,
                ),
            }

        except NodeExecutionError:
            raise
        except subprocess.CalledProcessError as exc:
            raise NodeExecutionError(
                "OGS identifySubdomains failed. "
                f"Return code: {exc.returncode}. Command: {exc.cmd}"
            ) from exc
        except Exception as exc:
            raise NodeExecutionError(
                f"OGS Identify Full Mesh failed: {exc}"
            ) from exc
