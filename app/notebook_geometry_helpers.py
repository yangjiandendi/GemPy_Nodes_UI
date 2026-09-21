from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Iterable, Tuple

from pathlib import Path

import numpy as np

NodeExecutionError = ValueError

# Generated from nodes.py and voxel_repair_nodes.py; do not edit this snapshot manually.

def _as_bool(value: Any, default: bool=False) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}

def _as_int(value: Any, default: Optional[int]=None) -> Optional[int]:
    if value is None or value == '':
        return default
    return int(value)

def _as_float(value: Any, default: Optional[float]=None) -> Optional[float]:
    if value is None or value == '':
        return default
    return float(value)

def _choose_mesh_scalar(mesh: Any, requested: str='auto') -> str:
    cell_keys = [str(k) for k in getattr(mesh, 'cell_data', {}).keys()]
    point_keys = [str(k) for k in getattr(mesh, 'point_data', {}).keys()]
    keys = cell_keys + point_keys
    req = str(requested or 'auto').strip()
    if req and req.lower() not in {'auto', 'none', ''}:
        for key in keys:
            if key == req or key.lower() == req.lower():
                return key
        return ''
    priority = ['MaterialIDs', 'MaterialID', 'material_ids', 'material_id', 'id', 'ids', 'lith_block', 'lithology', 'layer', 'layer_id', 'BoundaryID']
    for p in priority:
        for key in keys:
            if key == p:
                return key
    for key in keys:
        if key.lower() not in {'cell_ids', 'cell_id', 'vtkoriginalcellids', 'vtkoriginalpointids'}:
            return key
    return keys[0] if keys else ''

def _polydata_face_lists(poly: Any) -> List[List[int]]:
    """Return PolyData polygon faces as lists of point ids."""
    faces: List[List[int]] = []
    try:
        raw = np.asarray(poly.faces, dtype=np.int64).ravel()
    except Exception:
        raw = np.asarray([], dtype=np.int64)
    i = 0
    while i < raw.size:
        n = int(raw[i])
        if n <= 0 or i + n >= raw.size + 1:
            break
        faces.append([int(v) for v in raw[i + 1:i + 1 + n].tolist()])
        i += n + 1
    return faces

def _faces_to_pyvista_array(faces: List[List[int]]) -> np.ndarray:
    out: List[int] = []
    for face in faces:
        if len(face) >= 3:
            out.append(int(len(face)))
            out.extend([int(v) for v in face])
    return np.asarray(out, dtype=np.int64)

def _boundary_edges_with_owner(faces: List[List[int]]) -> List[tuple[int, int, int]]:
    """Return boundary edges as (a, b, owner_cell_index)."""
    edges: Dict[tuple[int, int], List[tuple[int, int, int]]] = {}
    for (ci, face) in enumerate(faces):
        if len(face) < 3:
            continue
        for (j, a) in enumerate(face):
            b = face[(j + 1) % len(face)]
            key = (min(int(a), int(b)), max(int(a), int(b)))
            edges.setdefault(key, []).append((int(a), int(b), int(ci)))
    boundary: List[tuple[int, int, int]] = []
    for entries in edges.values():
        if len(entries) == 1:
            boundary.append(entries[0])
    return boundary

def _safe_cell_array_concat_for_thickening(arr: Any, owner_indices: List[int], n_faces: int) -> Optional[np.ndarray]:
    """Duplicate cell data for inner/outer faces and copy owner values to side walls."""
    try:
        a = np.asarray(arr)
        if a.shape[0] != n_faces:
            return None
        side = a[np.asarray(owner_indices, dtype=np.int64)] if owner_indices else a[:0]
        return np.concatenate([a, a, side], axis=0)
    except Exception:
        return None

def _build_normal_thickened_mesh(mesh: Any, *, distance: float, mode: str='symmetric', close_sides: bool=True, triangulate: bool=True, consistent_normals: bool=True, auto_orient_normals: bool=True, flip_normals: bool=False, clean_input: bool=True, clean_output: bool=True) -> tuple[Any, Dict[str, Any]]:
    """Thicken a surface mesh by offsetting points along point normals.

    The result is a PolyData shell surface. For open surfaces, boundary side
    faces are generated so the buffer becomes a closed shell surface.
    """
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f'PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.') from exc
    d = float(distance)
    if not np.isfinite(d) or abs(d) <= 0:
        raise NodeExecutionError('Buffer distance must be a non-zero finite number.')
    try:
        surf = mesh.extract_surface()
    except Exception:
        surf = mesh.copy(deep=True)
    if clean_input:
        try:
            surf = surf.clean(tolerance=0.0)
        except Exception:
            pass
    if triangulate:
        try:
            surf = surf.triangulate()
        except Exception:
            pass
    if int(getattr(surf, 'n_points', 0)) == 0 or int(getattr(surf, 'n_cells', 0)) == 0:
        raise NodeExecutionError('Input mesh has no surface points/cells to thicken.')
    try:
        surf_n = surf.compute_normals(point_normals=True, cell_normals=False, consistent_normals=bool(consistent_normals), auto_orient_normals=bool(auto_orient_normals), flip_normals=bool(flip_normals), inplace=False)
    except TypeError:
        surf_n = surf.compute_normals(point_normals=True, cell_normals=False, inplace=False)
    except Exception as exc:
        raise NodeExecutionError(f'Could not compute mesh normals for thickening: {exc}') from exc
    normals = None
    for key in ['Normals', 'PointNormals']:
        try:
            if key in surf_n.point_data:
                normals = np.asarray(surf_n.point_data[key], dtype=float)
                break
        except Exception:
            pass
    if normals is None or normals.shape[0] != int(surf_n.n_points):
        raise NodeExecutionError('Could not obtain point normals from the input mesh.')
    lens = np.linalg.norm(normals, axis=1)
    lens[lens == 0] = 1.0
    normals = normals / lens[:, None]
    pts = np.asarray(surf_n.points, dtype=float)
    mode = str(mode or 'symmetric').strip().lower()
    if mode in {'symmetric', 'both', 'centered'}:
        inner_pts = pts - normals * (d * 0.5)
        outer_pts = pts + normals * (d * 0.5)
    elif mode in {'outward', 'positive', 'one_sided_outward'}:
        inner_pts = pts.copy()
        outer_pts = pts + normals * d
    elif mode in {'inward', 'negative', 'one_sided_inward'}:
        inner_pts = pts - normals * d
        outer_pts = pts.copy()
    else:
        raise NodeExecutionError('Thicken mode must be symmetric, outward, or inward.')
    faces = _polydata_face_lists(surf_n)
    if not faces:
        raise NodeExecutionError('Could not parse polygon faces from the input surface mesh.')
    n_pts = int(surf_n.n_points)
    n_faces = len(faces)
    inner_faces = [[int(v) for v in reversed(face)] for face in faces]
    outer_faces = [[int(v) + n_pts for v in face] for face in faces]
    boundary_edges = _boundary_edges_with_owner(faces) if close_sides else []
    side_faces: List[List[int]] = []
    side_owner_indices: List[int] = []
    for (a, b, owner) in boundary_edges:
        side_faces.append([int(a), int(b), int(b) + n_pts, int(a) + n_pts])
        side_owner_indices.append(int(owner))
    all_points = np.vstack([inner_pts, outer_pts])
    all_faces = inner_faces + outer_faces + side_faces
    poly = pv.PolyData(all_points, _faces_to_pyvista_array(all_faces))
    for (key, arr) in getattr(surf_n, 'cell_data', {}).items():
        combined_arr = _safe_cell_array_concat_for_thickening(arr, side_owner_indices, n_faces)
        if combined_arr is not None and combined_arr.shape[0] == poly.n_cells:
            try:
                poly.cell_data[str(key)] = combined_arr
            except Exception:
                pass
    for (key, arr) in getattr(surf_n, 'point_data', {}).items():
        try:
            a = np.asarray(arr)
            if a.shape[0] == n_pts and str(key) not in {'Normals', 'PointNormals'}:
                poly.point_data[str(key)] = np.concatenate([a, a], axis=0)
        except Exception:
            pass
    part = np.concatenate([np.full(n_faces, 0, dtype=np.int32), np.full(n_faces, 1, dtype=np.int32), np.full(len(side_faces), 2, dtype=np.int32)])
    poly.cell_data['thickening_part'] = part
    poly.cell_data['buffer_distance'] = np.full(poly.n_cells, float(d), dtype=float)
    poly.field_data['thickening_reference_points'] = (inner_pts + outer_pts) * 0.5
    poly.field_data['thickening_reference_faces'] = np.asarray(surf_n.faces, dtype=np.int64)
    poly.field_data['thickening_half_width'] = np.asarray([abs(d) * 0.5])
    if clean_output:
        try:
            poly = poly.clean(tolerance=0.0)
        except Exception:
            pass
    metadata = {'operation': 'thicken_mesh_along_normals', 'input_surface_cells': int(n_faces), 'input_surface_points': int(n_pts), 'output_cells': int(getattr(poly, 'n_cells', 0)), 'output_points': int(getattr(poly, 'n_points', 0)), 'buffer_distance': float(d), 'mode': mode, 'close_sides': bool(close_sides), 'boundary_edges_closed': int(len(side_faces)), 'triangulate_input': bool(triangulate), 'cell_data': list(getattr(poly, 'cell_data', {}).keys()), 'point_data': list(getattr(poly, 'point_data', {}).keys()), 'bounds': [float(v) for v in getattr(poly, 'bounds', [])], 'note': 'Output is a thickened PolyData shell surface. Boundary side faces are added for open surfaces when Close sides is enabled.'}
    return (poly, metadata)

pv = None

binary_fill_holes = None

cKDTree = None

STALE_POINT_ARRAYS = {'bulk_node_ids', 'vtkOriginalPointIds', 'PointMergeMap'}

STALE_CELL_ARRAYS = {'bulk_element_ids', 'vtkOriginalCellIds', 'merge_source_index', 'merge_source_cell', 'number_bulk_elements'}

def _ensure_repair_dependencies() -> None:
    global pv, binary_fill_holes, cKDTree
    if pv is None:
        try:
            import pyvista as _pv
            from scipy.ndimage import binary_fill_holes as _binary_fill_holes
            from scipy.spatial import cKDTree as _cKDTree
        except Exception as exc:
            raise NodeExecutionError(f'Repair and Reorder Voxel Mesh requires pyvista and scipy. Import failed: {exc}') from exc
        pv = _pv
        binary_fill_holes = _binary_fill_holes
        cKDTree = _cKDTree

def _ensure_ogs_dependencies() -> None:
    global pv
    if pv is None:
        try:
            import pyvista as _pv
        except Exception as exc:
            raise NodeExecutionError(f'OGS Identify Full Mesh requires pyvista: {exc}') from exc
        pv = _pv

def get_uniform_voxel_cell_type(mesh: pv.UnstructuredGrid) -> pv.CellType:
    """
    Return the single supported 8-node volume cell type used by the mesh.

    VTK cell type 11 = VOXEL
    VTK cell type 12 = HEXAHEDRON
    """
    unique_types = np.unique(np.asarray(mesh.celltypes))
    if unique_types.size != 1:
        (unique_values, counts) = np.unique(np.asarray(mesh.celltypes), return_counts=True)
        raise ValueError(f'The mesh must use one uniform 8-node cell type. Found: {dict(zip(unique_values.tolist(), counts.tolist()))}')
    cell_type_value = int(unique_types[0])
    if cell_type_value == int(pv.CellType.VOXEL):
        return pv.CellType.VOXEL
    if cell_type_value == int(pv.CellType.HEXAHEDRON):
        return pv.CellType.HEXAHEDRON
    raise ValueError(f'Only VTK_VOXEL (11) and VTK_HEXAHEDRON (12) are supported. Found cell type: {cell_type_value}')

def infer_regular_spacing(values: np.ndarray, decimals: int=6) -> float:
    """Infer nominal regular-grid spacing from cell-center coordinates."""
    unique_values = np.unique(np.round(np.asarray(values), decimals))
    differences = np.diff(unique_values)
    differences = differences[differences > 10 ** (-decimals + 1)]
    if differences.size == 0:
        raise ValueError('Could not infer a positive grid spacing.')
    return float(np.median(differences))

def remove_stale_mapping_arrays(mesh: pv.UnstructuredGrid) -> pv.UnstructuredGrid:
    """Remove mappings that become invalid after repair or reordering."""
    cleaned = mesh.copy(deep=True)
    for name in list(cleaned.point_data.keys()):
        if name in STALE_POINT_ARRAYS:
            del cleaned.point_data[name]
    for name in list(cleaned.cell_data.keys()):
        if name in STALE_CELL_ARRAYS:
            del cleaned.cell_data[name]
    for name in list(cleaned.field_data.keys()):
        if name == 'bulk_element_ids':
            del cleaned.field_data[name]
    return cleaned

def remove_voxels_below_bottom(mesh: pv.UnstructuredGrid, bottom_z: float, dz: float, tolerance: float=1e-06) -> pv.UnstructuredGrid:
    """Keep cells whose lower face is at or above bottom_z."""
    centers_z = np.asarray(mesh.cell_centers().points)[:, 2]
    lower_face_z = centers_z - dz / 2.0
    keep = lower_face_z >= bottom_z - tolerance
    return mesh.extract_cells(np.flatnonzero(keep))

def infer_axis_grid_reference(values: np.ndarray, spacing: float) -> tuple[float, np.ndarray, float]:
    """Infer the phase/reference of one regular center-coordinate axis.

    A mesh can be perfectly regular while its center coordinates are shifted by
    a constant offset relative to a configured origin or bottom. The returned
    integer indices always start at zero for the minimum observed coordinate.
    """
    coordinates = np.asarray(values, dtype=float).reshape(-1)
    if coordinates.size == 0:
        raise ValueError('Cannot infer a grid reference from an empty coordinate axis.')
    spacing = float(spacing)
    if not np.isfinite(spacing) or spacing <= 0:
        raise ValueError(f'Grid spacing must be positive, got {spacing!r}.')
    anchor = float(np.min(coordinates))
    provisional_indices = np.rint((coordinates - anchor) / spacing).astype(np.int64)
    candidate_references = coordinates - provisional_indices.astype(float) * spacing
    reference = float(np.median(candidate_references))
    indices = np.rint((coordinates - reference) / spacing).astype(np.int64)
    minimum_index = int(indices.min())
    reference = float(reference + minimum_index * spacing)
    indices = indices - minimum_index
    reconstructed = reference + indices.astype(float) * spacing
    maximum_residual = float(np.max(np.abs(reconstructed - coordinates)))
    return (reference, indices, maximum_residual)

def build_voxel_index(mesh: pv.UnstructuredGrid, dx: float, dy: float, dz: float, bottom_z: float, tolerance: float=0.0001, reference_mode: str='auto_from_centers') -> dict:
    """Quantize cell centers onto a nominal regular voxel grid.

    ``auto_from_centers`` infers the actual X/Y/Z center-lattice phase from the
    input mesh. This is the recommended mode and accepts a regular grid whose
    bottom is shifted relative to ``bottom_z``.

    ``configured_bottom_strict`` preserves the original behavior and forces the
    first Z center to ``bottom_z + dz/2``.
    """
    centers = np.asarray(mesh.cell_centers().points, dtype=float)
    if centers.size == 0:
        raise ValueError('The voxel mesh contains no cells.')
    reference_mode = str(reference_mode or 'auto_from_centers').strip().lower()
    (x_reference, i, residual_x) = infer_axis_grid_reference(centers[:, 0], dx)
    (y_reference, j, residual_y) = infer_axis_grid_reference(centers[:, 1], dy)
    if reference_mode == 'configured_bottom_strict':
        z_reference = float(bottom_z + dz / 2.0)
        k = np.rint((centers[:, 2] - z_reference) / dz).astype(np.int64)
        reconstructed_z = z_reference + k.astype(float) * dz
        residual_z = float(np.max(np.abs(reconstructed_z - centers[:, 2])))
        if np.min(k) < 0:
            raise ValueError('Cells below the configured bottom remain in strict grid-reference mode.')
    elif reference_mode == 'auto_from_centers':
        (z_reference, k, residual_z) = infer_axis_grid_reference(centers[:, 2], dz)
    else:
        raise ValueError(f"Unknown grid reference mode {reference_mode!r}. Use 'auto_from_centers' or 'configured_bottom_strict'.")
    residuals = {'x': float(residual_x), 'y': float(residual_y), 'z': float(residual_z)}
    maximum_residual = float(max(residuals.values()))
    if maximum_residual > float(tolerance):
        raise ValueError(f'Cell centers do not fit a regular grid after reference inference. Maximum residual={maximum_residual:.9g}; per-axis residuals={residuals}; spacing={[float(dx), float(dy), float(dz)]}; references={[x_reference, y_reference, z_reference]}; tolerance={float(tolerance):.9g}.')
    nx = int(i.max()) + 1
    ny = int(j.max()) + 1
    nz = int(k.max()) + 1
    occupancy = np.zeros((nx, ny, nz), dtype=bool)
    cell_ids = np.full((nx, ny, nz), -1, dtype=np.int64)
    duplicate_count = 0
    for (cell_id, (ii, jj, kk)) in enumerate(zip(i, j, k)):
        if occupancy[ii, jj, kk]:
            duplicate_count += 1
            continue
        occupancy[ii, jj, kk] = True
        cell_ids[ii, jj, kk] = cell_id
    if duplicate_count:
        raise ValueError(f'Found {duplicate_count} duplicate voxel locations after regular-grid quantization.')
    effective_grid_bottom_z = float(z_reference - dz / 2.0)
    return {'centers': centers, 'x_reference': float(x_reference), 'y_reference': float(y_reference), 'z_reference': float(z_reference), 'dx': float(dx), 'dy': float(dy), 'dz': float(dz), 'nx': nx, 'ny': ny, 'nz': nz, 'occupancy': occupancy, 'cell_ids': cell_ids, 'i': i, 'j': j, 'k': k, 'reference_mode': reference_mode, 'grid_references': [float(x_reference), float(y_reference), float(z_reference)], 'axis_residuals': residuals, 'maximum_grid_residual': maximum_residual, 'configured_bottom_z': float(bottom_z), 'effective_grid_bottom_z': effective_grid_bottom_z, 'grid_bottom_offset_from_configured': float(effective_grid_bottom_z - bottom_z)}

def find_source_cell(i: int, j: int, k: int, occupancy: np.ndarray, cell_ids: np.ndarray, maximum_xy_radius: int=20) -> int:
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
    (nx, ny, _) = occupancy.shape
    for radius in range(1, maximum_xy_radius + 1):
        candidates = []
        for di in range(-radius, radius + 1):
            for dj in range(-radius, radius + 1):
                if max(abs(di), abs(dj)) != radius:
                    continue
                (ni, nj) = (i + di, j + dj)
                if 0 <= ni < nx and 0 <= nj < ny:
                    candidates.append((ni, nj))
        for (ni, nj) in candidates:
            if occupancy[ni, nj, k]:
                return int(cell_ids[ni, nj, k])
        best_source = None
        best_vertical_distance = None
        for (ni, nj) in candidates:
            neighbor_k = np.flatnonzero(occupancy[ni, nj, :])
            if neighbor_k.size == 0:
                continue
            nearest_k = neighbor_k[np.argmin(np.abs(neighbor_k - k))]
            vertical_distance = abs(int(nearest_k) - k)
            if best_vertical_distance is None or vertical_distance < best_vertical_distance:
                best_vertical_distance = vertical_distance
                best_source = int(cell_ids[ni, nj, nearest_k])
        if best_source is not None:
            return best_source
    raise RuntimeError(f'No source cell found near voxel index ({i}, {j}, {k}).')

def detect_bottom_repair_voxels(info: dict, lowest_layers: int) -> list[tuple[int, int, int]]:
    """
    Detect missing voxels near the bottom.

    Empty bottom columns connected to the outside are not filled.
    Enclosed bottom holes are filled continuously.
    """
    occupancy = info['occupancy']
    (nx, ny, nz) = occupancy.shape
    number_of_low_layers = min(lowest_layers, nz)
    bottom_occupied = occupancy[:, :, 0]
    filled_bottom_footprint = binary_fill_holes(bottom_occupied)
    enclosed_bottom_holes = filled_bottom_footprint & ~bottom_occupied
    missing = []
    for i in range(nx):
        for j in range(ny):
            existing_k = np.flatnonzero(occupancy[i, j, :])
            if bottom_occupied[i, j]:
                maximum_existing_k = int(existing_k.max())
                upper_k = min(number_of_low_layers - 1, maximum_existing_k)
                for k in range(upper_k + 1):
                    if not occupancy[i, j, k]:
                        missing.append((i, j, k))
                continue
            if not enclosed_bottom_holes[i, j]:
                continue
            if existing_k.size:
                first_existing_k = int(existing_k.min())
                maximum_existing_k = int(existing_k.max())
                inspected_upper = min(number_of_low_layers - 1, maximum_existing_k)
                connection_upper = first_existing_k - 1
                upper_k = max(inspected_upper, connection_upper)
            else:
                upper_k = number_of_low_layers - 1
            for k in range(upper_k + 1):
                if not occupancy[i, j, k]:
                    missing.append((i, j, k))
    return missing

def create_voxel_patch(mesh: pv.UnstructuredGrid, info: dict, missing_voxels: list[tuple[int, int, int]], maximum_xy_radius: int=20) -> pv.UnstructuredGrid:
    """Create missing HEX8 cells and inherit data from nearby cells/points."""
    if not missing_voxels:
        return pv.UnstructuredGrid()
    x_ref = info['x_reference']
    y_ref = info['y_reference']
    z_ref = info['z_reference']
    dx = info['dx']
    dy = info['dy']
    dz = info['dz']
    occupancy = info['occupancy']
    cell_ids = info['cell_ids']
    points = []
    connectivity = []
    source_cell_ids = []
    mesh_cell_type = get_uniform_voxel_cell_type(mesh)
    for (i, j, k) in missing_voxels:
        xc = x_ref + i * dx
        yc = y_ref + j * dy
        zc = z_ref + k * dz
        (x0, x1) = (xc - dx / 2.0, xc + dx / 2.0)
        (y0, y1) = (yc - dy / 2.0, yc + dy / 2.0)
        (z0, z1) = (zc - dz / 2.0, zc + dz / 2.0)
        base = len(points)
        if mesh_cell_type == pv.CellType.VOXEL:
            voxel_points = [[x0, y0, z0], [x1, y0, z0], [x0, y1, z0], [x1, y1, z0], [x0, y0, z1], [x1, y0, z1], [x0, y1, z1], [x1, y1, z1]]
        else:
            voxel_points = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
        points.extend(voxel_points)
        connectivity.append([base, base + 1, base + 2, base + 3, base + 4, base + 5, base + 6, base + 7])
        source_cell_ids.append(find_source_cell(i, j, k, occupancy, cell_ids, maximum_xy_radius=maximum_xy_radius))
    patch_points = np.asarray(points, dtype=float)
    patch_connectivity = np.asarray(connectivity, dtype=np.int64)
    patch = pv.UnstructuredGrid({mesh_cell_type: patch_connectivity}, patch_points)
    source_cell_ids = np.asarray(source_cell_ids, dtype=np.int64)
    for name in mesh.cell_data.keys():
        if name in STALE_CELL_ARRAYS:
            continue
        patch.cell_data[name] = np.asarray(mesh.cell_data[name])[source_cell_ids].copy()
    if mesh.point_data:
        point_tree = cKDTree(np.asarray(mesh.points))
        (_, nearest_point_ids) = point_tree.query(patch_points, k=1)
        for name in mesh.point_data.keys():
            if name in STALE_POINT_ARRAYS:
                continue
            patch.point_data[name] = np.asarray(mesh.point_data[name])[nearest_point_ids].copy()
    return patch

def append_patch(mesh: pv.UnstructuredGrid, patch: pv.UnstructuredGrid, merge_point_tolerance: float=1e-08) -> pv.UnstructuredGrid:
    """Append patch cells and merge coincident grid points."""
    if patch.n_cells == 0:
        return mesh.copy(deep=True)
    merged = mesh.merge(patch, merge_points=True, tolerance=float(merge_point_tolerance))
    for name in mesh.field_data.keys():
        if name not in merged.field_data:
            merged.field_data[name] = np.asarray(mesh.field_data[name]).copy()
    return merged

def remove_floating_columns(mesh: pv.UnstructuredGrid, dx: float, dy: float, dz: float, bottom_z: float, tolerance: float=0.0001, reference_mode: str='auto_from_centers') -> tuple[pv.UnstructuredGrid, int, int]:
    """Delete complete columns whose lowest voxel does not rest on bottom_z."""
    info = build_voxel_index(mesh, dx, dy, dz, bottom_z, tolerance=tolerance, reference_mode=reference_mode)
    occupancy = info['occupancy']
    column_has_bottom = occupancy[:, :, 0]
    keep_cells = column_has_bottom[info['i'], info['j']]
    floating_columns = occupancy.any(axis=2) & ~column_has_bottom
    cleaned = mesh.extract_cells(np.flatnonzero(keep_cells))
    return (cleaned, int(np.count_nonzero(~keep_cells)), int(np.count_nonzero(floating_columns)))

def remove_one_outer_material_layer(mesh: pv.UnstructuredGrid, dx: float, dy: float, dz: float, bottom_z: float, material_array: str, material_id: int, tolerance: float=0.0001, reference_mode: str='auto_from_centers') -> tuple[pv.UnstructuredGrid, int, int]:
    """
    Remove one initial outer layer of pure-material columns.

    This is a single pass. Newly exposed columns are not checked again.
    """
    info = build_voxel_index(mesh, dx, dy, dz, bottom_z, tolerance=tolerance, reference_mode=reference_mode)
    occupancy = info['occupancy']
    cell_ids = info['cell_ids']
    footprint = occupancy.any(axis=2)
    padded = np.pad(footprint, 1, mode='constant', constant_values=False)
    west_missing = ~padded[:-2, 1:-1]
    east_missing = ~padded[2:, 1:-1]
    south_missing = ~padded[1:-1, :-2]
    north_missing = ~padded[1:-1, 2:]
    outer = footprint & (west_missing | east_missing | south_missing | north_missing)
    materials = np.asarray(mesh.cell_data[material_array]).reshape(-1)
    remove_column = np.zeros_like(footprint, dtype=bool)
    for (i, j) in np.argwhere(outer):
        ids = cell_ids[i, j, :]
        ids = ids[ids >= 0]
        if ids.size and np.all(materials[ids] == material_id):
            remove_column[i, j] = True
    remove_cells = remove_column[info['i'], info['j']]
    cleaned = mesh.extract_cells(np.flatnonzero(~remove_cells))
    return (cleaned, int(np.count_nonzero(remove_cells)), int(np.count_nonzero(remove_column)))

def first_occurrence_node_order(sorted_connectivity: np.ndarray, number_of_points: int, chunk_size: int=2000000) -> tuple[np.ndarray, np.ndarray]:
    """Order nodes by first occurrence in the reordered cell connectivity."""
    flat = sorted_connectivity.reshape(-1)
    sentinel = np.iinfo(np.int64).max
    first_position = np.full(number_of_points, sentinel, dtype=np.int64)
    for start in range(0, flat.size, chunk_size):
        stop = min(start + chunk_size, flat.size)
        ids = flat[start:stop]
        positions = np.arange(start, stop, dtype=np.int64)
        np.minimum.at(first_position, ids, positions)
    used_nodes = np.flatnonzero(first_position != sentinel)
    used_nodes = used_nodes[np.argsort(first_position[used_nodes], kind='stable')]
    unused_nodes = np.flatnonzero(first_position == sentinel)
    return (np.concatenate((used_nodes, unused_nodes)), first_position)

def reorder_mesh_by_material(mesh: pv.UnstructuredGrid, material_array: str, add_node_material_ids: bool=True, keep_original_order_ids: bool=False) -> pv.UnstructuredGrid:
    """
    Reorder cells by MaterialID -> z -> y -> x.

    Reorder nodes by first occurrence in those reordered cells.
    No bulk IDs are written in Part 1.
    """
    mesh = remove_stale_mapping_arrays(mesh)
    if material_array not in mesh.cell_data:
        raise KeyError(f"Missing cell-data array '{material_array}'.")
    mesh_cell_type = get_uniform_voxel_cell_type(mesh)
    connectivity = np.asarray(mesh.cell_connectivity, dtype=np.int64).reshape(mesh.n_cells, 8)
    centers = np.asarray(mesh.cell_centers().points)
    materials = np.asarray(mesh.cell_data[material_array]).reshape(-1)
    cell_order = np.lexsort((centers[:, 0], centers[:, 1], centers[:, 2], materials))
    sorted_connectivity_old = connectivity[cell_order]
    sorted_materials = materials[cell_order]
    (node_order, first_position) = first_occurrence_node_order(sorted_connectivity_old, mesh.n_points)
    old_to_new_node = np.empty(mesh.n_points, dtype=np.int64)
    old_to_new_node[node_order] = np.arange(mesh.n_points, dtype=np.int64)
    sorted_connectivity_new = old_to_new_node[sorted_connectivity_old]
    sorted_points = np.asarray(mesh.points)[node_order]
    reordered = pv.UnstructuredGrid({mesh_cell_type: sorted_connectivity_new}, sorted_points)
    for name in mesh.point_data.keys():
        if name in STALE_POINT_ARRAYS:
            continue
        reordered.point_data[name] = np.asarray(mesh.point_data[name])[node_order]
    for name in mesh.cell_data.keys():
        if name in STALE_CELL_ARRAYS:
            continue
        reordered.cell_data[name] = np.asarray(mesh.cell_data[name])[cell_order]
    for name in mesh.field_data.keys():
        reordered.field_data[name] = np.asarray(mesh.field_data[name]).copy()
    if add_node_material_ids:
        sentinel = np.iinfo(np.int64).max
        used_old_nodes = np.flatnonzero(first_position != sentinel)
        old_node_material = np.full(mesh.n_points, -1, dtype=sorted_materials.dtype)
        first_cell_position = first_position[used_old_nodes] // 8
        old_node_material[used_old_nodes] = sorted_materials[first_cell_position]
        reordered.point_data['NodeMaterialIDs'] = old_node_material[node_order]
    if keep_original_order_ids:
        reordered.point_data['OriginalNodeIDs'] = node_order.astype(np.uint64)
        reordered.cell_data['OriginalElementIDs'] = cell_order.astype(np.uint64)
    return reordered

def editor_repair(mesh, params):
    _ensure_repair_dependencies()
    if not isinstance(mesh, pv.UnstructuredGrid):
        mesh = mesh.cast_to_unstructured_grid()
    material_array = str(params.get('material_array') or 'MaterialIDs').strip() or 'MaterialIDs'
    bottom_z = float(_as_float(params.get('bottom_z'), -200.0))
    lowest_layers = int(_as_int(params.get('lowest_layers_to_repair'), 20) or 20)
    spacing_decimals = int(_as_int(params.get('spacing_decimals'), 6) or 6)
    grid_tolerance = float(_as_float(params.get('grid_tolerance'), 0.0001) or 0.0001)
    grid_reference_mode = str(params.get('grid_reference_mode') or 'auto_from_centers').strip().lower()
    bottom_tolerance = float(_as_float(params.get('bottom_tolerance'), 1e-06) or 1e-06)
    maximum_xy_radius = int(_as_int(params.get('maximum_xy_source_radius'), 20) or 20)
    merge_point_tolerance = float(_as_float(params.get('merge_point_tolerance'), 1e-08) or 1e-08)
    repair_bottom_holes = _as_bool(params.get('repair_bottom_holes'), True)
    remove_floating = _as_bool(params.get('remove_floating_columns'), True)
    remove_outer = _as_bool(params.get('remove_one_outer_material_layer'), True)
    outer_material_id = int(_as_int(params.get('outer_layer_material_id'), 1) or 1)
    add_node_material_ids = _as_bool(params.get('add_node_material_ids'), True)
    keep_original_order_ids = _as_bool(params.get('keep_original_order_ids'), False)
    remove_stale = _as_bool(params.get('remove_stale_mapping_arrays'), True)
    reindex_material_ids_from_zero = _as_bool(params.get('reindex_material_ids_from_zero', params.get('convert_material_ids', True)), True)
    if remove_stale:
        mesh_original = remove_stale_mapping_arrays(mesh)
    else:
        mesh_original = mesh.copy(deep=True)
    if material_array not in mesh_original.cell_data:
        raise NodeExecutionError(f"'{material_array}' not found. Available cell arrays: {list(mesh_original.cell_data.keys())}")
    get_uniform_voxel_cell_type(mesh_original)
    centers_original = np.asarray(mesh_original.cell_centers().points)
    dx = infer_regular_spacing(centers_original[:, 0], decimals=spacing_decimals)
    dy = infer_regular_spacing(centers_original[:, 1], decimals=spacing_decimals)
    dz = infer_regular_spacing(centers_original[:, 2], decimals=spacing_decimals)
    original_points = int(mesh_original.n_points)
    original_cells = int(mesh_original.n_cells)
    mesh_clipped = remove_voxels_below_bottom(mesh_original, bottom_z=bottom_z, dz=dz, tolerance=bottom_tolerance)
    if mesh_clipped.n_cells == 0:
        raise NodeExecutionError('All cells were removed by the configured bottom clipping.')
    removed_below = int(original_cells - mesh_clipped.n_cells)
    index_before_fill = build_voxel_index(mesh_clipped, dx=dx, dy=dy, dz=dz, bottom_z=bottom_z, tolerance=grid_tolerance, reference_mode=grid_reference_mode)
    missing_voxels: List[tuple[int, int, int]] = []
    if repair_bottom_holes:
        missing_voxels = detect_bottom_repair_voxels(index_before_fill, lowest_layers=lowest_layers)
    patch = create_voxel_patch(mesh_clipped, index_before_fill, missing_voxels, maximum_xy_radius=maximum_xy_radius)
    mesh_filled = append_patch(mesh_clipped, patch, merge_point_tolerance=merge_point_tolerance)
    removed_floating_cells = 0
    removed_floating_columns = 0
    if remove_floating:
        (mesh_supported, removed_floating_cells, removed_floating_columns) = remove_floating_columns(mesh_filled, dx=dx, dy=dy, dz=dz, bottom_z=bottom_z, tolerance=grid_tolerance, reference_mode=grid_reference_mode)
    else:
        mesh_supported = mesh_filled
    removed_outer_cells = 0
    removed_outer_columns = 0
    if remove_outer:
        (mesh_cleaned, removed_outer_cells, removed_outer_columns) = remove_one_outer_material_layer(mesh_supported, dx=dx, dy=dy, dz=dz, bottom_z=bottom_z, material_array=material_array, material_id=outer_material_id, tolerance=grid_tolerance, reference_mode=grid_reference_mode)
    else:
        mesh_cleaned = mesh_supported
    if mesh_cleaned.n_cells == 0:
        raise NodeExecutionError('The selected repair settings removed all cells.')
    mesh_reordered = reorder_mesh_by_material(mesh_cleaned, material_array=material_array, add_node_material_ids=add_node_material_ids, keep_original_order_ids=keep_original_order_ids)
    material_values = np.asarray(mesh_reordered.cell_data[material_array]).copy()
    unique_before = np.unique(material_values)
    material_reindex_mapping: Dict[str, int] = {}
    if reindex_material_ids_from_zero:
        material_reindex_mapping = {str(original_value.item() if hasattr(original_value, 'item') else original_value): int(new_id) for (new_id, original_value) in enumerate(unique_before)}
        reindexed_cells = np.searchsorted(unique_before, material_values).astype(np.int32)
        mesh_reordered.cell_data[material_array] = reindexed_cells
        if 'NodeMaterialIDs' in mesh_reordered.point_data:
            node_values = np.asarray(mesh_reordered.point_data['NodeMaterialIDs']).copy()
            reindexed_nodes = np.full(node_values.shape, -1, dtype=np.int32)
            valid = np.isin(node_values, unique_before)
            if np.any(valid):
                reindexed_nodes[valid] = np.searchsorted(unique_before, node_values[valid]).astype(np.int32)
            mesh_reordered.point_data['NodeMaterialIDs'] = reindexed_nodes
    if remove_stale:
        mesh_reordered = remove_stale_mapping_arrays(mesh_reordered)
    unique_after = np.unique(np.asarray(mesh_reordered.cell_data[material_array]))
    return mesh_reordered

class ExtractVoxelBoundariesNode:

    @staticmethod
    def infer_regular_spacing_from_centers(values: Any, decimals: int=8, min_diff: float=1e-05) -> float:
        """Infer true regular-grid spacing while ignoring tiny coordinate noise.

        The old Extract Voxel Boundaries logic used every rounded unique cell
        center coordinate as a grid line. Very small floating-point deviations
        could therefore create fake columns/rows and shift indices. This helper
        instead uses the median of real coordinate gaps, ignoring tiny gaps such
        as 1e-7.
        """
        vals = np.unique(np.round(np.asarray(values, dtype=float), int(decimals)))
        if vals.size <= 1:
            return 1.0
        diffs = np.diff(vals)
        diffs = diffs[np.isfinite(diffs) & (diffs > float(min_diff))]
        return float(np.median(diffs)) if diffs.size else 1.0

    @staticmethod
    def build_voxel_index_map(mesh: Any, cell_data_name: str='MaterialIDs', decimals: int=8) -> Dict[str, Any]:
        centers = np.asarray(mesh.cell_centers().points, dtype=float)
        if centers.size == 0:
            raise NodeExecutionError('Cannot extract voxel boundaries from an empty mesh.')
        dx = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 0], decimals)
        dy = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 1], decimals)
        dz = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 2], decimals)
        xmin_c = float(np.round(np.nanmin(centers[:, 0]), int(decimals)))
        ymin_c = float(np.round(np.nanmin(centers[:, 1]), int(decimals)))
        zmin_c = float(np.round(np.nanmin(centers[:, 2]), int(decimals)))
        ii = np.rint((centers[:, 0] - xmin_c) / dx).astype(int)
        jj = np.rint((centers[:, 1] - ymin_c) / dy).astype(int)
        kk = np.rint((centers[:, 2] - zmin_c) / dz).astype(int)
        ii -= int(ii.min()) if ii.size and ii.min() < 0 else 0
        jj -= int(jj.min()) if jj.size and jj.min() < 0 else 0
        kk -= int(kk.min()) if kk.size and kk.min() < 0 else 0
        (nx, ny, nz) = (int(ii.max()) + 1, int(jj.max()) + 1, int(kk.max()) + 1)
        occ = np.zeros((nx, ny, nz), dtype=bool)
        cell_ids = -np.ones((nx, ny, nz), dtype=int)
        duplicate_centers = 0
        for (cid, (i, j, k)) in enumerate(zip(ii, jj, kk)):
            (i, j, k) = (int(i), int(j), int(k))
            if occ[i, j, k]:
                duplicate_centers += 1
                continue
            occ[i, j, k] = True
            cell_ids[i, j, k] = int(cid)
        origin = (xmin_c - dx / 2.0, ymin_c - dy / 2.0, zmin_c - dz / 2.0)
        xs = origin[0] + (np.arange(nx, dtype=float) + 0.5) * dx
        ys = origin[1] + (np.arange(ny, dtype=float) + 0.5) * dy
        zs = origin[2] + (np.arange(nz, dtype=float) + 0.5) * dz
        return {'xs': xs, 'ys': ys, 'zs': zs, 'nx': nx, 'ny': ny, 'nz': nz, 'dx': float(dx), 'dy': float(dy), 'dz': float(dz), 'origin': origin, 'occ': occ, 'cell_ids': cell_ids, 'cell_data_name': cell_data_name, 'grid_indexing': 'quantized_from_median_spacing', 'duplicate_quantized_centers': int(duplicate_centers), 'occupied_voxels': int(occ.sum()), 'input_cells': int(mesh.n_cells)}

    @staticmethod
    def collect_input_cell_scalars(mesh: Any) -> Dict[str, np.ndarray]:
        """Collect all input cell-data scalar arrays that can be transferred to boundary faces.

        Boundary faces are generated from source voxel cells, so cell-data arrays
        can be copied unambiguously via the source cell id. 1D and multi-column
        cell arrays are both supported as long as their first dimension matches
        mesh.n_cells.
        """
        arrays: Dict[str, np.ndarray] = {}
        n_cells = int(getattr(mesh, 'n_cells', 0))
        for (name, values) in getattr(mesh, 'cell_data', {}).items():
            try:
                arr = np.asarray(values)
                if arr.shape[0] == n_cells:
                    arrays[str(name)] = arr
            except Exception:
                pass
        return arrays

    @staticmethod
    def boundary_arrays_from_source_cells(mesh: Any, source_cell_ids: List[int], preferred_first: Optional[str]=None) -> Dict[str, Any]:
        """Create boundary cell arrays by copying all input cell-data scalars.

        Each generated boundary face stores the scalar values of the voxel cell
        from which the face was generated.
        """
        scalars = ExtractVoxelBoundariesNode.collect_input_cell_scalars(mesh)
        if preferred_first and preferred_first in scalars:
            scalars = {preferred_first: scalars[preferred_first], **{k: v for (k, v) in scalars.items() if k != preferred_first}}
        ids = np.asarray(source_cell_ids, dtype=np.int64)
        out: Dict[str, Any] = {}
        if ids.size == 0:
            for (name, arr) in scalars.items():
                out[name] = np.asarray(arr[:0])
            return out
        valid = (ids >= 0) & (ids < int(getattr(mesh, 'n_cells', 0)))
        for (name, arr) in scalars.items():
            try:
                shape = (ids.size,) + tuple(np.asarray(arr).shape[1:])
                copied = np.empty(shape, dtype=np.asarray(arr).dtype)
                if valid.all():
                    copied = np.asarray(arr)[ids]
                else:
                    copied[:] = 0
                    copied[valid] = np.asarray(arr)[ids[valid]]
                out[name] = copied
            except Exception:
                pass
        return out

    @staticmethod
    def collect_input_point_scalars(mesh: Any) -> Dict[str, np.ndarray]:
        """Collect all input point-data arrays that can be transferred.

        Arrays are accepted when their first dimension equals mesh.n_points.
        Multi-component arrays, such as vectors, are preserved.
        """
        arrays: Dict[str, np.ndarray] = {}
        n_points = int(getattr(mesh, 'n_points', 0))
        for (name, values) in getattr(mesh, 'point_data', {}).items():
            try:
                arr = np.asarray(values)
                if arr.shape[0] == n_points:
                    arrays[str(name)] = arr
            except Exception:
                pass
        return arrays

    @staticmethod
    def _fill_value_for_dtype(dtype: np.dtype) -> Any:
        """Return a safe fill value for unmatched boundary points."""
        try:
            if np.issubdtype(dtype, np.floating):
                return np.nan
            if np.issubdtype(dtype, np.complexfloating):
                return np.nan + 0j
            if np.issubdtype(dtype, np.bool_):
                return False
            if np.issubdtype(dtype, np.integer):
                return 0
            if np.issubdtype(dtype, np.str_) or np.issubdtype(dtype, np.bytes_):
                return ''
        except Exception:
            pass
        return 0

    @staticmethod
    def map_boundary_points_to_input_points(mesh: Any, boundary: Any, info: Dict[str, Any]) -> tuple[np.ndarray, Dict[str, Any]]:
        """Map generated boundary vertices back to original input mesh points.

        Boundary vertices lie on the regular voxel-grid vertices. Both input and
        boundary coordinates are therefore quantized with the same inferred
        voxel spacing/origin. This is robust to the tiny floating-point noise
        already handled by the boundary cell indexing fix.

        If more than one input point collapses to the same grid vertex, the first
        matching input point is used. Such duplicates are common in uncleaned
        unstructured voxel grids and are reported.
        """
        input_points = np.asarray(getattr(mesh, 'points', np.empty((0, 3))), dtype=float)
        boundary_points = np.asarray(getattr(boundary, 'points', np.empty((0, 3))), dtype=float)
        n_boundary = int(boundary_points.shape[0])
        if n_boundary == 0:
            return (np.empty((0,), dtype=np.int64), {'boundary_points': 0, 'matched_points': 0, 'unmatched_points': 0, 'duplicate_input_grid_vertices': 0, 'mapping': 'quantized_grid_vertex'})
        if input_points.shape[0] == 0:
            return (np.full(n_boundary, -1, dtype=np.int64), {'boundary_points': n_boundary, 'matched_points': 0, 'unmatched_points': n_boundary, 'duplicate_input_grid_vertices': 0, 'mapping': 'quantized_grid_vertex'})
        (dx, dy, dz) = (float(info['dx']), float(info['dy']), float(info['dz']))
        (x0, y0, z0) = [float(v) for v in info['origin']]
        (nx, ny, nz) = (int(info['nx']), int(info['ny']), int(info['nz']))
        spacing = np.asarray([dx, dy, dz], dtype=float)
        origin = np.asarray([x0, y0, z0], dtype=float)
        input_ijk = np.rint((input_points - origin[None, :]) / spacing[None, :]).astype(np.int64)
        boundary_ijk = np.rint((boundary_points - origin[None, :]) / spacing[None, :]).astype(np.int64)
        sx = int(nx + 1)
        sy = int(ny + 1)
        sz = int(nz + 1)
        input_valid = (input_ijk[:, 0] >= 0) & (input_ijk[:, 0] < sx) & (input_ijk[:, 1] >= 0) & (input_ijk[:, 1] < sy) & (input_ijk[:, 2] >= 0) & (input_ijk[:, 2] < sz)
        boundary_valid = (boundary_ijk[:, 0] >= 0) & (boundary_ijk[:, 0] < sx) & (boundary_ijk[:, 1] >= 0) & (boundary_ijk[:, 1] < sy) & (boundary_ijk[:, 2] >= 0) & (boundary_ijk[:, 2] < sz)
        valid_input_ids = np.where(input_valid)[0].astype(np.int64)
        valid_input_ijk = input_ijk[input_valid]
        input_keys = (valid_input_ijk[:, 0] + sx * (valid_input_ijk[:, 1] + sy * valid_input_ijk[:, 2])).astype(np.int64)
        order = np.argsort(input_keys, kind='mergesort')
        sorted_keys = input_keys[order]
        sorted_ids = valid_input_ids[order]
        (unique_keys, unique_first) = np.unique(sorted_keys, return_index=True)
        unique_ids = sorted_ids[unique_first]
        duplicate_count = int(sorted_keys.size - unique_keys.size)
        mapped = np.full(n_boundary, -1, dtype=np.int64)
        valid_boundary_ids = np.where(boundary_valid)[0].astype(np.int64)
        valid_boundary_ijk = boundary_ijk[boundary_valid]
        boundary_keys = (valid_boundary_ijk[:, 0] + sx * (valid_boundary_ijk[:, 1] + sy * valid_boundary_ijk[:, 2])).astype(np.int64)
        pos = np.searchsorted(unique_keys, boundary_keys)
        found = pos < unique_keys.size
        if found.any():
            found_indices = np.where(found)[0]
            found[found_indices] = unique_keys[pos[found_indices]] == boundary_keys[found_indices]
        mapped_valid = np.full(valid_boundary_ids.size, -1, dtype=np.int64)
        mapped_valid[found] = unique_ids[pos[found]]
        mapped[valid_boundary_ids] = mapped_valid
        missing = np.where(mapped < 0)[0]
        fallback_matches = 0
        if missing.size:
            try:
                from scipy.spatial import cKDTree
                tree = cKDTree(input_points)
                (distances, nearest) = tree.query(boundary_points[missing], k=1)
                tolerance = max(min(dx, dy, dz) * 1e-05, 1e-08)
                accept = np.isfinite(distances) & (distances <= tolerance)
                mapped[missing[accept]] = np.asarray(nearest, dtype=np.int64)[accept]
                fallback_matches = int(np.count_nonzero(accept))
            except Exception:
                pass
        matched = int(np.count_nonzero(mapped >= 0))
        return (mapped, {'boundary_points': n_boundary, 'matched_points': matched, 'unmatched_points': int(n_boundary - matched), 'fallback_nearest_matches': fallback_matches, 'duplicate_input_grid_vertices': duplicate_count, 'mapping': 'quantized_grid_vertex'})

    @classmethod
    def transfer_input_point_data(cls, mesh: Any, boundary: Any, info: Dict[str, Any]) -> Dict[str, Any]:
        """Copy every compatible input point-data array to a boundary mesh."""
        point_scalars = cls.collect_input_point_scalars(mesh)
        (source_point_ids, mapping_report) = cls.map_boundary_points_to_input_points(mesh, boundary, info)
        copied_names: List[str] = []
        if int(getattr(boundary, 'n_points', 0)) == 0:
            return {**mapping_report, 'copied_point_data': copied_names}
        valid = source_point_ids >= 0
        for (name, values) in point_scalars.items():
            try:
                arr = np.asarray(values)
                out_shape = (source_point_ids.size,) + tuple(arr.shape[1:])
                out = np.empty(out_shape, dtype=arr.dtype)
                out[...] = cls._fill_value_for_dtype(arr.dtype)
                if valid.any():
                    out[valid] = arr[source_point_ids[valid]]
                boundary.point_data[str(name)] = out
                copied_names.append(str(name))
            except Exception:
                pass
        return {**mapping_report, 'copied_point_data': copied_names}

    @staticmethod
    def build_polydata_from_quads(quads: List[np.ndarray], cell_arrays: Optional[Dict[str, Any]]=None):
        import pyvista as pv
        if len(quads) == 0:
            poly = pv.PolyData()
            if cell_arrays is not None:
                for (name, vals) in cell_arrays.items():
                    arr = np.asarray(vals)
                    poly.cell_data[name] = np.array([], dtype=arr.dtype if arr.size > 0 else float)
            return poly
        pts_all = []
        cells_all = []
        offset = 0
        for q in quads:
            pts_all.append(q)
            cells_all.append(np.hstack([[4], np.arange(offset, offset + 4)]))
            offset += 4
        pts_all = np.vstack(pts_all)
        cells_all = np.hstack(cells_all)
        poly = pv.PolyData(pts_all, cells_all)
        if cell_arrays is not None:
            for (name, vals) in cell_arrays.items():
                poly.cell_data[name] = np.asarray(vals)
        try:
            poly = poly.clean(tolerance=0.0)
        except TypeError:
            poly = poly.clean()
        return poly

    @classmethod
    def extract_top_bottom(cls, info: Dict[str, Any], mesh: Any, cell_data_name: str='MaterialIDs', add_top_risers: bool=True):
        occ = info['occ']
        cell_ids = info['cell_ids']
        (nx, ny, nz) = (info['nx'], info['ny'], info['nz'])
        (dx, dy, dz) = (info['dx'], info['dy'], info['dz'])
        (x0, y0, z0) = info['origin']
        (top_quads, bot_quads) = ([], [])
        top_source_cells: List[int] = []
        bot_source_cells: List[int] = []

        def voxel_bounds(i, j, k):
            xmin = x0 + i * dx
            xmax = xmin + dx
            ymin = y0 + j * dy
            ymax = ymin + dy
            zmin = z0 + k * dz
            zmax = zmin + dz
            return (xmin, xmax, ymin, ymax, zmin, zmax)
        top_k = -np.ones((nx, ny), dtype=int)
        bot_k = -np.ones((nx, ny), dtype=int)
        for i in range(nx):
            for j in range(ny):
                ks = np.where(occ[i, j, :])[0]
                if len(ks) == 0:
                    continue
                k_top = ks.max()
                k_bot = ks.min()
                top_k[i, j] = k_top
                bot_k[i, j] = k_bot
                (xmin, xmax, ymin, ymax, zmin, zmax) = voxel_bounds(i, j, k_top)
                top_quads.append(np.array([[xmin, ymin, zmax], [xmax, ymin, zmax], [xmax, ymax, zmax], [xmin, ymax, zmax]]))
                top_source_cells.append(int(cell_ids[i, j, k_top]))
                (xmin, xmax, ymin, ymax, zmin, zmax) = voxel_bounds(i, j, k_bot)
                bot_quads.append(np.array([[xmin, ymin, zmin], [xmin, ymax, zmin], [xmax, ymax, zmin], [xmax, ymin, zmin]]))
                bot_source_cells.append(int(cell_ids[i, j, k_bot]))
        if add_top_risers:
            for i in range(nx - 1):
                for j in range(ny):
                    k1 = top_k[i, j]
                    k2 = top_k[i + 1, j]
                    if k1 < 0 or k2 < 0 or k1 == k2:
                        continue
                    x = x0 + (i + 1) * dx
                    ymin = y0 + j * dy
                    ymax = ymin + dy
                    k_low = min(k1, k2)
                    k_high = max(k1, k2)
                    z_low = z0 + (k_low + 1) * dz
                    z_high = z0 + (k_high + 1) * dz
                    if z_high > z_low:
                        for k in range(k_low + 1, k_high + 1):
                            owner = int(cell_ids[i if k1 > k2 else i + 1, j, k])
                            if owner < 0:
                                continue
                            (lo, hi) = (z0 + k * dz, z0 + (k + 1) * dz)
                            quad = np.array([[x, ymin, lo], [x, ymax, lo], [x, ymax, hi], [x, ymin, hi]])
                            top_quads.append(quad if k1 > k2 else quad[::-1])
                            top_source_cells.append(owner)
            for i in range(nx):
                for j in range(ny - 1):
                    k1 = top_k[i, j]
                    k2 = top_k[i, j + 1]
                    if k1 < 0 or k2 < 0 or k1 == k2:
                        continue
                    xmin = x0 + i * dx
                    xmax = xmin + dx
                    y = y0 + (j + 1) * dy
                    k_low = min(k1, k2)
                    k_high = max(k1, k2)
                    z_low = z0 + (k_low + 1) * dz
                    z_high = z0 + (k_high + 1) * dz
                    if z_high > z_low:
                        for k in range(k_low + 1, k_high + 1):
                            owner = int(cell_ids[i, j if k1 > k2 else j + 1, k])
                            if owner < 0:
                                continue
                            (lo, hi) = (z0 + k * dz, z0 + (k + 1) * dz)
                            quad = np.array([[xmin, y, lo], [xmax, y, lo], [xmax, y, hi], [xmin, y, hi]])
                            top_quads.append(quad[::-1] if k1 > k2 else quad)
                            top_source_cells.append(owner)
        top_arrays = cls.boundary_arrays_from_source_cells(mesh, top_source_cells, preferred_first=cell_data_name)
        bot_arrays = cls.boundary_arrays_from_source_cells(mesh, bot_source_cells, preferred_first=cell_data_name)
        top = cls.build_polydata_from_quads(top_quads, top_arrays)
        bottom = cls.build_polydata_from_quads(bot_quads, bot_arrays)
        return (top, bottom)

    @staticmethod
    def build_footprint_boundary_edges(info: Dict[str, Any]):
        occ = info['occ']
        (nx, ny, nz) = (info['nx'], info['ny'], info['nz'])
        footprint = occ.any(axis=2)
        edges = []
        for i in range(nx):
            for j in range(ny):
                if not footprint[i, j]:
                    continue
                if j == 0 or not footprint[i, j - 1]:
                    edges.append({'start': (i, j), 'end': (i + 1, j), 'column': (i, j), 'local_face': 'south'})
                if i == nx - 1 or not footprint[i + 1, j]:
                    edges.append({'start': (i + 1, j), 'end': (i + 1, j + 1), 'column': (i, j), 'local_face': 'east'})
                if j == ny - 1 or not footprint[i, j + 1]:
                    edges.append({'start': (i + 1, j + 1), 'end': (i, j + 1), 'column': (i, j), 'local_face': 'north'})
                if i == 0 or not footprint[i - 1, j]:
                    edges.append({'start': (i, j + 1), 'end': (i, j), 'column': (i, j), 'local_face': 'west'})
        return (footprint, edges)

    @staticmethod
    def polygon_area_xy(points: np.ndarray) -> float:
        x = points[:, 0]
        y = points[:, 1]
        return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)

    @staticmethod
    def trace_cycles_from_oriented_edges(edges: List[Dict[str, Any]]):
        start_to_edge = {}
        for (eid, e) in enumerate(edges):
            start_to_edge.setdefault(e['start'], []).append(eid)
        used = np.zeros(len(edges), dtype=bool)
        cycles = []
        for eid0 in range(len(edges)):
            if used[eid0]:
                continue
            (cycle_edge_ids, cycle_vertices) = ([], [])
            eid = eid0
            v_start = edges[eid]['start']
            while True:
                if used[eid]:
                    break
                used[eid] = True
                e = edges[eid]
                cycle_edge_ids.append(eid)
                if len(cycle_vertices) == 0:
                    cycle_vertices.append(e['start'])
                cycle_vertices.append(e['end'])
                v = e['end']
                if v == v_start:
                    break
                candidates = start_to_edge.get(v, [])
                next_unused = [cid for cid in candidates if not used[cid]]
                if len(next_unused) == 0:
                    raise NodeExecutionError('Boundary tracing failed: open contour encountered.')
                if len(next_unused) > 1:
                    raise NodeExecutionError('Boundary tracing failed: ambiguous next edge encountered.')
                eid = next_unused[0]
            cycles.append({'edge_ids': cycle_edge_ids, 'vertices': cycle_vertices})
        return cycles

    @staticmethod
    def rotate_to_min_gap(angles: np.ndarray) -> int:
        diffs = np.abs(np.diff(np.r_[angles, angles[0] + 2 * np.pi]))
        return int(np.argmax(diffs) + 1) % len(angles)

    @staticmethod
    def angle_in_ccw_interval(a: float, start: float, end: float) -> bool:
        if start <= end:
            return start <= a < end
        return a >= start or a < end

    @classmethod
    def classify_cycle_edges_into_four_sides(cls, cycle: Dict[str, Any], info: Dict[str, Any], angle_offset_deg: float=0.0, south_east_boundary_deg: float=-49.0, east_north_boundary_deg: float=45.0, north_west_boundary_deg: float=135.0, west_south_boundary_deg: float=226.0) -> Dict[str, List[int]]:
        (x0, y0, _) = info['origin']
        (dx, dy) = (info['dx'], info['dy'])
        edge_ids = cycle['edge_ids']
        verts = cycle['vertices'][:-1]
        m = len(edge_ids)
        verts_phys = np.array([[x0 + u * dx, y0 + v * dy] for (u, v) in verts])
        cx = verts_phys[:, 0].mean()
        cy = verts_phys[:, 1].mean()
        edge_midpoints = []
        for i in range(m):
            p0 = verts_phys[i]
            p1 = verts_phys[(i + 1) % m]
            edge_midpoints.append(0.5 * (p0 + p1))
        edge_midpoints = np.array(edge_midpoints)
        angles = np.arctan2(edge_midpoints[:, 1] - cy, edge_midpoints[:, 0] - cx)
        k0 = cls.rotate_to_min_gap(angles)
        edge_ids_rot = edge_ids[k0:] + edge_ids[:k0]
        mids_rot = np.vstack([edge_midpoints[k0:], edge_midpoints[:k0]])
        angles_rot = np.arctan2(mids_rot[:, 1] - cy, mids_rot[:, 0] - cx)
        offset = np.deg2rad(angle_offset_deg)
        b_se = (np.deg2rad(south_east_boundary_deg) - offset) % (2 * np.pi)
        b_en = (np.deg2rad(east_north_boundary_deg) - offset) % (2 * np.pi)
        b_nw = (np.deg2rad(north_west_boundary_deg) - offset) % (2 * np.pi)
        b_ws = (np.deg2rad(west_south_boundary_deg) - offset) % (2 * np.pi)
        side_groups = {'north': [], 'south': [], 'east': [], 'west': []}
        for (eid, ang) in zip(edge_ids_rot, angles_rot):
            a = ang % (2 * np.pi)
            if cls.angle_in_ccw_interval(a, b_se, b_en):
                side_groups['east'].append(eid)
            elif cls.angle_in_ccw_interval(a, b_en, b_nw):
                side_groups['north'].append(eid)
            elif cls.angle_in_ccw_interval(a, b_nw, b_ws):
                side_groups['west'].append(eid)
            else:
                side_groups['south'].append(eid)
        return side_groups

    @classmethod
    def extract_side_meshes_from_outer_contours(cls, info: Dict[str, Any], mesh: Any, cell_data_name: str='MaterialIDs') -> Dict[str, Any]:
        (footprint, edges) = cls.build_footprint_boundary_edges(info)
        cycles = cls.trace_cycles_from_oriented_edges(edges)
        outer_cycles = []
        (x0, y0, _) = info['origin']
        (dx, dy) = (info['dx'], info['dy'])
        for cyc in cycles:
            verts = cyc['vertices'][:-1]
            verts_phys = np.array([[x0 + u * dx, y0 + v * dy] for (u, v) in verts])
            area = cls.polygon_area_xy(verts_phys)
            if area > 0:
                outer_cycles.append(cyc)
        occ = info['occ']
        cell_ids = info['cell_ids']
        dz = info['dz']
        z0 = info['origin'][2]
        side_quads = {'north': [], 'south': [], 'east': [], 'west': []}
        side_source_cells = {'north': [], 'south': [], 'east': [], 'west': []}

        def vertex_phys(v):
            (u, vv) = v
            return np.array([x0 + u * dx, y0 + vv * dy])
        for cyc in outer_cycles:
            side_edge_groups = cls.classify_cycle_edges_into_four_sides(cyc, info)
            for (side_name, edge_ids) in side_edge_groups.items():
                for eid in edge_ids:
                    e = edges[eid]
                    (i, j) = e['column']
                    p0_xy = vertex_phys(e['start'])
                    p1_xy = vertex_phys(e['end'])
                    ks = np.where(occ[i, j, :])[0]
                    if len(ks) == 0:
                        continue
                    for k in ks:
                        zmin = z0 + k * dz
                        zmax = zmin + dz
                        q = np.array([[p0_xy[0], p0_xy[1], zmin], [p1_xy[0], p1_xy[1], zmin], [p1_xy[0], p1_xy[1], zmax], [p0_xy[0], p0_xy[1], zmax]])
                        side_quads[side_name].append(q)
                        side_source_cells[side_name].append(int(cell_ids[i, j, k]))
        return {side_name: cls.build_polydata_from_quads(side_quads[side_name], cls.boundary_arrays_from_source_cells(mesh, side_source_cells[side_name], preferred_first=cell_data_name)) for side_name in ['north', 'south', 'east', 'west']}

    @classmethod
    def extract_boundaries_from_voxel_grid(cls, mesh: Any, cell_data_name: str='MaterialIDs', decimals: int=8, add_top_risers: bool=True) -> Dict[str, Any]:
        info = cls.build_voxel_index_map(mesh, cell_data_name=cell_data_name, decimals=decimals)
        (top, bottom) = cls.extract_top_bottom(info, mesh, cell_data_name=cell_data_name, add_top_risers=add_top_risers)
        side_meshes = cls.extract_side_meshes_from_outer_contours(info, mesh, cell_data_name=cell_data_name)
        boundaries = {'top': top, 'bottom': bottom, 'north': side_meshes['north'], 'south': side_meshes['south'], 'east': side_meshes['east'], 'west': side_meshes['west']}
        boundary_id_map = {'top': 1, 'bottom': 2, 'north': 3, 'south': 4, 'east': 5, 'west': 6}
        for (name, bnd) in boundaries.items():
            bnd.cell_data['BoundaryID'] = np.full(bnd.n_cells, boundary_id_map[name], dtype=np.int32)
            point_report = cls.transfer_input_point_data(mesh, bnd, info)
            try:
                bnd._node_editor_point_data_transfer_report = point_report
            except Exception:
                pass
        return boundaries
