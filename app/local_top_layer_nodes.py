from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .models import NodeExecutionError, RuntimeValue
from .storage import get_file_path, register_output_file
from .nodes import (
    BaseNode,
    _as_bool,
    _as_float,
    _attach_web_surface_preview,
    _choose_mesh_scalar,
    _many,
    _mesh_from_runtime_value,
    _mesh_preview,
    _save_pyvista_mesh_compatible,
)

_TEMP_DEPTH_POINT = "__local_depth_below_dem_point__"
_TEMP_DEPTH_CELL = "__local_depth_below_dem_cell__"


def _copy_mesh(mesh: Any) -> Any:
    try:
        return mesh.copy(deep=True)
    except Exception:
        return mesh.copy()


def _resolve_mesh(
    inputs: Dict[str, Any],
    input_name: str,
    file_id: str,
    *,
    required: bool,
) -> Tuple[Optional[Any], str, Dict[str, Any]]:
    """Resolve a mesh from a connected RuntimeValue or a file selector."""
    values = _many(inputs, input_name)
    if len(values) > 1:
        raise NodeExecutionError(
            f"Input port {input_name} expects one mesh, got {len(values)}. "
            "Combine meshes first when several parts must be processed together."
        )
    if values:
        mesh, name, meta = _mesh_from_runtime_value(values[0])
        return mesh, name, meta

    selected = str(file_id or "").strip()
    if selected:
        try:
            import pyvista as pv
        except Exception as exc:
            raise NodeExecutionError(
                f"PyVista is not installed/importable: {exc}. Install requirements.txt."
            ) from exc
        path = get_file_path(selected)
        try:
            mesh = pv.read(str(path))
        except Exception as exc:
            raise NodeExecutionError(f"Could not read selected mesh {path}: {exc}") from exc
        return mesh, path.name, {
            "source_kind": "file_select",
            "source_name": path.name,
            "file_id": selected,
        }

    if required:
        raise NodeExecutionError(
            f"Missing required input: connect {input_name} or choose a mesh file."
        )
    return None, "", {}


def _offset_topography_vertical(
    topography_mesh: Any,
    thickness: float,
    *,
    triangulate: bool = True,
    clean: bool = False,
) -> Tuple[Any, Dict[str, Any]]:
    """Create the local cutoff surface Z_cutoff(X,Y) = Z_DEM(X,Y) - thickness."""
    d = float(thickness)
    if not np.isfinite(d) or d <= 0.0:
        raise NodeExecutionError("Top-layer thickness must be a finite number greater than zero.")

    try:
        surface = topography_mesh.extract_surface()
    except Exception:
        surface = _copy_mesh(topography_mesh)

    if triangulate:
        try:
            surface = surface.triangulate()
        except Exception:
            pass

    if int(getattr(surface, "n_points", 0)) <= 0:
        raise NodeExecutionError("The DEM/topography mesh contains no points.")

    lowered = _copy_mesh(surface)
    original_points = np.asarray(surface.points, dtype=float)
    if original_points.ndim != 2 or original_points.shape[1] < 3:
        raise NodeExecutionError("The DEM/topography mesh does not contain XYZ coordinates.")
    if not np.isfinite(original_points[:, :3]).all():
        raise NodeExecutionError("The DEM/topography mesh contains non-finite XYZ coordinates.")

    lowered_points = original_points.copy()
    lowered_points[:, 2] -= d
    lowered.points = lowered_points

    updated_arrays = []
    for key in list(getattr(lowered, "point_data", {}).keys()):
        try:
            values = np.asarray(lowered.point_data[key])
        except Exception:
            continue
        if values.ndim != 1 or values.shape[0] != lowered_points.shape[0]:
            continue
        if not np.issubdtype(values.dtype, np.number):
            continue
        # Only shift a scalar when it actually stores the original point Z.
        if np.allclose(values.astype(float), original_points[:, 2], rtol=0.0, atol=1e-7):
            lowered.point_data[key] = values.astype(float) - d
            updated_arrays.append(str(key))

    try:
        lowered.point_data["local_cutoff_z"] = lowered_points[:, 2].copy()
        lowered.point_data["removed_top_thickness"] = np.full(
            lowered_points.shape[0], d, dtype=float
        )
    except Exception:
        pass

    if clean:
        try:
            lowered = lowered.clean(tolerance=0.0)
        except TypeError:
            lowered = lowered.clean()
        except Exception:
            pass

    metadata = {
        "operation": "offset_dem_vertically_for_local_top_removal",
        "top_layer_thickness": d,
        "offset_direction": "negative_global_z",
        "input_z_min": float(np.min(original_points[:, 2])),
        "input_z_max": float(np.max(original_points[:, 2])),
        "cutoff_z_min": float(np.min(lowered_points[:, 2])),
        "cutoff_z_max": float(np.max(lowered_points[:, 2])),
        "n_points": int(lowered_points.shape[0]),
        "n_cells": int(getattr(lowered, "n_cells", 0)),
        "updated_z_like_point_arrays": updated_arrays,
        "formula": "local_cutoff_z(x, y) = dem_z(x, y) - top_layer_thickness",
    }
    return lowered, metadata


def _build_dem_sampler(
    topography_mesh: Any,
    method: str = "auto",
):
    """Return a chunkable function that samples DEM Z at XY coordinates.

    A regular-grid interpolator is preferred for raster-like DEM meshes. An
    arbitrary triangulated surface falls back to a cKDTree nearest-point lookup.
    """
    try:
        from scipy.interpolate import RegularGridInterpolator
        from scipy.spatial import cKDTree
    except Exception as exc:
        raise NodeExecutionError(
            f"SciPy interpolation is required for local DEM thresholding: {exc}"
        ) from exc

    points = np.asarray(topography_mesh.points, dtype=float)
    if points.ndim != 2 or points.shape[1] < 3 or points.shape[0] == 0:
        raise NodeExecutionError("The DEM/topography mesh has no usable XYZ points.")

    finite = np.isfinite(points[:, 0]) & np.isfinite(points[:, 1]) & np.isfinite(points[:, 2])
    points = points[finite, :3]
    if points.shape[0] == 0:
        raise NodeExecutionError("The DEM/topography mesh has no finite XYZ points.")

    x = points[:, 0]
    y = points[:, 1]
    z = points[:, 2]
    bounds = [float(x.min()), float(x.max()), float(y.min()), float(y.max())]
    requested = str(method or "auto").strip().lower()
    if requested not in {"auto", "regular_grid", "nearest"}:
        raise NodeExecutionError("DEM sampling method must be auto, regular_grid, or nearest.")

    regular_error = ""
    if requested in {"auto", "regular_grid"}:
        try:
            unique_x, inverse_x = np.unique(x, return_inverse=True)
            unique_y, inverse_y = np.unique(y, return_inverse=True)
            expected = int(unique_x.size) * int(unique_y.size)
            if expected != int(points.shape[0]):
                raise ValueError(
                    f"unique X/Y product {expected} differs from point count {points.shape[0]}"
                )
            z_grid = np.full((unique_y.size, unique_x.size), np.nan, dtype=float)
            z_grid[inverse_y, inverse_x] = z
            if not np.isfinite(z_grid).all():
                raise ValueError("the X/Y grid contains duplicates or missing positions")
            interpolator = RegularGridInterpolator(
                (unique_y, unique_x),
                z_grid,
                method="linear",
                bounds_error=False,
                fill_value=np.nan,
            )

            def sample_regular(xy: np.ndarray, chunk_size: int = 500_000) -> np.ndarray:
                query = np.asarray(xy, dtype=float)
                result = np.full(query.shape[0], np.nan, dtype=float)
                for start in range(0, query.shape[0], int(chunk_size)):
                    stop = min(start + int(chunk_size), query.shape[0])
                    block = query[start:stop]
                    result[start:stop] = interpolator(
                        np.column_stack([block[:, 1], block[:, 0]])
                    )
                return result

            dx = float(np.median(np.diff(unique_x))) if unique_x.size > 1 else None
            dy = float(np.median(np.diff(unique_y))) if unique_y.size > 1 else None
            return sample_regular, {
                "sampling_method": "regular_grid_linear",
                "dem_points_used": int(points.shape[0]),
                "dem_grid_shape": [int(unique_y.size), int(unique_x.size)],
                "dem_dx": dx,
                "dem_dy": dy,
                "dem_xy_bounds": bounds,
            }
        except Exception as exc:
            regular_error = str(exc)
            if requested == "regular_grid":
                raise NodeExecutionError(
                    "DEM sampling was forced to regular_grid, but the DEM is not a complete "
                    f"regular XY lattice: {exc}"
                ) from exc

    tree = cKDTree(np.column_stack([x, y]))

    def sample_nearest(xy: np.ndarray, chunk_size: int = 500_000) -> np.ndarray:
        query = np.asarray(xy, dtype=float)
        result = np.full(query.shape[0], np.nan, dtype=float)
        inside = (
            (query[:, 0] >= bounds[0])
            & (query[:, 0] <= bounds[1])
            & (query[:, 1] >= bounds[2])
            & (query[:, 1] <= bounds[3])
        )
        ids = np.flatnonzero(inside)
        for start in range(0, ids.size, int(chunk_size)):
            block_ids = ids[start : start + int(chunk_size)]
            _distance, nearest = tree.query(query[block_ids], k=1, workers=-1)
            result[block_ids] = z[np.asarray(nearest, dtype=np.int64)]
        return result

    return sample_nearest, {
        "sampling_method": "nearest_dem_point",
        "dem_points_used": int(points.shape[0]),
        "dem_grid_shape": None,
        "dem_xy_bounds": bounds,
        "regular_grid_fallback_reason": regular_error or None,
    }


def _threshold_keep_upper(
    mesh: Any,
    *,
    value: float,
    scalars: str,
    preference: str,
    all_scalars: bool,
) -> Any:
    """Keep scalar values >= value across supported PyVista versions."""
    kwargs = {
        "value": float(value),
        "scalars": scalars,
        "preference": preference,
        "continuous": False,
    }
    if preference == "point":
        kwargs["all_scalars"] = bool(all_scalars)
    try:
        return mesh.threshold(method="upper", **kwargs)
    except TypeError:
        # Older PyVista versions accept a [lower, upper] range instead of method.
        return mesh.threshold(value=[float(value), np.inf], **kwargs)


def _remove_local_top_layer(
    base_mesh: Any,
    topography_mesh: Any,
    *,
    thickness: float,
    sampling_method: str = "auto",
    selection_mode: str = "cell_center",
    crop_to_dem_xy: bool = True,
    clean_output: bool = True,
) -> Tuple[Any, Any, Dict[str, Any]]:
    """Remove cells within a locally measured thickness below the DEM.

    For each sampled position, depth_below_dem = DEM_Z(X,Y) - mesh_Z. Cells are
    retained only when the selected depth criterion is >= thickness.
    """
    d = float(thickness)
    lowered_dem, offset_meta = _offset_topography_vertical(
        topography_mesh, d, triangulate=True, clean=False
    )
    sampler, sampler_meta = _build_dem_sampler(topography_mesh, sampling_method)

    work = _copy_mesh(base_mesh)
    input_cells = int(getattr(work, "n_cells", 0))
    input_points = int(getattr(work, "n_points", 0))
    if input_cells <= 0 or input_points <= 0:
        raise NodeExecutionError("The input mesh contains no cells or points.")

    mode = str(selection_mode or "cell_center").strip().lower()
    if mode not in {"cell_center", "all_points", "any_point"}:
        raise NodeExecutionError(
            "Selection mode must be cell_center, all_points, or any_point."
        )

    dem_bounds = sampler_meta["dem_xy_bounds"]
    operations = [
        "sample local DEM elevation",
        f"depth_below_dem = dem_z(x,y) - mesh_z",
        f"keep depth_below_dem >= {d:g}",
    ]

    if mode == "cell_center":
        centers = np.asarray(work.cell_centers().points, dtype=float)
        dem_z = sampler(centers[:, :2])
        depth = dem_z - centers[:, 2]
        depth[~np.isfinite(depth)] = -np.inf
        work.cell_data[_TEMP_DEPTH_CELL] = depth
        result = _threshold_keep_upper(
            work,
            value=d,
            scalars=_TEMP_DEPTH_CELL,
            preference="cell",
            all_scalars=False,
        )
        valid_samples = int(np.isfinite(dem_z).sum())
        depth_values = depth[np.isfinite(depth)]
        criterion_note = "cell center depth below the local DEM"
    else:
        points = np.asarray(work.points, dtype=float)
        dem_z = sampler(points[:, :2])
        depth = dem_z - points[:, 2]
        depth[~np.isfinite(depth)] = -np.inf
        work.point_data[_TEMP_DEPTH_POINT] = depth
        result = _threshold_keep_upper(
            work,
            value=d,
            scalars=_TEMP_DEPTH_POINT,
            preference="point",
            all_scalars=(mode == "all_points"),
        )
        valid_samples = int(np.isfinite(dem_z).sum())
        depth_values = depth[np.isfinite(depth)]
        criterion_note = (
            "all cell vertices are at least the requested depth below the local DEM"
            if mode == "all_points"
            else "at least one cell vertex is at least the requested depth below the local DEM"
        )

    for association in [getattr(result, "point_data", {}), getattr(result, "cell_data", {})]:
        for key in [_TEMP_DEPTH_POINT, _TEMP_DEPTH_CELL]:
            try:
                if key in association:
                    del association[key]
            except Exception:
                pass

    if crop_to_dem_xy:
        try:
            centers = np.asarray(result.cell_centers().points, dtype=float)
            inside = (
                (centers[:, 0] >= float(dem_bounds[0]))
                & (centers[:, 0] <= float(dem_bounds[1]))
                & (centers[:, 1] >= float(dem_bounds[2]))
                & (centers[:, 1] <= float(dem_bounds[3]))
            )
            result = result.extract_cells(np.flatnonzero(inside))
            operations.append("crop retained cells to DEM XY bounds")
        except Exception:
            # Samples outside the DEM were already assigned -inf and removed.
            pass

    if int(getattr(result, "n_cells", 0)) <= 0:
        raise NodeExecutionError(
            "The local top-layer threshold removed all cells. Check mesh/DEM coordinate "
            "systems, units, thickness, and selection mode."
        )

    if clean_output:
        try:
            result = result.clean(
                tolerance=0.0,
                remove_unused_points=True,
                average_point_data=False,
            )
        except TypeError:
            try:
                result = result.clean()
            except Exception:
                pass
        except Exception:
            pass

    output_cells = int(getattr(result, "n_cells", 0))
    output_points = int(getattr(result, "n_points", 0))
    finite_depth = depth_values[np.isfinite(depth_values)]
    report = {
        "operation": "remove_local_top_layer_by_dem",
        "top_layer_thickness": d,
        "selection_mode": mode,
        "selection_criterion": criterion_note,
        "crop_to_dem_xy": bool(crop_to_dem_xy),
        "clean_output": bool(clean_output),
        "input_cells": input_cells,
        "output_cells": output_cells,
        "removed_cells": input_cells - output_cells,
        "input_points": input_points,
        "output_points": output_points,
        "valid_dem_samples": valid_samples,
        "sampled_depth_min": float(finite_depth.min()) if finite_depth.size else None,
        "sampled_depth_max": float(finite_depth.max()) if finite_depth.size else None,
        "output_bounds": [float(v) for v in getattr(result, "bounds", [])],
        "operations": operations,
        "local_cutoff_surface": offset_meta,
        "dem_sampling": sampler_meta,
        "formula": "keep cells where DEM_Z(X,Y) - mesh_Z >= top_layer_thickness",
        "note": (
            "This is a local topographic threshold. Every XY location uses its own "
            "DEM elevation; no single global cutoff Z is used."
        ),
    }
    return result, lowered_dem, report


class RemoveLocalTopLayerNode(BaseNode):
    type_name = "RemoveLocalTopLayer"

    def run(
        self,
        inputs: Dict[str, Any],
        params: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, RuntimeValue]:
        try:
            import pyvista as pv  # noqa: F401
        except Exception as exc:
            raise NodeExecutionError(
                f"PyVista is not installed/importable: {exc}. Install requirements.txt."
            ) from exc

        thickness = _as_float(params.get("top_layer_thickness"), 20.0)
        if thickness is None:
            thickness = 20.0

        topography, topography_name, topography_source = _resolve_mesh(
            inputs,
            "topography_mesh",
            str(params.get("topography_mesh_file_id") or ""),
            required=True,
        )
        base_mesh, base_name, base_source = _resolve_mesh(
            inputs,
            "input_mesh",
            str(params.get("input_mesh_file_id") or ""),
            required=False,
        )

        lowered_dem, offset_meta = _offset_topography_vertical(
            topography,
            float(thickness),
            triangulate=_as_bool(params.get("triangulate_dem"), True),
            clean=_as_bool(params.get("clean_lowered_dem"), False),
        )

        lowered_name = str(
            params.get("lowered_dem_file_name") or "dem_local_top_minus_20m.vtp"
        ).strip() or "dem_local_top_minus_20m.vtp"
        lowered_path, lowered_final_name, lowered_adjustment = _save_pyvista_mesh_compatible(
            lowered_dem, lowered_name
        )
        lowered_record = register_output_file(
            lowered_path, display_name=lowered_final_name
        )

        if base_mesh is None:
            result = lowered_dem
            report = {
                "operation": "create_local_cutoff_dem_only",
                "input_mode": "dem_only",
                "topography_source": topography_source,
                "topography_name": topography_name,
                "local_cutoff_surface": offset_meta,
                "note": (
                    "No volume mesh was connected. The main mesh output is the DEM "
                    "shifted vertically downward by the requested local top-layer thickness."
                ),
            }
            result_path = lowered_path
            result_name = lowered_final_name
            result_record = lowered_record
            result_adjustment = lowered_adjustment
        else:
            result, lowered_dem, report = _remove_local_top_layer(
                base_mesh,
                topography,
                thickness=float(thickness),
                sampling_method=str(params.get("dem_sampling_method") or "auto"),
                selection_mode=str(params.get("selection_mode") or "cell_center"),
                crop_to_dem_xy=_as_bool(params.get("crop_to_dem_xy"), True),
                clean_output=_as_bool(params.get("clean_output_mesh"), True),
            )
            report.update({
                "input_mode": "volume_mesh_and_dem",
                "base_mesh_source": base_source,
                "base_mesh_name": base_name,
                "topography_source": topography_source,
                "topography_name": topography_name,
            })
            result_name_requested = str(
                params.get("output_file_name") or "mesh_without_local_top_20m.vtu"
            ).strip() or "mesh_without_local_top_20m.vtu"
            result_path, result_name, result_adjustment = _save_pyvista_mesh_compatible(
                result, result_name_requested
            )
            result_record = register_output_file(
                result_path, display_name=result_name
            )

        show_edges = _as_bool(params.get("show_edges"), True)
        scalar = _choose_mesh_scalar(
            result, str(params.get("mesh_scalars") or "auto")
        )
        preview = _mesh_preview(
            result, result_name, file_id=result_record["file_id"]
        )
        preview.update({
            "preview_type": "mesh",
            "operation": report.get("operation"),
            "scalar_used": scalar,
            "saved_output_file": result_name,
            "output_extension_adjustment": result_adjustment,
            "lowered_dem_file": lowered_final_name,
            "lowered_dem_file_id": lowered_record["file_id"],
            "lowered_dem_download_url": f"/api/download/{lowered_record['file_id']}",
            "lowered_dem_extension_adjustment": lowered_adjustment,
            "mesh_preview_url": (
                f"/api/pyvista/mesh/{result_record['file_id']}"
                f"?show_edges={str(show_edges).lower()}&scalars={scalar or ''}"
            ),
            "pyvista_preview_url": (
                f"/api/pyvista/mesh/{result_record['file_id']}"
                f"?show_edges={str(show_edges).lower()}&scalars={scalar or ''}"
            ),
            "pyvista_button_label": "Open Local Top-Layer Result 3D Popup",
            "web_scalar": scalar,
            "web_show_edges": show_edges,
            "web_viewer_label": "Mesh without local top layer",
            **report,
        })
        _attach_web_surface_preview(
            preview,
            result,
            Path(result_name).stem,
            preferred_scalar=scalar or "",
            show_edges=show_edges,
        )
        preview["inline_display"] = "web_3d_viewer"

        lowered_preview = _mesh_preview(
            lowered_dem,
            lowered_final_name,
            file_id=lowered_record["file_id"],
        )
        lowered_preview.update({
            "operation": "local_cutoff_dem",
            "top_layer_thickness": float(thickness),
            "download_url": f"/api/download/{lowered_record['file_id']}",
            **offset_meta,
        })

        return {
            "mesh": RuntimeValue(
                "mesh",
                result,
                name=result_name,
                preview=preview,
                metadata={"file_id": result_record["file_id"], **preview},
            ),
            "file": RuntimeValue(
                "file",
                Path(result_record["path"]),
                name=result_name,
                preview=preview,
                metadata={"file_id": result_record["file_id"], **preview},
            ),
            "lowered_topography": RuntimeValue(
                "mesh",
                lowered_dem,
                name=lowered_final_name,
                preview=lowered_preview,
                metadata={
                    "file_id": lowered_record["file_id"],
                    **lowered_preview,
                },
            ),
            "lowered_topography_file": RuntimeValue(
                "file",
                Path(lowered_record["path"]),
                name=lowered_final_name,
                preview=lowered_preview,
                metadata={
                    "file_id": lowered_record["file_id"],
                    **lowered_preview,
                },
            ),
            "report": RuntimeValue(
                "report",
                preview,
                name="remove_local_top_layer_report",
                preview=preview,
                metadata=preview,
            ),
        }
