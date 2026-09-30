from __future__ import annotations

import inspect
import json
import shutil
from io import BytesIO
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from .models import NodeExecutionError, RuntimeValue
from .runtime_store import put_runtime_object
from .storage import get_file_path, make_runtime_path, register_output_file
from .schema_checks import geological_table_report, validate_or_raise
from .shared_grid import make_shared_grid, snap_bounds, centers_are_aligned, voxel_cells_are_aligned


def _single(inputs: Dict[str, Any], name: str) -> RuntimeValue:
    value = inputs.get(name)
    if value is None:
        raise NodeExecutionError(f"Missing required input port: {name}")
    if isinstance(value, list):
        if len(value) == 0:
            raise NodeExecutionError(f"Input port {name} has no values")
        if len(value) > 1:
            raise NodeExecutionError(f"Input port {name} expects one value, got {len(value)}")
        return value[0]
    return value


def _many(inputs: Dict[str, Any], name: str) -> List[RuntimeValue]:
    value = inputs.get(name)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    return float(value)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise NodeExecutionError(f"Invalid JSON: {exc}") from exc



def _find_xyz_columns(df: pd.DataFrame) -> Optional[Dict[str, str]]:
    """Find X/Y/Z columns case-insensitively."""
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}
    cols = {}
    for canonical in ["x", "y", "z"]:
        if canonical not in lookup:
            return None
        cols[canonical.upper()] = lookup[canonical]
    return cols


def _xyz_bounds_from_table(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    cols = _find_xyz_columns(df)
    if not cols:
        return None
    xyz = pd.DataFrame({
        "X": pd.to_numeric(df[cols["X"]], errors="coerce"),
        "Y": pd.to_numeric(df[cols["Y"]], errors="coerce"),
        "Z": pd.to_numeric(df[cols["Z"]], errors="coerce"),
    })
    valid = xyz.notna().all(axis=1)
    if not bool(valid.any()):
        return None
    xyzv = xyz.loc[valid]
    raw = [
        float(xyzv["X"].min()), float(xyzv["X"].max()),
        float(xyzv["Y"].min()), float(xyzv["Y"].max()),
        float(xyzv["Z"].min()), float(xyzv["Z"].max()),
    ]
    return {
        "coordinate_columns": cols,
        "valid_xyz_rows": int(valid.sum()),
        "invalid_xyz_rows": int((~valid).sum()),
        "raw_extent": raw,
    }


def _merge_extent_bounds(bounds: Sequence[Optional[Dict[str, Any]]]) -> Optional[List[float]]:
    extents = [b.get("raw_extent") for b in bounds if b and b.get("raw_extent")]
    if not extents:
        return None
    return [
        min(e[0] for e in extents), max(e[1] for e in extents),
        min(e[2] for e in extents), max(e[3] for e in extents),
        min(e[4] for e in extents), max(e[5] for e in extents),
    ]


def _extent_with_padding(raw_extent: Sequence[float], padding_percent: float = 5.0) -> List[float]:
    vals = [float(v) for v in raw_extent]
    if len(vals) != 6:
        raise NodeExecutionError("Internal extent calculation failed: expected 6 extent values.")
    pad_fraction = max(float(padding_percent or 0.0), 0.0) / 100.0
    out: List[float] = []
    for lo, hi in [(vals[0], vals[1]), (vals[2], vals[3]), (vals[4], vals[5])]:
        rng = hi - lo
        if not np.isfinite(rng) or rng < 0:
            raise NodeExecutionError("Internal extent calculation failed: invalid min/max range.")
        pad = rng * pad_fraction
        # Degenerate ranges still need a small non-zero extent for GemPy.
        if pad == 0:
            scale = max(abs(lo), abs(hi), 1.0)
            pad = scale * pad_fraction if pad_fraction > 0 else 1.0
        out.extend([float(lo - pad), float(hi + pad)])
    return out


def _auto_extent_from_tables(surface_points: pd.DataFrame, orientations: pd.DataFrame, padding_percent: float = 5.0) -> Dict[str, Any]:
    sp_bounds = _xyz_bounds_from_table(surface_points)
    op_bounds = _xyz_bounds_from_table(orientations)
    raw = _merge_extent_bounds([sp_bounds, op_bounds])
    if raw is None:
        raise NodeExecutionError("Could not auto-calculate model extent: no valid numeric X/Y/Z coordinates found in surface_points or orientations.")
    padded = _extent_with_padding(raw, padding_percent)
    return {
        "raw_extent": raw,
        "padded_extent": padded,
        "padding_percent": float(padding_percent or 0.0),
        "surface_points_bounds": sp_bounds,
        "orientations_bounds": op_bounds,
    }



def _table_preview(df: pd.DataFrame, rows: int = 10) -> Dict[str, Any]:
    if isinstance(df, dict):
        if not df:
            raise NodeExecutionError("Cannot preview an empty Excel workbook dictionary.")
        first_sheet = next(iter(df))
        df = df[first_sheet]
    preview = df.head(rows).replace({np.nan: None}).to_dict(orient="records")
    numeric = df.select_dtypes(include=[np.number])
    ranges = {}
    for col in numeric.columns:
        colv = pd.to_numeric(df[col], errors="coerce")
        if colv.notna().any():
            ranges[col] = {"min": float(colv.min()), "max": float(colv.max())}
    formations = None
    if "formation" in df.columns:
        vc = df["formation"].astype(str).value_counts().head(30)
        formations = vc.to_dict()
    xyz_bounds = _xyz_bounds_from_table(df)
    out = {
        "rows": int(len(df)),
        "columns": list(map(str, df.columns)),
        "dtypes": {str(k): str(v) for k, v in df.dtypes.items()},
        "head": preview,
        "numeric_ranges": ranges,
        "formation_counts": formations,
    }
    if xyz_bounds:
        out["xyz_bounds"] = xyz_bounds
        out["raw_xyz_extent"] = xyz_bounds["raw_extent"]
        out["suggested_extent_5_percent"] = _extent_with_padding(xyz_bounds["raw_extent"], 5.0)
    return out


def _attach_pyvista_preview(preview: Dict[str, Any], df: pd.DataFrame, display_name: str) -> Dict[str, Any]:
    """Save a CSV copy that can be opened by the PyVista preview endpoint."""
    out = dict(preview)
    if not all(col in df.columns for col in ["X", "Y", "Z"]):
        out["pyvista_available"] = False
        out["pyvista_reason"] = "The table needs X, Y, Z columns for 3D preview."
        return out
    try:
        path = make_runtime_path(f"pyvista_preview_{display_name or 'table'}", ".csv")
        df.to_csv(path, index=False)
        record = register_output_file(path, display_name=f"pyvista_preview_{display_name or 'table'}.csv")
        out["pyvista_available"] = True
        out["pyvista_file_id"] = record["file_id"]
        out["pyvista_preview_url"] = f"/api/pyvista/table/{record['file_id']}"
        out["pyvista_note"] = "Opens a local PyVista desktop window from the FastAPI process."
    except Exception as exc:
        out["pyvista_available"] = False
        out["pyvista_reason"] = f"Could not prepare PyVista preview CSV: {exc}"
    return out


def _report_preview(report: Dict[str, Any]) -> Dict[str, Any]:
    return report


MESH_SUFFIXES = {".vtk", ".vtp", ".vtu", ".vti", ".stl", ".ply", ".obj"}
TABLE_SUFFIXES = {".csv", ".xlsx", ".xls"}
RASTER_SUFFIXES = {".tif", ".tiff"}


def _infer_file_type_from_name(name: str, file_type: str = "auto") -> str:
    typ = (file_type or "auto").lower()
    if typ != "auto":
        return typ
    suffix = Path(str(name)).suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".xlsx", ".xls"}:
        return "xlsx"
    if suffix in MESH_SUFFIXES:
        return "mesh"
    if suffix in RASTER_SUFFIXES:
        return "raster"
    if suffix == ".npy":
        return "npy"
    if suffix == ".json":
        return "json"
    if suffix == ".gempy":
        return "gempy"
    raise NodeExecutionError(f"Cannot infer file type from {name}. Choose csv/xlsx, json, gempy, mesh, raster/tif, or npy.")


def _read_table_from_path(path: Path, file_type: str = "auto", sheet_name: Optional[str] = None) -> pd.DataFrame:
    typ = _infer_file_type_from_name(path.name, file_type)
    if typ == "csv":
        return pd.read_csv(path)
    if typ in {"xlsx", "excel"}:
        kwargs = {}
        if sheet_name:
            kwargs["sheet_name"] = sheet_name
        return pd.read_excel(path, **kwargs)
    raise NodeExecutionError(f"Unsupported table file type: {file_type}")


def _read_mesh_from_path(path: Path):
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc
    try:
        return pv.read(str(path))
    except Exception as exc:
        raise NodeExecutionError(f"Could not read mesh file {path.name}: {exc}") from exc



def _mesh_to_web_surface(mesh: Any) -> Optional[Any]:
    """Convert a PyVista mesh to a browser-friendly VTP PolyData surface."""
    try:
        import pyvista as pv
    except Exception:
        return None

    try:
        if isinstance(mesh, pv.PolyData):
            poly = mesh.copy(deep=True)
        else:
            poly = mesh.extract_surface()
        if getattr(poly, "n_points", 0) <= 0:
            return None
        try:
            poly = poly.clean(tolerance=0.0)
        except TypeError:
            poly = poly.clean()
        except Exception:
            pass
        return poly
    except Exception:
        return None



def _numpy_json_list(values: Any, *, max_len: Optional[int] = None) -> List[Any]:
    arr = np.asarray(values)
    if max_len is not None and arr.shape[0] > max_len:
        arr = arr[:max_len]
    out = arr.tolist()
    return out


def _poly_faces_to_simple_lists(poly: Any, *, max_faces: int = 50000) -> List[List[int]]:
    faces_out: List[List[int]] = []
    try:
        raw = np.asarray(poly.faces, dtype=np.int64).ravel()
        i = 0
        while i < raw.size and len(faces_out) < max_faces:
            n = int(raw[i])
            ids = raw[i + 1:i + 1 + n].astype(int).tolist()
            if len(ids) >= 2:
                faces_out.append(ids)
            i += n + 1
    except Exception:
        pass

    if not faces_out:
        try:
            raw = np.asarray(poly.lines, dtype=np.int64).ravel()
            i = 0
            while i < raw.size and len(faces_out) < max_faces:
                n = int(raw[i])
                ids = raw[i + 1:i + 1 + n].astype(int).tolist()
                if len(ids) >= 2:
                    faces_out.append(ids)
                i += n + 1
        except Exception:
            pass

    if not faces_out:
        try:
            n_points = int(poly.n_points)
            faces_out = [[i] for i in range(min(n_points, max_faces))]
        except Exception:
            pass

    return faces_out


def _array_to_small_numeric_list(arr: Any, max_items: int) -> Optional[List[float]]:
    try:
        a = np.asarray(arr)
        if a.ndim != 1:
            return None
        if a.shape[0] > max_items:
            a = a[:max_items]
        out = []
        for v in a:
            try:
                fv = float(v)
                out.append(fv if np.isfinite(fv) else 0.0)
            except Exception:
                out.append(0.0)
        return out
    except Exception:
        return None


def _register_web_mesh_json_preview(poly: Any, name: str = "mesh") -> Optional[Dict[str, Any]]:
    """Register a lightweight JSON mesh for the built-in no-VTK fallback viewer.

    This avoids relying on external vtk.js/CDN for right-panel previews. The JSON
    is intentionally simple: points, faces, and scalar arrays. It is not a
    replacement for VTK/VTU export; it is only for browser-side fallback display.
    """
    if poly is None:
        return None

    raw_stem = Path(str(name)).stem or "mesh"
    stem = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in raw_stem).strip("_") or "mesh"
    max_points = 60000
    max_faces = 80000

    try:
        points_np = np.asarray(poly.points, dtype=float)
        if points_np.ndim != 2 or points_np.shape[1] < 3 or points_np.shape[0] == 0:
            return None

        original_point_count = int(points_np.shape[0])
        if original_point_count > max_points:
            # Keep the first N points for a responsive fallback viewer. The VTP
            # file remains available for full vtk.js/PyVista viewing.
            points_np = points_np[:max_points, :3]
        else:
            points_np = points_np[:, :3]

        faces = _poly_faces_to_simple_lists(poly, max_faces=max_faces)
        # Drop faces that reference points excluded by max_points.
        n_kept = int(points_np.shape[0])
        faces = [[int(i) for i in face if int(i) < n_kept] for face in faces]
        faces = [face for face in faces if len(face) >= 1]

        point_scalars: Dict[str, List[float]] = {}
        try:
            for key, arr in getattr(poly, "point_data", {}).items():
                vals = _array_to_small_numeric_list(arr, n_kept)
                if vals is not None and len(vals) == n_kept:
                    point_scalars[str(key)] = vals
        except Exception:
            pass

        cell_scalars: Dict[str, List[float]] = {}
        try:
            n_faces = len(faces)
            for key, arr in getattr(poly, "cell_data", {}).items():
                vals = _array_to_small_numeric_list(arr, n_faces)
                if vals is not None and len(vals) >= min(n_faces, len(vals)):
                    cell_scalars[str(key)] = vals[:n_faces]
        except Exception:
            pass

        payload = {
            "schema": "gempy-node-editor-simple-mesh-v1",
            "name": stem,
            "points": points_np.tolist(),
            "faces": faces,
            "pointScalars": point_scalars,
            "cellScalars": cell_scalars,
            "bounds": [float(v) for v in getattr(poly, "bounds", [])],
            "n_points": int(n_kept),
            "n_cells": int(len(faces)),
            "original_n_points": original_point_count,
            "original_n_cells": int(getattr(poly, "n_cells", len(faces))),
            "truncated": bool(original_point_count > n_kept or int(getattr(poly, "n_cells", len(faces))) > len(faces)),
        }
        path = make_runtime_path(f"{stem}_web_preview", ".json")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        record = register_output_file(path, display_name=f"{stem}_web_preview.json")
        return record
    except Exception:
        return None


def _register_web_surface_preview(mesh: Any, name: str = "mesh") -> tuple[Optional[Dict[str, Any]], Optional[Any]]:
    """Save a temporary VTP surface preview and register it as an output file."""
    poly = _mesh_to_web_surface(mesh)
    if poly is None:
        return None, None
    raw_stem = Path(str(name)).stem or "mesh"
    stem = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in raw_stem).strip("_") or "mesh"
    path = make_runtime_path(f"{stem}_web_surface", ".vtp")
    try:
        poly.save(path)
        record = register_output_file(path, display_name=f"{stem}_web_surface.vtp")
        return record, poly
    except Exception:
        return None, poly


def _attach_web_surface_preview(preview: Dict[str, Any], mesh: Any, name: str, preferred_scalar: str = "", show_edges: bool = False) -> Dict[str, Any]:
    """Attach inline browser preview fields using an auto-generated VTP surface."""
    record, poly = _register_web_surface_preview(mesh, name)
    if record is None:
        preview["web_viewer_available"] = False
        preview["web_viewer_reason"] = "Could not create a VTP surface preview for the browser viewer."
        return preview

    scalar = preferred_scalar or ""
    if poly is not None and (not scalar or (scalar not in getattr(poly, "cell_data", {}) and scalar not in getattr(poly, "point_data", {}))):
        scalar = _choose_mesh_scalar(poly, scalar or "auto") or scalar

    preview["web_mesh_url"] = f"/api/raw/{record['file_id']}"
    preview["web_mesh_file_id"] = record["file_id"]
    preview["web_mesh_format"] = "vtp"
    preview["web_scalar"] = scalar or ""
    preview["web_show_edges"] = bool(show_edges)
    preview["web_viewer_available"] = True

    json_record = _register_web_mesh_json_preview(poly, name)
    if json_record is not None:
        preview["web_mesh_json_url"] = f"/api/raw/{json_record['file_id']}"
        preview["web_mesh_json_file_id"] = json_record["file_id"]
        preview["web_mesh_json_format"] = "simple_mesh_json"
        preview["web_fallback_available"] = True

    preview["web_preview_note"] = "Inline browser preview uses VTK.js when available. If VTK.js cannot load, the built-in lightweight mesh viewer is used as fallback. The original output file remains available for download/PyVista."
    return preview



def _mesh_preview(mesh: Any, name: str = "mesh", file_id: Optional[str] = None) -> Dict[str, Any]:
    preview: Dict[str, Any] = {
        "name": name,
        "n_cells": int(getattr(mesh, "n_cells", 0)),
        "n_points": int(getattr(mesh, "n_points", 0)),
        "bounds": [float(v) for v in getattr(mesh, "bounds", [])],
        "cell_data": list(getattr(mesh, "cell_data", {}).keys()),
        "point_data": list(getattr(mesh, "point_data", {}).keys()),
    }
    if file_id:
        suffix = Path(str(name)).suffix.lower().lstrip(".")
        # Meshes produced by PyVista/GemPy are usually VTP/VTU. If the runtime
        # value did not carry a suffix, infer from the mesh type so the browser
        # viewer can choose the correct VTK.js reader.
        if not suffix:
            suffix = "vtu" if "unstructured" in mesh.__class__.__name__.lower() else "vtp"
        preview["file_id"] = file_id
        preview["download_url"] = f"/api/download/{file_id}"
        preview["mesh_preview_url"] = f"/api/pyvista/mesh/{file_id}"
        # Inline browser preview fields are attached by _attach_web_surface_preview.
        # Do not point the browser directly at arbitrary VTU/VTK files here.
    return preview




def _choose_mesh_scalar(mesh: Any, requested: str = "auto") -> str:
    cell_keys = [str(k) for k in getattr(mesh, "cell_data", {}).keys()]
    point_keys = [str(k) for k in getattr(mesh, "point_data", {}).keys()]
    keys = cell_keys + point_keys
    req = str(requested or "auto").strip()
    if req and req.lower() not in {"auto", "none", ""}:
        for key in keys:
            if key == req or key.lower() == req.lower():
                return key
        return ""
    priority = ["MaterialIDs", "MaterialID", "material_ids", "material_id", "id", "ids", "lith_block", "lithology", "layer", "layer_id", "BoundaryID"]
    for p in priority:
        for key in keys:
            if key == p:
                return key
    for key in keys:
        if key.lower() not in {"cell_ids", "cell_id", "vtkoriginalcellids", "vtkoriginalpointids"}:
            return key
    return keys[0] if keys else ""


def _parse_scalar_filter_values(raw_values: Any) -> List[float]:
    """Parse a comma/semicolon separated list of numeric scalar values."""
    if raw_values is None:
        return []
    if isinstance(raw_values, (list, tuple, np.ndarray)):
        tokens = list(np.asarray(raw_values).ravel())
    else:
        text = str(raw_values).strip().replace(";", ",")
        tokens = [token.strip() for token in text.split(",") if token.strip()]
    values: List[float] = []
    for token in tokens:
        try:
            value = float(token)
        except (TypeError, ValueError) as exc:
            raise NodeExecutionError(
                f"Scalar filter values must be numeric and comma-separated; could not parse {token!r}."
            ) from exc
        if not np.isfinite(value):
            raise NodeExecutionError(f"Scalar filter value must be finite, got {token!r}.")
        if value not in values:
            values.append(value)
    return values


def _filter_mesh_by_scalar_values(mesh: Any, requested_scalar: str, raw_values: Any) -> tuple[Any, Dict[str, Any]]:
    """Return a display-only subset matching selected cell or point scalar values."""
    scalar_name = _choose_mesh_scalar(mesh, requested_scalar)
    if not scalar_name:
        available = list(getattr(mesh, "cell_data", {}).keys()) + list(getattr(mesh, "point_data", {}).keys())
        raise NodeExecutionError(
            f"Visualization scalar filter could not find {requested_scalar!r}. Available scalars: {available}"
        )
    selected_values = _parse_scalar_filter_values(raw_values)
    if not selected_values:
        raise NodeExecutionError(
            "Visualization scalar filter is enabled, but Scalar values to show is empty. "
            "Enter one value such as 2, or several values such as 2,5,7."
        )

    if scalar_name in getattr(mesh, "cell_data", {}):
        association = "cell"
        source_values = np.asarray(mesh.cell_data[scalar_name])
    else:
        association = "point"
        source_values = np.asarray(mesh.point_data[scalar_name])
    if source_values.ndim != 1:
        raise NodeExecutionError(
            f"Visualization scalar filter requires a one-component scalar array; {scalar_name!r} "
            f"has shape {list(source_values.shape)}."
        )
    try:
        numeric_values = source_values.astype(float)
    except (TypeError, ValueError) as exc:
        raise NodeExecutionError(
            f"Visualization scalar filter requires numeric values; {scalar_name!r} has dtype {source_values.dtype}."
        ) from exc

    mask = np.zeros(numeric_values.shape, dtype=bool)
    for selected in selected_values:
        mask |= np.isclose(numeric_values, selected, rtol=1e-9, atol=1e-9)
    selected_count = int(np.count_nonzero(mask))
    if selected_count == 0:
        available_values = np.unique(numeric_values)
        sample = available_values[:30].tolist()
        raise NodeExecutionError(
            f"No {association}s have {scalar_name} in {selected_values}. "
            f"Available values (first 30): {sample}"
        )

    if association == "cell":
        filtered = mesh.extract_cells(np.flatnonzero(mask))
    else:
        # Point-data filtering is displayed as the selected points only. Cell-data
        # material filtering (the common MaterialIDs case) preserves whole cells.
        filtered = mesh.extract_points(np.flatnonzero(mask), adjacent_cells=False, include_cells=False)

    report = {
        "enabled": True,
        "scalar": scalar_name,
        "association": association,
        "values": selected_values,
        "input_cells": int(getattr(mesh, "n_cells", 0)),
        "input_points": int(getattr(mesh, "n_points", 0)),
        "matching_values": selected_count,
        "display_cells": int(getattr(filtered, "n_cells", 0)),
        "display_points": int(getattr(filtered, "n_points", 0)),
        "display_only": True,
    }
    return filtered, report


def _mesh_runtime_suffix(mesh: Any) -> str:
    """Choose a PyVista writer extension that matches the concrete dataset."""
    class_name = mesh.__class__.__name__.lower()
    if "unstructured" in class_name:
        return ".vtu"
    if "polydata" in class_name:
        return ".vtp"
    if "image" in class_name or "uniformgrid" in class_name:
        return ".vti"
    if "rectilinear" in class_name:
        return ".vtr"
    if "structured" in class_name:
        return ".vts"
    return ".vtk"


def _visualization_mesh_preview(
    mesh: Any,
    *,
    name: str,
    file_id: str,
    scalars: str,
    show_edges: bool,
    generate_thumbnail: bool,
    filter_by_scalar_values: bool,
    scalar_filter_values: Any,
) -> Dict[str, Any]:
    display_mesh = mesh
    display_name = name
    display_file_id = file_id
    scalar = _choose_mesh_scalar(display_mesh, scalars)
    filter_report: Optional[Dict[str, Any]] = None

    if filter_by_scalar_values:
        display_mesh, filter_report = _filter_mesh_by_scalar_values(display_mesh, scalars, scalar_filter_values)
        scalar = str(filter_report["scalar"])
        suffix = _mesh_runtime_suffix(display_mesh)
        source_stem = Path(str(name)).stem or "mesh"
        safe_scalar = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in scalar)
        value_label = "_".join(str(value).replace(".", "p").replace("-", "m") for value in filter_report["values"])
        display_name = f"{source_stem}_{safe_scalar}_{value_label}_display{suffix}"
        display_path = make_runtime_path(Path(display_name).stem, suffix)
        display_mesh.save(display_path)
        record = register_output_file(display_path, display_name=display_name)
        display_file_id = str(record["file_id"])
    elif not display_file_id:
        suffix = _mesh_runtime_suffix(display_mesh)
        display_path = make_runtime_path(Path(str(name)).stem or "preview_mesh", suffix)
        display_mesh.save(display_path)
        record = register_output_file(display_path, display_name=f"{Path(str(name)).stem or 'preview_mesh'}{suffix}")
        display_file_id = str(record["file_id"])

    preview = _mesh_preview(display_mesh, display_name, display_file_id)
    preview.update({"preview_type": "mesh"})
    query = urlencode({"show_edges": str(show_edges).lower(), "scalars": scalar or ""})
    preview["pyvista_preview_url"] = f"/api/pyvista/mesh/{display_file_id}?{query}"
    preview["pyvista_button_label"] = "Enlarge / Open 3D"
    _attach_web_surface_preview(preview, display_mesh, display_name, preferred_scalar=scalar or "", show_edges=show_edges)
    preview["web_viewer_label"] = "Inline mesh viewer"
    preview["inline_display"] = "web_3d_viewer"
    if filter_report is not None:
        preview["scalar_filter"] = filter_report
        preview["filter_message"] = (
            f"Display-only filter: {filter_report['scalar']} = "
            f"{', '.join(str(value) for value in filter_report['values'])}. "
            f"Showing {filter_report['display_cells']} cells and {filter_report['display_points']} points."
        )
    if generate_thumbnail:
        thumb = _mesh_thumbnail(
            display_mesh,
            scalars=scalar,
            show_edges=show_edges,
            name=Path(str(display_name)).stem + "_preview",
        )
        if thumb:
            preview.update(thumb)
    return preview


def _mesh_thumbnail(mesh: Any, *, scalars: str = "auto", show_edges: bool = False, name: str = "mesh_preview") -> Optional[Dict[str, Any]]:
    """Create a small PNG thumbnail for inline inspector preview.

    This is best-effort. If off-screen rendering is unavailable, the caller still
    gets metadata and an enlarge/popup button.
    """
    try:
        import pyvista as pv
        scalar_name = _choose_mesh_scalar(mesh, scalars)
        p = pv.Plotter(notebook=False, off_screen=True, window_size=(720, 480))
        kwargs = {"show_edges": bool(show_edges)}
        # Make point-cloud tables visible in inline thumbnails.
        try:
            if getattr(mesh, "n_cells", 0) == getattr(mesh, "n_points", -1):
                kwargs["point_size"] = 9
                kwargs["render_points_as_spheres"] = True
        except Exception:
            pass
        if scalar_name:
            kwargs["scalars"] = scalar_name
        p.add_mesh(mesh, **kwargs)
        try:
            p.view_isometric()
            p.reset_camera()
        except Exception:
            pass
        png = make_runtime_path(name, ".png")
        p.screenshot(str(png))
        p.close()
        rec = register_output_file(png, display_name=f"{name}.png")
        return {
            "image_url": f"/api/raw/{rec['file_id']}",
            "thumbnail_file_id": rec["file_id"],
            "thumbnail_note": "Static inline thumbnail generated off-screen. Use Enlarge/Open 3D for interactive inspection.",
        }
    except Exception as exc:
        return {"thumbnail_error": str(exc)}


def _array_preview(arr: Any, name: str = "array", rows: int = 10) -> Dict[str, Any]:
    a = np.asarray(arr)
    preview: Dict[str, Any] = {
        "preview_type": "array",
        "name": name,
        "shape": list(a.shape),
        "dtype": str(a.dtype),
        "size": int(a.size),
    }
    if a.size:
        finite = a[np.isfinite(a)] if np.issubdtype(a.dtype, np.number) else np.asarray([])
        if finite.size:
            preview.update({
                "min": float(np.nanmin(a)),
                "max": float(np.nanmax(a)),
                "mean": float(np.nanmean(a)),
            })
        preview["unique_sample"] = np.unique(a.ravel()[: min(a.size, 5000)]).tolist()[:50]
    if a.ndim == 1:
        preview["head"] = [{"index": int(i), "value": a.ravel()[i].item() if hasattr(a.ravel()[i], "item") else str(a.ravel()[i])} for i in range(min(rows, a.size))]
        preview["columns"] = ["index", "value"]
    elif a.ndim == 2:
        # Save a small image preview for numeric 2D arrays.
        try:
            import matplotlib.pyplot as plt
            png = make_runtime_path(f"{name}_array_preview", ".png")
            fig, ax = plt.subplots(figsize=(5, 3.5), dpi=120)
            ax.imshow(a, aspect="auto")
            ax.set_title(name)
            fig.tight_layout()
            fig.savefig(png)
            plt.close(fig)
            rec = register_output_file(png, display_name=f"{name}_array_preview.png")
            preview["image_url"] = f"/api/raw/{rec['file_id']}"
        except Exception as exc:
            preview["thumbnail_error"] = str(exc)
    elif a.ndim == 3:
        try:
            import matplotlib.pyplot as plt
            mid = a.shape[2] // 2
            png = make_runtime_path(f"{name}_slice_preview", ".png")
            fig, ax = plt.subplots(figsize=(5, 3.5), dpi=120)
            ax.imshow(a[:, :, mid].T, origin="lower", aspect="auto")
            ax.set_title(f"{name}: middle z slice {mid}")
            fig.tight_layout()
            fig.savefig(png)
            plt.close(fig)
            rec = register_output_file(png, display_name=f"{name}_slice_preview.png")
            preview["image_url"] = f"/api/raw/{rec['file_id']}"
        except Exception as exc:
            preview["thumbnail_error"] = str(exc)
    return preview


def _raster_preview(path: Path, file_id: Optional[str] = None, name: str = "raster") -> Dict[str, Any]:
    preview: Dict[str, Any] = {"preview_type": "raster", "name": name, "file_name": path.name}
    if file_id:
        preview["download_url"] = f"/api/download/{file_id}"
        preview["file_id"] = file_id
    try:
        from PIL import Image
        img = Image.open(path)
        preview.update({
            "format": img.format,
            "mode": img.mode,
            "width": int(img.width),
            "height": int(img.height),
        })
        thumb = img.copy()
        thumb.thumbnail((900, 520))
        png = make_runtime_path(Path(path.name).stem + "_raster_preview", ".png")
        # Convert unsupported modes to RGB/L for PNG preview.
        if thumb.mode not in {"RGB", "RGBA", "L"}:
            thumb = thumb.convert("L")
        thumb.save(png)
        rec = register_output_file(png, display_name=f"{Path(path.name).stem}_raster_preview.png")
        preview["image_url"] = f"/api/raw/{rec['file_id']}"
    except Exception as exc:
        preview["thumbnail_error"] = str(exc)
    return preview




def _find_column_case_insensitive(df: pd.DataFrame, names: Sequence[str]) -> Optional[str]:
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}
    for name in names:
        key = str(name).strip().lower()
        if key in lookup:
            return lookup[key]
    return None


def _spatial_table_preview(
    df: pd.DataFrame,
    *,
    name: str = "table",
    source_file_id: Optional[str] = None,
    rows: int = 15,
    generate_thumbnail: bool = True,
    show_edges: bool = False,
) -> Dict[str, Any]:
    """Preview a table as geological spatial data when X/Y/Z columns exist.

    The table preview is still included, but a PyVista point cloud is generated
    for inline/right-panel inspection and optional enlarged 3D popup viewing.
    """
    base = _table_preview(df, rows=rows)
    base.update({
        "preview_type": "spatial_table" if df is not None else "table",
        "name": name,
    })
    if source_file_id:
        base["download_url"] = f"/api/download/{source_file_id}"
        base["file_id"] = source_file_id

    x_col = _find_column_case_insensitive(df, ["X", "x"])
    y_col = _find_column_case_insensitive(df, ["Y", "y"])
    z_col = _find_column_case_insensitive(df, ["Z", "z"])
    if not (x_col and y_col and z_col):
        base["spatial_preview_available"] = False
        base["spatial_preview_reason"] = "No X/Y/Z coordinate columns found. Showing table only."
        return base

    xyz_bounds = _xyz_bounds_from_table(df)
    if xyz_bounds:
        base["xyz_bounds"] = xyz_bounds
        base["raw_xyz_extent"] = xyz_bounds["raw_extent"]
        base["suggested_extent_5_percent"] = _extent_with_padding(xyz_bounds["raw_extent"], 5.0)

    coords_df = pd.DataFrame({
        "x": pd.to_numeric(df[x_col], errors="coerce"),
        "y": pd.to_numeric(df[y_col], errors="coerce"),
        "z": pd.to_numeric(df[z_col], errors="coerce"),
    })
    valid = coords_df.notna().all(axis=1)
    coords = coords_df.loc[valid, ["x", "y", "z"]].to_numpy(dtype=float)
    base["spatial_preview_available"] = True
    base["spatial_rows_valid"] = int(valid.sum())
    base["spatial_rows_invalid"] = int((~valid).sum())
    base["coordinate_columns"] = {"x": x_col, "y": y_col, "z": z_col}

    if coords.shape[0] == 0:
        base["spatial_preview_reason"] = "X/Y/Z columns exist, but no valid numeric coordinate rows were found."
        return base

    try:
        import pyvista as pv

        cloud = pv.PolyData(coords)
        # Explicit vertex cells make point clouds more robust across PyVista/VTK versions.
        cloud.verts = np.hstack([
            np.ones((coords.shape[0], 1), dtype=np.int64),
            np.arange(coords.shape[0], dtype=np.int64).reshape(-1, 1),
        ])

        formation_col = _find_column_case_insensitive(df, ["formation", "Formation", "surface", "Surface", "element", "Element", "elements_names"])
        if formation_col:
            formations = df.loc[valid, formation_col].astype(str).fillna("").to_numpy()
            unique = list(dict.fromkeys(formations.tolist()))
            mapping = {v: i for i, v in enumerate(unique)}
            cloud.point_data["formation_id"] = np.array([mapping[v] for v in formations], dtype=np.int32)
            cloud.point_data["formation"] = formations
            base["formation_column"] = formation_col
            base["formation_id_map"] = mapping
            scalar_for_preview = "formation_id"
        else:
            cloud.point_data["point_id"] = np.arange(coords.shape[0], dtype=np.int32)
            scalar_for_preview = "point_id"

        gx = _find_column_case_insensitive(df, ["G_x", "gx", "Gx", "g_x"])
        gy = _find_column_case_insensitive(df, ["G_y", "gy", "Gy", "g_y"])
        gz = _find_column_case_insensitive(df, ["G_z", "gz", "Gz", "g_z"])
        az_col = _find_column_case_insensitive(df, ["azimuth", "az", "dip_azimuth"])
        dip_col = _find_column_case_insensitive(df, ["dip"])
        pol_col = _find_column_case_insensitive(df, ["polarity", "pole", "direction"])

        if gx and gy and gz:
            vecs = pd.DataFrame({
                "gx": pd.to_numeric(df.loc[valid, gx], errors="coerce"),
                "gy": pd.to_numeric(df.loc[valid, gy], errors="coerce"),
                "gz": pd.to_numeric(df.loc[valid, gz], errors="coerce"),
            }).fillna(0.0).to_numpy(dtype=float)
            cloud.point_data["orientation_vector"] = vecs
            try:
                norms = np.linalg.norm(vecs, axis=1)
                base["orientation_vector_mean_length"] = float(np.nanmean(norms)) if norms.size else None
            except Exception:
                pass
            base["orientation_columns"] = {"G_x": gx, "G_y": gy, "G_z": gz}
            base["orientation_format"] = "gradient"
            base["contains_orientations"] = True
            base["orientation_preview"] = "arrows"
        elif az_col and dip_col:
            pol_values = df.loc[valid, pol_col] if pol_col else None
            vx, vy, vz = _azimuth_dip_polarity_to_gradient(
                df.loc[valid, az_col],
                df.loc[valid, dip_col],
                pol_values,
            )
            vecs = np.column_stack([vx, vy, vz])
            cloud.point_data["orientation_vector"] = vecs
            try:
                norms = np.linalg.norm(vecs, axis=1)
                base["orientation_vector_mean_length"] = float(np.nanmean(norms)) if norms.size else None
            except Exception:
                pass
            base["orientation_columns"] = {"azimuth": az_col, "dip": dip_col, "polarity": pol_col}
            base["orientation_format"] = "azimuth_dip"
            base["orientation_conversion"] = "azimuth/dip/polarity converted to orientation arrows and GemPy G_x/G_y/G_z convention"
            base["contains_orientations"] = True
            base["orientation_preview"] = "arrows"
        else:
            base["contains_orientations"] = False

        suffix = ".vtp"
        mesh_path = make_runtime_path(Path(str(name)).stem + "_spatial_points", suffix)
        cloud.save(mesh_path)
        rec = register_output_file(mesh_path, display_name=f"{Path(str(name)).stem}_spatial_points.vtp")
        file_id = rec["file_id"]

        base.update({
            "spatial_mesh_file_id": file_id,
            "spatial_mesh_file_name": f"{Path(str(name)).stem}_spatial_points.vtp",
            "mesh_preview_url": f"/api/pyvista/mesh/{file_id}?scalars={quote(scalar_for_preview)}&show_edges=false&vector=orientation_vector",
            "pyvista_preview_url": f"/api/pyvista/mesh/{file_id}?scalars={quote(scalar_for_preview)}&show_edges=false&vector=orientation_vector",
            "pyvista_button_label": "Enlarge / Open 3D point cloud",
            "pyvista_note": "Spatial table detected from X/Y/Z columns. Points are colored by formation when available. Orientation tables are shown with arrows from G_x/G_y/G_z.",
            "inline_display": "spatial_point_cloud",
        })

        if generate_thumbnail:
            thumb = _mesh_thumbnail(cloud, scalars=scalar_for_preview, show_edges=show_edges, name=Path(str(name)).stem + "_spatial_table_preview")
            if thumb:
                base.update(thumb)

    except Exception as exc:
        base["spatial_preview_available"] = False
        base["spatial_preview_reason"] = f"Could not create spatial table preview: {exc}"

    return base

def _file_preview(path: Path, file_id: Optional[str] = None, *, rows: int = 15, scalars: str = "auto", show_edges: bool = False) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    file_type = _infer_file_type_from_name(path.name, "auto")
    if file_type in {"csv", "xlsx"}:
        df = _read_table_from_path(path, file_type)
        prev = _spatial_table_preview(
            df,
            name=path.name,
            source_file_id=file_id,
            rows=rows,
            generate_thumbnail=True,
            show_edges=show_edges,
        )
        prev["preview_type"] = "spatial_table_file" if prev.get("spatial_preview_available") else "table_file"
    elif file_type == "mesh":
        mesh = _read_mesh_from_path(path)
        prev = _mesh_preview(mesh, path.name, file_id)
        prev["preview_type"] = "mesh_file"
        scalar = _choose_mesh_scalar(mesh, scalars)
        if file_id:
            query = urlencode({"show_edges": str(show_edges).lower(), "scalars": scalar or ""})
            prev["pyvista_preview_url"] = f"/api/pyvista/mesh/{file_id}?{query}"
            prev["pyvista_button_label"] = "Enlarge / Open 3D"
            _attach_web_surface_preview(prev, mesh, path.name, preferred_scalar=scalar or "", show_edges=show_edges)
        thumb = _mesh_thumbnail(mesh, scalars=scalar, show_edges=show_edges, name=Path(path.name).stem + "_mesh_preview")
        if thumb:
            prev.update(thumb)
    elif file_type == "raster":
        prev = _raster_preview(path, file_id=file_id, name=path.name)
    elif file_type == "npy":
        arr = np.load(path)
        prev = _array_preview(arr, name=path.name, rows=rows)
    elif file_type == "json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                keys = list(data.keys())[:20]
                prev = {
                    "preview_type": "json_file",
                    "file_name": path.name,
                    "top_level_type": "object",
                    "top_level_keys": keys,
                    "jsonio_hint": "If this is a legacy GemPy JsonIO model, connect Load Uploaded File.file to Load GemPy Model, or select it directly there.",
                }
            elif isinstance(data, list):
                prev = {
                    "preview_type": "json_file",
                    "file_name": path.name,
                    "top_level_type": "array",
                    "length": len(data),
                    "jsonio_hint": "If this is a legacy GemPy JsonIO model, connect Load Uploaded File.file to Load GemPy Model, or select it directly there.",
                }
            else:
                prev = {"preview_type": "json_file", "file_name": path.name, "top_level_type": type(data).__name__}
        except Exception as exc:
            prev = {"preview_type": "json_file", "file_name": path.name, "warning": f"Could not parse JSON preview: {exc}"}
    else:
        prev = {"preview_type": "file", "file_name": path.name}
    if file_id:
        prev["download_url"] = f"/api/download/{file_id}"
        prev["file_id"] = file_id
    return prev

def _resolve_optional_mesh_input(
    inputs: Dict[str, Any],
    input_name: str,
    file_id: str,
    fallback_stem: str,
) -> tuple[Optional[Path], str, Optional[Dict[str, Any]]]:
    """Resolve an optional mesh either from an input port or a file_select id.

    Returns (path, file_id_for_preview_endpoint, preview). If a connected mesh
    does not already have a registered file_id, it is written to a temporary VTK
    file and registered, so the PyVista popup endpoint can read it by file_id.
    """
    connected = inputs.get(input_name)
    rv = None
    if isinstance(connected, list):
        rv = connected[0] if connected else None
    elif connected is not None:
        rv = connected

    if rv is not None:
        if rv.kind not in {"mesh", "file"}:
            raise NodeExecutionError(f"Input port {input_name} expects mesh/file, got {rv.kind}")
        meta_file_id = str(rv.metadata.get("file_id") or "") if rv.metadata else ""
        if meta_file_id:
            path = get_file_path(meta_file_id)
            return path, meta_file_id, rv.preview if isinstance(rv.preview, dict) else None
        if rv.kind == "file" and isinstance(rv.value, (str, Path)):
            path = Path(rv.value)
            if not path.exists():
                raise NodeExecutionError(f"Connected file path does not exist for {input_name}: {path}")
            record = register_output_file(path, display_name=path.name)
            return get_file_path(record["file_id"]), record["file_id"], rv.preview if isinstance(rv.preview, dict) else None
        # Generic in-memory PyVista mesh: save and register it.
        suffix = ".vtp"
        try:
            if getattr(rv.value, "n_cells", 0) and rv.value.__class__.__name__.lower().find("unstructured") >= 0:
                suffix = ".vtu"
        except Exception:
            pass
        path = make_runtime_path(fallback_stem, suffix)
        try:
            rv.value.save(path)
        except Exception as exc:
            raise NodeExecutionError(f"Could not save connected mesh from port {input_name}: {exc}") from exc
        record = register_output_file(path, display_name=f"{fallback_stem}{suffix}")
        return get_file_path(record["file_id"]), record["file_id"], _mesh_preview(rv.value, rv.name or fallback_stem, record["file_id"])

    if file_id:
        path = get_file_path(file_id)
        return path, file_id, None
    return None, "", None





def _infer_geo_model_resolution(geo_model: Any) -> Optional[List[int]]:
    """Best-effort extraction of a regular-grid resolution from a GemPy GeoModel."""
    candidates = []
    try:
        candidates.append(getattr(geo_model.grid.regular_grid, "resolution", None))
    except Exception:
        pass
    try:
        candidates.append(getattr(geo_model.grid, "resolution", None))
    except Exception:
        pass
    try:
        candidates.append(getattr(geo_model, "resolution", None))
    except Exception:
        pass

    for value in candidates:
        if value is None:
            continue
        try:
            arr = np.asarray(value).astype(int).ravel()
            if arr.size == 3 and np.all(arr > 0):
                return [int(v) for v in arr.tolist()]
        except Exception:
            continue
    return None


def _geo_model_resolution_report(geo_model: Any) -> Dict[str, Any]:
    """Report regular-grid resolution provenance for model previews."""
    explicit = bool(getattr(geo_model, "_node_editor_resolution_was_explicit", False))
    explicit_resolution = getattr(geo_model, "_node_editor_resolution", None)
    inferred_resolution = _infer_geo_model_resolution(geo_model)

    if explicit_resolution is None and explicit and inferred_resolution is not None:
        explicit_resolution = inferred_resolution

    return {
        "explicit_resolution": explicit,
        "explicit_resolution_value": explicit_resolution,
        "inferred_regular_grid_resolution": inferred_resolution,
        "json_io_save_safe": bool(explicit and (explicit_resolution or inferred_resolution)),
        "note": "Resolution provenance is diagnostic; .gempy save uses GemPy's native serializer.",
    }


def _geo_model_basic_preview(geo_model: Any, name: str = "geo_model", source: str = "GemPy model") -> Dict[str, Any]:
    """Compact preview for a deserialized GeoModel."""
    project_name = str(getattr(geo_model, "project_name", "") or getattr(geo_model, "name", "") or name)
    elements = _extract_element_names_from_geo_model(geo_model) if "_extract_element_names_from_geo_model" in globals() else []
    groups = _structural_group_summary(geo_model) if "_structural_group_summary" in globals() else []
    try:
        extent = [float(v) for v in getattr(geo_model.grid.regular_grid, "extent", [])]
    except Exception:
        extent = []
    preview = {
        "project_name": project_name,
        "source": source,
        "extent": extent,
        "resolution_report": _geo_model_resolution_report(geo_model),
        "available_elements_from_geo_model": elements,
        "structural_groups": groups,
        "message": "GeoModel loaded. Its computed solution is not stored; connect it to Compute GemPy Model to recompute.",
    }
    return preview


class BaseNode:
    type_name = "BaseNode"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        raise NotImplementedError


class LoadUploadedFileNode(BaseNode):
    type_name = "LoadUploadedFile"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        file_id = params.get("file_id")
        if not file_id:
            raise NodeExecutionError("Choose an uploaded file in the node parameters.")
        path = get_file_path(file_id)
        requested_type = params.get("file_type", "auto")
        file_type = _infer_file_type_from_name(path.name, requested_type)
        base_file_preview = {
            "file_id": file_id,
            "file_name": path.name,
            "download_url": f"/api/download/{file_id}",
            "preview_type": "file",
        }
        outputs: Dict[str, RuntimeValue] = {
            "file": RuntimeValue("file", path, name=path.name, preview=base_file_preview, metadata={"file_id": file_id})
        }

        if file_type in {"csv", "xlsx", "excel"}:
            df = _read_table_from_path(path, file_type, params.get("sheet_name") or None)
            if isinstance(df, dict):
                if not df:
                    raise NodeExecutionError(f"Excel file {path.name} contains no sheets.")
                first_sheet = next(iter(df))
                df = df[first_sheet]
            preview = _spatial_table_preview(df, name=path.name, source_file_id=file_id, rows=15, generate_thumbnail=True)
            preview.update({"file_id": file_id, "download_url": f"/api/download/{file_id}", "file_name": path.name})
            outputs["table"] = RuntimeValue("table", df, name=path.name, preview=preview, metadata={"file_id": file_id})
            return outputs

        if file_type in {"mesh", "vtk", "vtp", "vtu", "vti", "stl", "ply", "obj"}:
            mesh = _read_mesh_from_path(path)
            preview = _mesh_preview(mesh, path.name, file_id)
            preview["file_type"] = file_type
            outputs["mesh"] = RuntimeValue("mesh", mesh, name=path.name, preview=preview, metadata={"file_id": file_id})
            outputs["file"] = RuntimeValue("file", path, name=path.name, preview=preview, metadata={"file_id": file_id})
            return outputs

        if file_type in {"raster", "tif", "tiff", "npy"}:
            preview = _file_preview(path, file_id=file_id)
            outputs["file"] = RuntimeValue("file", path, name=path.name, preview=preview, metadata={"file_id": file_id})
            return outputs

        return outputs



class LoadKadiFileNode(BaseNode):
    type_name = "LoadKadiFile"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            from kadi_apy import KadiManager
        except Exception as exc:
            raise NodeExecutionError("kadi_apy is not installed or not importable. Install requirements-gempy.txt and configure Kadi authentication.") from exc

        record_id = params.get("record_id")
        file_name = str(params.get("file_name") or "").strip()
        requested_type = params.get("file_type") or "auto"
        if not record_id or not file_name:
            raise NodeExecutionError("record_id and file_name are required for Kadi loading.")

        file_type = _infer_file_type_from_name(file_name, requested_type)
        suffix = Path(file_name).suffix.lower() or (".csv" if file_type == "csv" else ".xlsx" if file_type in {"xlsx", "excel"} else ".json" if file_type == "json" else ".vtk")

        with KadiManager() as manager:
            record = manager.record(id=int(record_id))
            kadi_file_id = record.get_file_id(file_name)
            response = record.download_file(kadi_file_id)
            response.raise_for_status()
            content = response.content

        local_path = make_runtime_path(Path(file_name).stem, suffix)
        local_path.write_bytes(content)
        stored_record = register_output_file(local_path, display_name=file_name)
        stored_path = get_file_path(stored_record["file_id"])

        if file_type == "csv":
            df = pd.read_csv(stored_path)
            return {
                "table": RuntimeValue("table", df, name=file_name, preview=_table_preview(df), metadata={"file_id": stored_record["file_id"], "kadi_record_id": int(record_id), "kadi_file_name": file_name}),
                "file": RuntimeValue("file", stored_path, name=file_name, preview={"file_id": stored_record["file_id"], "download_url": f"/api/download/{stored_record['file_id']}"}, metadata={"file_id": stored_record["file_id"]}),
            }
        if file_type in {"xlsx", "excel"}:
            sheet_name = params.get("sheet_name") or None
            # Important: do not pass sheet_name=None to pandas.read_excel.
            # pandas interprets sheet_name=None as "read all sheets" and returns
            # a dict[str, DataFrame], which then breaks table preview.  With no
            # sheet specified we intentionally match Load Uploaded File behavior:
            # read the first sheet only.
            df = _read_table_from_path(stored_path, "xlsx", sheet_name=sheet_name)
            if isinstance(df, dict):
                # Defensive fallback for older code paths / pandas behavior.
                if not df:
                    raise NodeExecutionError(f"Excel file {file_name} contains no sheets.")
                first_sheet = next(iter(df))
                df = df[first_sheet]
            return {
                "table": RuntimeValue("table", df, name=file_name, preview=_table_preview(df), metadata={"file_id": stored_record["file_id"], "kadi_record_id": int(record_id), "kadi_file_name": file_name}),
                "file": RuntimeValue("file", stored_path, name=file_name, preview={"file_id": stored_record["file_id"], "download_url": f"/api/download/{stored_record['file_id']}"}, metadata={"file_id": stored_record["file_id"]}),
            }
        if file_type in {"mesh", "vtk", "vtp", "vtu", "vti", "stl", "ply", "obj"}:
            mesh = _read_mesh_from_path(stored_path)
            preview = _mesh_preview(mesh, file_name, stored_record["file_id"])
            preview.update({"kadi_record_id": int(record_id), "kadi_file_name": file_name, "file_type": file_type})
            return {
                "mesh": RuntimeValue("mesh", mesh, name=file_name, preview=preview, metadata={"file_id": stored_record["file_id"], "kadi_record_id": int(record_id), "kadi_file_name": file_name}),
                "file": RuntimeValue("file", stored_path, name=file_name, preview={"file_id": stored_record["file_id"], "download_url": f"/api/download/{stored_record['file_id']}", **preview}, metadata={"file_id": stored_record["file_id"]}),
            }
        if file_type in {"gempy", "json"}:
            preview = {"file_id": stored_record["file_id"], "download_url": f"/api/download/{stored_record['file_id']}", "file_type": file_type}
            return {"file": RuntimeValue("file", stored_path, name=file_name, preview=preview, metadata=preview)}
        raise NodeExecutionError(f"Unsupported Kadi file type: {file_type}")


class MergeTablesNode(BaseNode):
    type_name = "MergeTables"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        values = _many(inputs, "tables")
        if not values:
            raise NodeExecutionError("MergeTables requires at least one input table.")

        records = []
        for idx, v in enumerate(values):
            if v.kind != "table":
                raise NodeExecutionError(f"MergeTables expects table inputs, got {v.kind}")
            source_name = v.name or f"input_{idx}"
            records.append({"index": idx, "name": source_name, "value": v})

        manual_order_raw = str(params.get("manual_order") or "").strip()
        order_tokens = [t.strip() for t in manual_order_raw.replace(";", ",").split(",") if t.strip()]
        warnings = []
        if order_tokens:
            ordered = []
            used = set()
            for token in order_tokens:
                match_i = None
                for i, rec in enumerate(records):
                    if i in used:
                        continue
                    name = str(rec["name"])
                    # Accept exact name, substring of filename, or original input index such as 0/1/2.
                    if token == name or token in name or token == str(rec["index"]):
                        match_i = i
                        break
                if match_i is None:
                    warnings.append(f"Manual order token not matched: {token}")
                else:
                    ordered.append(records[match_i])
                    used.add(match_i)
            for i, rec in enumerate(records):
                if i not in used:
                    ordered.append(rec)
            records = ordered

        add_source = _as_bool(params.get("add_source_column"), False)
        source_col = str(params.get("source_column") or "_merge_source")
        dfs = []
        input_order = []
        for out_idx, rec in enumerate(records):
            df = rec["value"].value.copy() if add_source else rec["value"].value
            if add_source:
                df[source_col] = rec["name"]
            dfs.append(df)
            input_order.append({
                "merge_position": out_idx,
                "original_input_position": rec["index"],
                "source": rec["name"],
                "rows": int(len(rec["value"].value)),
                "columns": list(map(str, rec["value"].value.columns)),
            })

        merged = pd.concat(dfs, axis=int(params.get("axis", 0)), ignore_index=_as_bool(params.get("ignore_index"), True))
        report = {
            "input_order": input_order,
            "manual_order_used": order_tokens,
            "warnings": warnings,
            "merged_rows": int(len(merged)),
            "merged_columns": list(map(str, merged.columns)),
            "source_column_added": add_source,
            "source_column": source_col if add_source else None,
        }
        return {
            "table": RuntimeValue("table", merged, name="merged_table", preview=_table_preview(merged)),
            "report": RuntimeValue("report", report, name="merge_report", preview=_report_preview(report)),
        }


class ValidateGeoTableNode(BaseNode):
    type_name = "ValidateGeoTable"

    REQUIRED = {
        "surface_points": ["X", "Y", "Z", "formation"],
        "orientations": ["X", "Y", "Z", "G_x", "G_y", "G_z", "formation"],
        "fault_points": ["X", "Y", "Z", "formation"],
        "fault_orientations": ["X", "Y", "Z", "G_x", "G_y", "G_z", "formation"],
    }

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"ValidateGeoTable expects table input, got {rv.kind}")
        df = rv.value
        table_kind = params.get("table_kind", "auto")
        if table_kind == "auto":
            cols_lower = {str(c).strip().lower() for c in df.columns}
            has_grad = bool({"g_x", "g_y", "g_z"}.issubset(cols_lower) or {"gx", "gy", "gz"}.issubset(cols_lower))
            has_azdip = bool({"azimuth", "dip"}.issubset(cols_lower))
            table_kind = "orientations" if (has_grad or has_azdip) else "surface_points"
        report: Dict[str, Any] = geological_table_report(df, table_kind)
        report["table_kind"] = table_kind
        report["summary"] = _table_preview(df)
        return {
            "table": RuntimeValue("table", df, name=rv.name, preview=_table_preview(df)),
            "report": RuntimeValue("report", report, name="validation_report", preview=_report_preview(report)),
        }



def _find_orientation_angle_columns(df: pd.DataFrame, azimuth_col: str = "auto", dip_col: str = "auto", polarity_col: str = "auto") -> Dict[str, Optional[str]]:
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}

    def pick(requested: str, aliases: List[str]) -> Optional[str]:
        req = str(requested or "auto").strip()
        if req and req.lower() not in {"auto", ""}:
            if req in df.columns:
                return req
            low = req.lower()
            if low in lookup:
                return lookup[low]
            return None
        for alias in aliases:
            if alias.lower() in lookup:
                return lookup[alias.lower()]
        return None

    return {
        "azimuth": pick(azimuth_col, ["azimuth", "az", "dip_direction", "dipdirection", "direction"]),
        "dip": pick(dip_col, ["dip", "dip_angle", "dipangle", "inclination"]),
        "polarity": pick(polarity_col, ["polarity", "pole_polarity", "sense"]),
    }


def _find_gradient_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}
    return {
        "G_x": lookup.get("g_x") or lookup.get("gx"),
        "G_y": lookup.get("g_y") or lookup.get("gy"),
        "G_z": lookup.get("g_z") or lookup.get("gz"),
    }


def _compute_gempy_gradient_from_dip_azimuth(
    azimuth_deg: Any,
    dip_deg: Any,
    polarity: Any = None,
    *,
    azimuth_is_dip_direction: bool = True,
    normalize: bool = True,
) -> np.ndarray:
    az = np.deg2rad(pd.to_numeric(azimuth_deg, errors="coerce").to_numpy(dtype=float))
    dip = np.deg2rad(pd.to_numeric(dip_deg, errors="coerce").to_numpy(dtype=float))
    if polarity is None:
        pol = np.ones_like(az, dtype=float)
    else:
        pol = pd.to_numeric(polarity, errors="coerce").fillna(1.0).to_numpy(dtype=float)

    # Convention: GemPy needs pole/gradient vector columns G_x/G_y/G_z.
    # Default assumes azimuth is dip direction, measured clockwise from north.
    # This matches the common GemPy-style conversion:
    #   G_x = sin(dip) * sin(azimuth)
    #   G_y = sin(dip) * cos(azimuth)
    #   G_z = cos(dip)
    # If the azimuth is strike, convert strike to dip direction by +90°.
    if not bool(azimuth_is_dip_direction):
        az = az + np.pi / 2.0

    gx = np.sin(dip) * np.sin(az) * pol
    gy = np.sin(dip) * np.cos(az) * pol
    gz = np.cos(dip) * pol
    out = np.column_stack([gx, gy, gz]).astype(float)

    if normalize:
        norms = np.linalg.norm(out, axis=1)
        valid = np.isfinite(norms) & (norms > 0)
        out[valid] = out[valid] / norms[valid, None]
    return out


class ConvertOrientationAnglesNode(BaseNode):
    type_name = "ConvertOrientationAngles"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"ConvertOrientationAngles expects table input, got {rv.kind}")

        df = rv.value.copy()
        angle_cols = _find_orientation_angle_columns(
            df,
            azimuth_col=str(params.get("azimuth_col") or "auto"),
            dip_col=str(params.get("dip_col") or "auto"),
            polarity_col=str(params.get("polarity_col") or "auto"),
        )
        grad_cols = _find_gradient_columns(df)

        if not angle_cols["azimuth"] or not angle_cols["dip"]:
            raise NodeExecutionError(
                "Convert Orientation Angles needs azimuth and dip columns. "
                f"Found azimuth={angle_cols['azimuth']}, dip={angle_cols['dip']}."
            )

        # Standardize pre-existing gradient columns if they use Gx/Gy/Gz.
        for canonical, existing in grad_cols.items():
            if existing and existing != canonical:
                if canonical not in df.columns:
                    df[canonical] = df[existing]
                elif _as_bool(params.get("overwrite_existing_gradients"), False):
                    df[canonical] = df[existing]

        for c in ["G_x", "G_y", "G_z"]:
            if c not in df.columns:
                df[c] = np.nan
            df[c] = pd.to_numeric(df[c], errors="coerce")

        az_col = angle_cols["azimuth"]
        dip_col = angle_cols["dip"]
        pol_col = angle_cols["polarity"]

        vectors = _compute_gempy_gradient_from_dip_azimuth(
            df[az_col],
            df[dip_col],
            df[pol_col] if pol_col else None,
            azimuth_is_dip_direction=_as_bool(params.get("azimuth_is_dip_direction"), True),
            normalize=_as_bool(params.get("normalize_vectors"), True),
        )

        has_existing = df[["G_x", "G_y", "G_z"]].notna().all(axis=1)
        valid_angles = pd.to_numeric(df[az_col], errors="coerce").notna() & pd.to_numeric(df[dip_col], errors="coerce").notna()
        overwrite = _as_bool(params.get("overwrite_existing_gradients"), False)
        target_mask = valid_angles if overwrite else (valid_angles & ~has_existing)

        df.loc[target_mask, "G_x"] = vectors[target_mask.to_numpy(), 0]
        df.loc[target_mask, "G_y"] = vectors[target_mask.to_numpy(), 1]
        df.loc[target_mask, "G_z"] = vectors[target_mask.to_numpy(), 2]

        # Normalize formation names to string to avoid GemPy mixed-type KeyError.
        formation_col = str(params.get("formation_col") or "formation").strip()
        if formation_col and formation_col in df.columns and _as_bool(params.get("formation_to_str"), True):
            df[formation_col] = _normalize_formation_series(df[formation_col])

        if _as_bool(params.get("drop_angle_columns"), False):
            drop_cols = [c for c in [az_col, dip_col, pol_col] if c and c in df.columns]
            df = df.drop(columns=drop_cols)

        if _as_bool(params.get("drop_incomplete_gradient_rows"), False):
            before_drop = len(df)
            df = df.dropna(subset=["G_x", "G_y", "G_z"]).reset_index(drop=True)
        else:
            before_drop = len(df)

        converted_count = int(target_mask.sum())
        report = {
            "input_rows": int(len(rv.value)),
            "output_rows": int(len(df)),
            "dropped_rows": int(before_drop - len(df)),
            "azimuth_column": az_col,
            "dip_column": dip_col,
            "polarity_column": pol_col,
            "azimuth_interpreted_as": "dip_direction" if _as_bool(params.get("azimuth_is_dip_direction"), True) else "strike_plus_90_degrees",
            "existing_gradient_columns_detected": grad_cols,
            "overwrite_existing_gradients": overwrite,
            "rows_with_existing_G_before": int(has_existing.sum()),
            "rows_with_valid_angles": int(valid_angles.sum()),
            "rows_converted_to_G_x_G_y_G_z": converted_count,
            "rows_with_complete_G_after": int(df[["G_x", "G_y", "G_z"]].notna().all(axis=1).sum()),
            "gradient_columns": ["G_x", "G_y", "G_z"],
            "formula": "G_x=sin(dip)*sin(azimuth), G_y=sin(dip)*cos(azimuth), G_z=cos(dip), optionally multiplied by polarity.",
        }

        preview = _table_preview(df)
        preview["name"] = "orientation_gradients_converted"
        preview["conversion_report"] = report
        return {
            "table": RuntimeValue("table", df, name="orientation_gradients_converted", preview=preview),
            "report": RuntimeValue("report", report, name="orientation_conversion_report", preview=_report_preview(report), metadata=report),
        }


class CleanGeoTableNode(BaseNode):
    type_name = "CleanGeoTable"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"CleanGeoTable expects table input, got {rv.kind}")
        df = rv.value.copy()
        coord_cols = [c.strip() for c in str(params.get("coord_cols", "X,Y,Z")).split(",") if c.strip()]
        grad_cols = [c.strip() for c in str(params.get("grad_cols", "G_x,G_y,G_z")).split(",") if c.strip()]
        formation_col = str(params.get("formation_col") or "formation").strip()
        converted_formation_to_str = False
        unique_formations_before = []
        unique_formations_after = []
        if _as_bool(params.get("formation_to_str"), True) and formation_col and formation_col in df.columns:
            try:
                unique_formations_before = [str(v) for v in pd.unique(df[formation_col].dropna())[:20]]
            except Exception:
                unique_formations_before = []
            # GemPy expects formation names to be strings. Numeric formation names
            # such as 0.0 can lead to name_id_map KeyError when surface points and
            # orientations are parsed with mixed dtype. Keep missing values missing,
            # but force all valid names to Python strings.
            df[formation_col] = df[formation_col].where(df[formation_col].isna(), df[formation_col].astype(str))
            converted_formation_to_str = True
            try:
                unique_formations_after = [str(v) for v in pd.unique(df[formation_col].dropna())[:20]]
            except Exception:
                unique_formations_after = []
        if _as_bool(params.get("numeric_xyz"), True):
            for col in coord_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
        if _as_bool(params.get("numeric_gradients"), True):
            for col in grad_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
        before = len(df)
        if _as_bool(params.get("drop_missing_xyz"), True) and all(c in df.columns for c in coord_cols):
            df = df.dropna(subset=coord_cols)
        removed_missing = before - len(df)
        removed_zero = 0
        if _as_bool(params.get("remove_zero_gradients"), False) and all(c in df.columns for c in grad_cols):
            before2 = len(df)
            grads = df[grad_cols].fillna(0).abs().sum(axis=1)
            df = df.loc[grads != 0].copy()
            removed_zero = before2 - len(df)
        report = {
            "rows_before": before,
            "rows_after": len(df),
            "removed_missing_xyz": int(removed_missing),
            "removed_zero_gradients": int(removed_zero),
            "formation_column": formation_col,
            "formation_to_str": bool(converted_formation_to_str),
            "formation_unique_before_sample": unique_formations_before,
            "formation_unique_after_sample": unique_formations_after,
            "note": "formation_to_str avoids GemPy name_id_map KeyError when formation names are numeric, e.g. 0.0.",
        }
        return {
            "table": RuntimeValue("table", df.reset_index(drop=True), name=f"cleaned_{rv.name}", preview=_table_preview(df)),
            "report": RuntimeValue("report", report, name="cleaning_report", preview=report),
        }



def _sample_take_counts(
    df: pd.DataFrame,
    group_col: str,
    n: int,
    allocation: str = "equal",
) -> Dict[Any, int]:
    """Compute per-group sample counts without expensive clustering."""
    if group_col not in df.columns:
        return {"__all__": min(int(n), len(df))}

    level_sizes = df[group_col].value_counts(dropna=False)
    levels = level_sizes.index.tolist()
    if not levels:
        return {}

    n = min(int(n), int(level_sizes.sum()))
    allocation = str(allocation or "equal").strip().lower()

    if allocation == "proportional":
        props = level_sizes / level_sizes.sum()
        raw = props * n
        alloc = np.floor(raw).astype(int)
        remainder = int(n - alloc.sum())
        frac = (raw - alloc).sort_values(ascending=False)
        for lvl in frac.index[:remainder]:
            alloc[lvl] += 1
    else:
        # For "equal" and also the old expensive "min_distance" option, use a
        # safe equal allocation.  The actual point selection below is spatially
        # spread but avoids long KMeans/min-distance loops.
        base = n // len(levels)
        rem = n % len(levels)
        alloc = pd.Series({lvl: base for lvl in levels})
        for lvl in level_sizes.sort_values(ascending=False).index.tolist()[:rem]:
            alloc[lvl] += 1

    take = alloc.clip(upper=level_sizes)
    leftover = n - int(take.sum())
    if leftover > 0:
        capacity = (level_sizes - take).to_dict()
        while leftover > 0:
            progressed = False
            for lvl in sorted(capacity, key=lambda x: capacity[x], reverse=True):
                if capacity[lvl] > 0:
                    take[lvl] += 1
                    capacity[lvl] -= 1
                    leftover -= 1
                    progressed = True
                    if leftover == 0:
                        break
            if not progressed:
                break
    return {lvl: int(v) for lvl, v in take.to_dict().items()}


def _prepare_numeric_features_for_sampling(
    df: pd.DataFrame,
    feature_cols: List[str],
    na_strategy: str,
    scale: str,
) -> tuple[pd.DataFrame, np.ndarray]:
    feats = [c for c in feature_cols if c in df.columns]
    if not feats:
        return df, np.empty((len(df), 0), dtype=float)

    num = df[feats].apply(pd.to_numeric, errors="coerce")
    if na_strategy == "drop":
        valid = ~num.isna().any(axis=1)
        df2 = df.loc[valid]
        num = num.loc[valid]
    else:
        # Median can be NaN for all-NaN columns; fill those with zero to avoid
        # sklearn/numpy stalls on invalid values.
        med = num.median(numeric_only=True).fillna(0.0)
        num = num.fillna(med).fillna(0.0)
        df2 = df.loc[num.index]

    X = num.to_numpy(dtype=np.float64, copy=True)
    if X.size and str(scale or "none").lower() == "standard":
        mu = np.nanmean(X, axis=0, keepdims=True)
        sigma = np.nanstd(X, axis=0, keepdims=True)
        sigma[~np.isfinite(sigma) | (sigma == 0)] = 1.0
        X = (X - mu) / sigma
    return df2, X


def _fast_representative_indices(
    df: pd.DataFrame,
    X: np.ndarray,
    k: int,
    random_state: Optional[int],
) -> List[Any]:
    """Fast deterministic spatially-spread representative sample.

    Avoids sklearn KMeans, which can hang or become very slow on some Windows /
    BLAS combinations and very large groups.  It selects quantile representatives
    along a stable spatial score and fills remaining rows deterministically.
    """
    if k <= 0 or len(df) == 0:
        return []
    if k >= len(df):
        return df.index.tolist()
    if X.size == 0 or X.shape[1] == 0:
        return df.sample(n=k, random_state=random_state).index.tolist()

    X = np.asarray(X, dtype=np.float64)
    X[~np.isfinite(X)] = 0.0

    # Stable score: use the dominant spread direction if cheap enough, otherwise
    # use a deterministic weighted projection. SVD is only applied to at most a
    # small subset so large geological tables stay responsive.
    try:
        if len(X) <= 50000 and X.shape[1] <= 8:
            Xc = X - X.mean(axis=0, keepdims=True)
            _u, _s, vh = np.linalg.svd(Xc, full_matrices=False)
            w = vh[0]
        else:
            w = np.arange(1, X.shape[1] + 1, dtype=float)
            w = w / np.linalg.norm(w)
    except Exception:
        w = np.arange(1, X.shape[1] + 1, dtype=float)
        w = w / np.linalg.norm(w)

    score = X @ w
    order = np.argsort(score, kind="mergesort")
    if k == 1:
        positions = [len(order) // 2]
    else:
        positions = np.linspace(0, len(order) - 1, k).round().astype(int).tolist()

    chosen_pos = []
    seen = set()
    for p in positions:
        p = int(max(0, min(len(order) - 1, p)))
        if p not in seen:
            seen.add(p)
            chosen_pos.append(p)

    chosen = [df.index[order[p]] for p in chosen_pos]
    if len(chosen) < k:
        pool = df.drop(index=chosen, errors="ignore")
        add = pool.sample(n=min(k - len(chosen), len(pool)), random_state=random_state).index.tolist()
        chosen.extend(add)
    return chosen[:k]


def fast_stratified_geo_sample(
    df: pd.DataFrame,
    group_col: str,
    feature_cols: Optional[List[str]] = None,
    n: int = 100,
    random_state: Optional[int] = None,
    allocation: str = "equal",
    na_strategy: str = "median",
    scale: str = "none",
    method: str = "fast",
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Responsive sampler for geological tables.

    method:
      - fast: formation-aware representative sampling without sklearn
      - random: formation-aware random sampling
      - kmeans: old sklearn KMeans sampler
    """
    n = int(n or 0)
    method = str(method or "fast").strip().lower()
    if n <= 0 or df.empty:
        return df.iloc[0:0].copy(), {"method": method, "target_rows": n, "selected_rows": 0}
    if n >= len(df):
        return df.reset_index(drop=True), {"method": method, "target_rows": n, "selected_rows": int(len(df)), "note": "n >= input rows; returned all rows."}

    if method == "kmeans":
        sampled = stratified_kmeans_sample(
            df=df,
            group_col=group_col,
            feature_cols=feature_cols,
            n=n,
            random_state=random_state,
            allocation=allocation,
            na_strategy=na_strategy,
            scale=scale,
        )
        return sampled, {"method": "kmeans", "target_rows": n, "selected_rows": int(len(sampled))}

    feature_cols = feature_cols or [c for c in df.select_dtypes(include=np.number).columns.tolist() if c != group_col]
    take = _sample_take_counts(df, group_col, n, allocation)

    parts = []
    group_reports = []

    if group_col not in df.columns:
        groups = [("__all__", df)]
    else:
        groups = list(df.groupby(group_col, sort=False, dropna=False))

    for lvl, g in groups:
        k_i = int(take.get(lvl, 0) if group_col in df.columns else take.get("__all__", n))
        if k_i <= 0 or g.empty:
            continue
        if k_i >= len(g):
            parts.append(g)
            group_reports.append({"group": str(lvl), "input_rows": int(len(g)), "selected_rows": int(len(g)), "mode": "all"})
            continue

        if method == "random":
            chosen = g.sample(n=k_i, random_state=random_state).index.tolist()
        else:
            g2, X = _prepare_numeric_features_for_sampling(g, feature_cols, na_strategy, scale)
            if len(g2) == 0:
                chosen = g.sample(n=min(k_i, len(g)), random_state=random_state).index.tolist()
            else:
                chosen = _fast_representative_indices(g2, X, k_i, random_state)

        parts.append(df.loc[chosen])
        group_reports.append({"group": str(lvl), "input_rows": int(len(g)), "selected_rows": int(len(chosen)), "mode": method})

    if not parts:
        return df.iloc[0:0].copy(), {"method": method, "target_rows": n, "selected_rows": 0, "groups": group_reports}

    out = pd.concat(parts, ignore_index=True)
    if len(out) > n:
        out = out.sample(n=n, random_state=random_state).reset_index(drop=True)
    else:
        out = out.reset_index(drop=True)

    report = {
        "method": method,
        "target_rows": int(n),
        "selected_rows": int(len(out)),
        "group_col": group_col,
        "feature_cols": feature_cols,
        "allocation": allocation,
        "na_strategy": na_strategy,
        "scale": scale,
        "groups": group_reports,
        "note": "Default fast method avoids sklearn KMeans/min-distance loops, so small samples from large tables remain responsive.",
    }
    return out, report



class SampleGeoTableNode(BaseNode):
    type_name = "SampleGeoTable"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"SampleGeoTable expects table input, got {rv.kind}")

        df = rv.value.copy()
        n = _as_int(params.get("n"), 100) or 100
        group_col = params.get("group_col") or "formation"
        feature_cols = [c.strip() for c in str(params.get("feature_cols", "X,Y,Z")).split(",") if c.strip()]
        allocation = params.get("allocation") or "equal"
        random_state = _as_int(params.get("random_state"), 42)
        na_strategy = params.get("na_strategy") or "median"
        scale = params.get("scale") or "none"
        method = params.get("method") or "fast"

        sampled, report = fast_stratified_geo_sample(
            df=df,
            group_col=group_col,
            feature_cols=feature_cols,
            n=n,
            random_state=random_state,
            allocation=allocation,
            na_strategy=na_strategy,
            scale=scale,
            method=method,
        )

        preview = _table_preview(sampled)
        preview.update({
            "sampling_report": report,
            "input_rows": int(len(df)),
            "output_rows": int(len(sampled)),
        })

        return {
            "table": RuntimeValue("table", sampled, name=f"sampled_{rv.name}", preview=preview, metadata={"sampling_report": report}),
            "report": RuntimeValue("report", report, name="sampling_report", preview=report, metadata=report),
        }


class PreviewDataNode(BaseNode):
    type_name = "Visualization"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rows = int(_as_int(params.get("rows"), 15) or 15)
        scalars = str(params.get("mesh_scalars") or "auto")
        show_edges = _as_bool(params.get("show_edges"), False)
        generate_thumbnail = _as_bool(params.get("generate_thumbnail"), True)
        filter_by_scalar_values = _as_bool(params.get("filter_by_scalar_values"), False)
        scalar_filter_values = params.get("scalar_filter_values", "")

        candidates: List[tuple[str, RuntimeValue]] = []

        # v31 primary input: one universal port.
        for rv in _many(inputs, "data"):
            candidates.append(("data", rv))

        # Backward compatibility for graphs created with v30 multi-input PreviewData.
        if not candidates:
            for port in ["table", "mesh", "file", "array", "voxel_model", "report", "geo_model", "solution"]:
                for rv in _many(inputs, port):
                    candidates.append((port, rv))

        if not candidates:
            preview = {
                "preview_type": "empty",
                "message": "Connect any output to the single Data input of this Visualization node, then run to this node.",
            }
            return {"preview": RuntimeValue("report", preview, name="empty_preview", preview=preview, metadata=preview)}

        port, rv = candidates[0]
        kind = rv.kind
        name = rv.name or port
        preview: Dict[str, Any] = {"preview_type": "generic", "source_port": port, "source_kind": kind, "name": name}

        try:
            if kind == "table":
                source_file_id = str(rv.metadata.get("file_id") or "") if rv.metadata else ""
                preview = _spatial_table_preview(
                    rv.value,
                    name=name,
                    source_file_id=source_file_id or None,
                    rows=rows,
                    generate_thumbnail=generate_thumbnail,
                    show_edges=show_edges,
                )
                preview.update({"source_port": port, "source_kind": kind, "name": name})

            elif kind == "mesh":
                file_id = str(rv.metadata.get("file_id") or "") if rv.metadata else ""
                mesh = rv.value
                preview = _visualization_mesh_preview(
                    mesh,
                    name=name,
                    file_id=file_id,
                    scalars=scalars,
                    show_edges=show_edges,
                    generate_thumbnail=generate_thumbnail,
                    filter_by_scalar_values=filter_by_scalar_values,
                    scalar_filter_values=scalar_filter_values,
                )
                preview.update({"preview_type": "mesh", "source_port": port, "source_kind": kind})

            elif kind == "file":
                file_id = str(rv.metadata.get("file_id") or "") if rv.metadata else ""
                path = get_file_path(file_id) if file_id else Path(rv.value)
                if not file_id:
                    rec = register_output_file(path, display_name=path.name)
                    file_id = rec["file_id"]
                    path = get_file_path(file_id)
                if filter_by_scalar_values:
                    if _infer_file_type_from_name(path.name, "auto") != "mesh":
                        raise NodeExecutionError("Scalar-value filtering is available only for mesh files or mesh outputs.")
                    preview = _visualization_mesh_preview(
                        _read_mesh_from_path(path),
                        name=path.name,
                        file_id=file_id,
                        scalars=scalars,
                        show_edges=show_edges,
                        generate_thumbnail=generate_thumbnail,
                        filter_by_scalar_values=True,
                        scalar_filter_values=scalar_filter_values,
                    )
                    preview["preview_type"] = "mesh_file"
                else:
                    preview = _file_preview(path, file_id=file_id, rows=rows, scalars=scalars, show_edges=show_edges)
                preview.update({"source_port": port, "source_kind": kind, "name": name})

            elif kind == "array":
                preview = _array_preview(rv.value, name=name, rows=rows)
                preview.update({"source_port": port, "source_kind": kind})

            elif kind == "voxel_model":
                path = Path(rv.value)
                file_id = str(rv.metadata.get("file_id") or "") if rv.metadata else ""
                if path.exists():
                    arr = np.load(path)
                    preview = _array_preview(arr, name=name, rows=rows)
                    if file_id:
                        preview["pyvista_preview_url"] = f"/api/pyvista/voxel/{file_id}?reshape=&show_edges={str(show_edges).lower()}"
                        preview["pyvista_button_label"] = "Enlarge / Open Voxel 3D"
                else:
                    preview = {"preview_type": "voxel_model", "name": name, "message": "Voxel model file path is not available."}

            elif kind == "geo_model":
                token = put_runtime_object(rv.value, kind="geo_model", name=name)
                preview = {
                    "preview_type": "geo_model",
                    "name": name,
                    "runtime_token": token,
                    "pyvista_preview_url": f"/api/pyvista/gempy-model/{token}",
                    "pyvista_button_label": "Enlarge / Open GemPy 3D",
                    "message": "GeoModel preview is available as an interactive 3D popup.",
                }

            elif kind == "gempy_solution":
                raw_arrays = getattr(rv.value, "raw_arrays", None)
                preview = {
                    "preview_type": "gempy_solution",
                    "name": name,
                    "available_raw_arrays": [k for k in dir(raw_arrays) if not k.startswith("_")] if raw_arrays is not None else [],
                    "message": "Connect Extract GemPy Array or Voxel Model Viewer for array/voxel previews.",
                }

            elif kind == "report":
                preview = rv.preview if isinstance(rv.preview, dict) else {"preview_type": "report", "report": rv.value}
                preview.update({"source_port": port, "source_kind": kind, "name": name})
            else:
                preview = {
                    "preview_type": "generic",
                    "source_port": port,
                    "source_kind": kind,
                    "name": name,
                    "summary": rv.preview if rv.preview is not None else str(type(rv.value)),
                }
        except Exception as exc:
            raise NodeExecutionError(f"PreviewData failed for {name} ({kind}): {exc}") from exc

        preview.setdefault("preview_node", True)
        return {"preview": RuntimeValue("report", preview, name=f"preview_{name}", preview=preview, metadata=preview)}



class PreviewTableNode(BaseNode):
    type_name = "PreviewTable"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"PreviewTable expects table input, got {rv.kind}")
        df = rv.value
        preview = _table_preview(df, rows=_as_int(params.get("rows"), 15) or 15)
        preview = _attach_pyvista_preview(preview, df, rv.name or "table")
        return {"report": RuntimeValue("report", preview, name=f"preview_{rv.name}", preview=preview)}


class SaveTableCsvNode(BaseNode):
    type_name = "SaveTableCsv"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "table")
        if rv.kind != "table":
            raise NodeExecutionError(f"SaveTableCsv expects table input, got {rv.kind}")
        file_name = params.get("file_name") or f"{rv.name or 'table'}.csv"
        if not str(file_name).lower().endswith(".csv"):
            file_name = f"{file_name}.csv"
        path = make_runtime_path(Path(file_name).stem, ".csv")
        rv.value.to_csv(path, index=False)
        record = register_output_file(path, display_name=file_name)
        preview = {"file_id": record["file_id"], "download_url": f"/api/download/{record['file_id']}", "file_name": file_name}
        return {"file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview)}




def _find_column_case_insensitive(df: pd.DataFrame, names: Sequence[str]) -> Optional[str]:
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}
    for name in names:
        key = str(name).strip().lower()
        if key in lookup:
            return lookup[key]
    return None


def _azimuth_dip_polarity_to_gradient(
    azimuth: Any,
    dip: Any,
    polarity: Any = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert azimuth/dip/polarity orientation format to GemPy G_x/G_y/G_z.

    Convention used here:
    - azimuth is dip direction, in degrees clockwise from north/Y.
    - dip is angle from horizontal, in degrees.
    - polarity multiplies the resulting normal vector; missing polarity defaults to +1.

    This gives a vertical upward normal for horizontal bedding, e.g. dip=0:
        G_x=0, G_y=0, G_z=+1
    """
    az = np.deg2rad(pd.to_numeric(azimuth, errors="coerce").astype(float))
    dp = np.deg2rad(pd.to_numeric(dip, errors="coerce").astype(float))
    if polarity is None:
        pol = np.ones_like(az, dtype=float)
    else:
        pol = pd.to_numeric(polarity, errors="coerce").fillna(1.0).astype(float).to_numpy()

    gx = np.sin(dp) * np.sin(az) * pol
    gy = np.sin(dp) * np.cos(az) * pol
    gz = np.cos(dp) * pol
    return gx, gy, gz


def _standardize_orientation_table_for_gempy(df: pd.DataFrame) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Return a copy with canonical GemPy orientation columns G_x/G_y/G_z.

    Accepted inputs:
    1. X,Y,Z,formation,G_x,G_y,G_z
    2. X,Y,Z,formation,Gx,Gy,Gz / gx,gy,gz
    3. X,Y,Z,formation,azimuth,dip,polarity(optional)

    The original azimuth/dip/polarity columns are kept for traceability, but
    GemPy receives the generated G_x/G_y/G_z columns.
    """
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    gx_col = _find_column_case_insensitive(out, ["G_x", "Gx", "gx", "g_x"])
    gy_col = _find_column_case_insensitive(out, ["G_y", "Gy", "gy", "g_y"])
    gz_col = _find_column_case_insensitive(out, ["G_z", "Gz", "gz", "g_z"])

    report: Dict[str, Any] = {
        "input_columns": list(map(str, df.columns)),
        "output_columns": None,
        "conversion": None,
        "orientation_format": None,
    }

    if gx_col and gy_col and gz_col:
        out["G_x"] = pd.to_numeric(out[gx_col], errors="coerce").astype(float)
        out["G_y"] = pd.to_numeric(out[gy_col], errors="coerce").astype(float)
        out["G_z"] = pd.to_numeric(out[gz_col], errors="coerce").astype(float)
        report.update({
            "orientation_format": "gradient",
            "conversion": "gradient_columns_canonicalized",
            "source_columns": {"G_x": gx_col, "G_y": gy_col, "G_z": gz_col},
        })
    else:
        az_col = _find_column_case_insensitive(out, ["azimuth", "az", "dip_azimuth"])
        dip_col = _find_column_case_insensitive(out, ["dip"])
        pol_col = _find_column_case_insensitive(out, ["polarity", "pole", "direction"])
        if not (az_col and dip_col):
            raise NodeExecutionError(
                "Invalid orientations input: expected either G_x/G_y/G_z "
                "or azimuth/dip/polarity(optional) columns."
            )

        polarity = out[pol_col] if pol_col else None
        gx, gy, gz = _azimuth_dip_polarity_to_gradient(out[az_col], out[dip_col], polarity)
        out["G_x"] = gx
        out["G_y"] = gy
        out["G_z"] = gz
        report.update({
            "orientation_format": "azimuth_dip",
            "conversion": "azimuth_dip_polarity_to_G_x_G_y_G_z",
            "source_columns": {"azimuth": az_col, "dip": dip_col, "polarity": pol_col},
            "conversion_convention": "azimuth=dip direction clockwise from north/Y; dip=angle from horizontal; polarity multiplies the normal vector",
        })

    report["output_columns"] = list(map(str, out.columns))
    return out, report



def _normalize_gempy_table_for_import(df: pd.DataFrame, kind: str = "surface") -> pd.DataFrame:
    """Return a GemPy-import-safe copy of a geological table.

    This avoids platform-dependent pandas extension string dtypes being passed
    into GemPy as pandas.arrays.StringArray.

    Important: orientation tables are kept in their native GemPy-supported
    format.  If the user provides azimuth/dip/polarity, those columns are passed
    to GemPy unchanged instead of being converted to G_x/G_y/G_z.
    """
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    # Normalize common numeric columns only when they are present.
    for col in ["X", "Y", "Z", "G_x", "G_y", "G_z", "Gx", "Gy", "Gz", "gx", "gy", "gz", "azimuth", "dip", "polarity"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)

    def _normalize_name_value(v):
        if pd.isna(v):
            return ""
        text = str(v).strip()
        try:
            f = float(text)
            if f.is_integer():
                return str(int(f))
            return str(f)
        except Exception:
            return text

    # GemPy expects formation/name data as Python strings or NumPy arrays.
    # Normalize numeric-like names consistently: 0, 0.0 and "0.0" all become "0".
    for col in ["formation", "surface", "element", "elements_names", "name"]:
        if col in out.columns:
            out[col] = out[col].map(_normalize_name_value).astype(object)

    return out


def _patch_gempy_stringarray_compat() -> None:
    """Patch GemPy name-id handling to normalize formation names.

    The important bug is not only pandas StringArray. With numeric-looking
    formation names, GemPy reads temporary CSV files and pandas may convert
    "0"/"0.0" back to numeric 0/0.0. In some GemPy versions,
    OrientationsTable._data_from_arrays directly does:

        name_id_map[name]

    instead of calling generate_ids_from_names. Therefore patching only
    generate_ids_from_names is not sufficient. This function also wraps
    SurfacePointsTable._data_from_arrays and OrientationsTable._data_from_arrays
    so names and name_id_map keys are normalized before GemPy performs lookup.
    """
    try:
        import numpy as _np
        import pandas as _pd
        import importlib

        def _stringify_one(value):
            if _pd.isna(value):
                return ""
            text = str(value).strip()
            try:
                f = float(text)
                if f.is_integer():
                    return str(int(f))
                return str(f)
            except Exception:
                return text

        def _convert_names(names):
            if isinstance(names, _pd.arrays.StringArray):
                values = list(names.astype(str))
            elif hasattr(names, "array") and isinstance(getattr(names, "array"), _pd.arrays.StringArray):
                values = list(names.astype(str))
            elif isinstance(names, _pd.Series):
                values = names.to_list()
            else:
                try:
                    values = list(names)
                except TypeError:
                    values = [names]
            return _np.asarray([_stringify_one(v) for v in values], dtype=object)

        def _convert_name_id_map(name_id_map):
            if name_id_map is None:
                return None
            try:
                return {_stringify_one(k): v for k, v in dict(name_id_map).items()}
            except Exception:
                return name_id_map

        # 1) Patch generate_ids_from_names where GemPy uses it.
        for module_name in ["gempy.core.data.surface_points", "gempy.core.data.orientations"]:
            mod = importlib.import_module(module_name)
            original = getattr(mod, "generate_ids_from_names", None)
            if original is None or getattr(original, "_node_editor_stringarray_patch", False):
                continue

            def gen_wrapper(name_id_map, names, x, _original=original):
                return _original(_convert_name_id_map(name_id_map), _convert_names(names), x)

            gen_wrapper._node_editor_stringarray_patch = True
            setattr(mod, "generate_ids_from_names", gen_wrapper)

        # 2) Patch _data_from_arrays directly. This is required for GemPy
        # orientations because some versions do name_id_map[name] directly.
        def _patch_data_from_arrays(class_obj, name_arg_index: int, map_arg_index: int):
            original = getattr(class_obj, "_data_from_arrays", None)
            if original is None or getattr(original, "_node_editor_name_patch", False):
                return

            def wrapper(cls, *args, _original=original, **kwargs):
                args = list(args)

                if len(args) > name_arg_index:
                    args[name_arg_index] = _convert_names(args[name_arg_index])
                if len(args) > map_arg_index:
                    args[map_arg_index] = _convert_name_id_map(args[map_arg_index])

                if "names" in kwargs:
                    kwargs["names"] = _convert_names(kwargs["names"])
                if "name_id_map" in kwargs:
                    kwargs["name_id_map"] = _convert_name_id_map(kwargs["name_id_map"])

                return _original(*args, **kwargs)

            wrapper._node_editor_name_patch = True
            setattr(class_obj, "_data_from_arrays", classmethod(wrapper))

        try:
            sp_mod = importlib.import_module("gempy.core.data.surface_points")
            sp_cls = getattr(sp_mod, "SurfacePointsTable", None)
            if sp_cls is not None:
                # Signature after cls: x, y, z, names, nugget, name_id_map
                _patch_data_from_arrays(sp_cls, name_arg_index=3, map_arg_index=5)
        except Exception:
            pass

        try:
            ori_mod = importlib.import_module("gempy.core.data.orientations")
            ori_cls = getattr(ori_mod, "OrientationsTable", None)
            if ori_cls is not None:
                # Signature after cls: x, y, z, G_x, G_y, G_z, names, nugget, name_id_map
                _patch_data_from_arrays(ori_cls, name_arg_index=6, map_arg_index=8)
        except Exception:
            pass

    except Exception:
        # Do not fail model creation because of a compatibility patch.
        pass


class SharedVoxelGridNode(BaseNode):
    type_name = "SharedVoxelGrid"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            origin = _parse_json(params.get("origin"), [0, 0, 0])
            dx = float(params.get("voxel_size"))
            dy_param = params.get("voxel_size_y")
            dz_param = params.get("voxel_size_z")
            dy = float(dx if dy_param in (None, "") else dy_param)
            dz = float(dx if dz_param in (None, "") else dz_param)
            grid = make_shared_grid(origin, [dx, dy, dz])
        except (TypeError, ValueError) as exc:
            raise NodeExecutionError(f"Invalid shared voxel grid: {exc}") from exc
        return {"grid": RuntimeValue("voxel_grid_spec", grid, name="shared voxel grid", preview=grid)}


def _shared_grid_input(inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rv = inputs.get("shared_grid")
    if isinstance(rv, list):
        rv = rv[0] if rv else None
    if rv is None:
        return None
    if rv.kind != "voxel_grid_spec":
        raise NodeExecutionError(f"Expected shared voxel grid, got {rv.kind}.")
    try:
        return make_shared_grid(rv.value["origin"], rv.value["spacing"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NodeExecutionError(f"Invalid shared voxel grid: {exc}") from exc


class CreateGemPyModelNode(BaseNode):
    type_name = "CreateGemPyModel"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        sp = _single(inputs, "surface_points")
        op = _single(inputs, "orientations")
        if sp.kind != "table" or op.kind != "table":
            raise NodeExecutionError("CreateGemPyModel expects table inputs for surface_points and orientations.")

        # Orientations can be provided in either GemPy-supported format:
        #   1) G_x/G_y/G_z
        #   2) azimuth/dip/polarity(optional)
        # Do not convert azimuth/dip/polarity to G_x/G_y/G_z here.  GemPy's
        # importer supports that format natively; the node editor only validates
        # that one accepted orientation format is present.
        surface_schema_report = validate_or_raise(sp.value, "surface_points", "Invalid surface_points input")
        orientation_schema_report = validate_or_raise(op.value, "orientations", "Invalid orientations input")
        orientation_schema_report["input_orientation_handling"] = "native_gempy_import_no_conversion"

        surface_formations = set(sp.value["formation"].astype(str)) if "formation" in sp.value.columns else set()
        orientation_formations = set(op.value["formation"].astype(str)) if "formation" in op.value.columns else set()
        formations_without_orientations = sorted(surface_formations - orientation_formations)

        project_name = params.get("project_name") or "GemPy Node Model"
        refinement = _as_int(params.get("refinement"), 3)
        resolution = _parse_json(params.get("resolution"), None)

        sp_df = _normalize_gempy_table_for_import(sp.value, kind="surface")
        op_df = _normalize_gempy_table_for_import(op.value, kind="orientation")

        auto_extent_enabled = _as_bool(params.get("auto_extent_from_data"), True)
        extent_user_overridden = _as_bool(params.get("extent_user_overridden"), False)
        extent_padding_percent = _as_float(params.get("extent_padding_percent"), 5.0)
        if extent_padding_percent is None:
            extent_padding_percent = 5.0

        auto_extent_report = _auto_extent_from_tables(sp_df, op_df, extent_padding_percent)
        if auto_extent_enabled and not extent_user_overridden:
            extent = auto_extent_report["padded_extent"]
            extent_source = "auto_from_surface_points_and_orientations"
        else:
            extent = _parse_json(params.get("extent"), None)
            extent_source = "manual"
            if not isinstance(extent, list) or len(extent) != 6:
                raise NodeExecutionError("extent must be a JSON list with 6 numbers: [x_min,x_max,y_min,y_max,z_min,z_max]")
            extent = [float(v) for v in extent]

        shared_grid = _shared_grid_input(inputs)
        requested_extent = list(extent)
        if shared_grid is not None:
            try:
                extent, resolution = snap_bounds(extent, shared_grid)
            except ValueError as exc:
                raise NodeExecutionError(str(exc)) from exc
            extent_source += "_aligned_to_shared_grid"

        sp_path = make_runtime_path("surface_points", ".csv")
        op_path = make_runtime_path("orientations", ".csv")
        sp_df.to_csv(sp_path, index=False)
        op_df.to_csv(op_path, index=False)

        kwargs = {
            "project_name": project_name,
            "extent": extent,
            "importer_helper": gp.data.ImporterHelper(
                path_to_orientations=str(op_path),
                path_to_surface_points=str(sp_path),
            ),
        }
        if resolution:
            kwargs["resolution"] = resolution
        else:
            kwargs["refinement"] = refinement
        geo_model = gp.create_geomodel(**kwargs)
        # Keep resolution provenance for previews and legacy JsonIO imports.
        try:
            geo_model._node_editor_resolution_was_explicit = bool(resolution)
            geo_model._node_editor_resolution = [int(v) for v in resolution] if resolution else None
        except Exception:
            pass

        # Match the original notebook behaviour when requested.  This does not
        # change the X/Y/Z coordinates of the input data; it sets the surface
        # point nugget used by GemPy during interpolation.
        apply_surface_point_nugget = _as_bool(params.get("apply_surface_point_nugget"), True)
        surface_point_nugget = _as_float(params.get("surface_point_nugget"), 0.01)
        nugget_applied = False
        if apply_surface_point_nugget and surface_point_nugget is not None:
            gp.modify_surface_points(
                geo_model=geo_model,
                nugget=float(surface_point_nugget),
            )
            nugget_applied = True

        available_elements = _extract_element_names_from_geo_model(geo_model)
        suggested_mapping = _auto_mapping_from_geo_model(geo_model)
        suggested_groups = _auto_groups_from_mapping(suggested_mapping)

        preview = {
            "project_name": project_name,
            "extent": extent,
            "requested_extent": requested_extent,
            "shared_grid": shared_grid,
            "extent_source": extent_source,
            "auto_extent_from_data": auto_extent_enabled,
            "extent_user_overridden": extent_user_overridden,
            "extent_padding_percent": extent_padding_percent,
            "auto_extent_report": auto_extent_report,
            "refinement": refinement,
            "resolution": resolution,
            "surface_points_rows": len(sp_df),
            "orientations_rows": len(op_df),
            "surface_schema": surface_schema_report,
            "orientation_schema": orientation_schema_report,
            "available_elements_from_geo_model": available_elements,
            "suggested_stack_mapping": suggested_mapping,
            "suggested_structural_groups": suggested_groups,
            "formations_without_orientations": formations_without_orientations,
            "schema_note": "Missing orientations for a formation may be acceptable if you add orientations later, but it is recorded for reproducibility.",
            "surface_point_nugget_applied": nugget_applied,
            "surface_point_nugget": surface_point_nugget if nugget_applied else None,
        }
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=project_name, preview=preview)}



def _as_name_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple, np.ndarray)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _structural_group_summary(geo_model: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for i, group in enumerate(geo_model.structural_frame.structural_groups):
            elements = []
            try:
                elements = [getattr(el, "name", str(el)) for el in group.elements]
            except Exception:
                pass
            relation = getattr(group, "structural_relation", None)
            out.append({
                "index": i,
                "name": getattr(group, "name", str(group)),
                "relation": getattr(relation, "name", str(relation)),
                "elements": elements,
            })
    except Exception:
        pass
    return out


def _apply_fault_relations_from_config(geo_model: Any, config: Any) -> Dict[str, Any]:
    """Apply geo_model.structural_frame.fault_relations from a UI-friendly config.

    Supported format:
        {
          "enabled": true,
          "relations": [
             {"from": "Fault_series", "to": "Layer_series", "active": true}
          ]
        }

    The relation matrix is built against the final current structural_group order.
    Rows are the faulting groups and columns are the affected groups, matching
    GemPy's structural_frame.fault_relations convention used in the notebook.
    """
    if not config:
        return {"enabled": False, "applied": False, "reason": "empty"}
    if isinstance(config, str):
        config = _parse_json(config, {})
    if not isinstance(config, dict):
        raise NodeExecutionError("Fault relations parameter must be an object created by the fault-relations editor.")

    enabled = _as_bool(config.get("enabled"), False)
    if not enabled:
        return {"enabled": False, "applied": False}

    groups = list(getattr(geo_model.structural_frame, "structural_groups", []))
    names = [str(getattr(g, "name", f"group_{i}")) for i, g in enumerate(groups)]
    n = len(names)
    name_to_idx = {name: i for i, name in enumerate(names)}
    matrix = np.zeros((n, n), dtype=int)
    applied = []

    # Backward-compatible: allow a raw matrix too.
    raw_matrix = config.get("matrix")
    if isinstance(raw_matrix, list) and len(raw_matrix) == n:
        arr = np.asarray(raw_matrix, dtype=int)
        if arr.shape != (n, n):
            raise NodeExecutionError(f"Fault relation matrix must be {n} x {n}, got {arr.shape}.")
        matrix = (arr != 0).astype(int)
        applied = [
            {"from": names[i], "to": names[j], "active": bool(matrix[i, j])}
            for i in range(n) for j in range(n) if matrix[i, j]
        ]
    else:
        relations = config.get("relations") or []
        if not isinstance(relations, list):
            raise NodeExecutionError("Fault relations must be a list.")
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            if not _as_bool(rel.get("active"), True):
                continue
            src = str(rel.get("from") or rel.get("source") or "").strip()
            dst = str(rel.get("to") or rel.get("target") or "").strip()
            if src not in name_to_idx or dst not in name_to_idx:
                # This can happen after the user edits mapping but has stale relation rows.
                continue
            i = name_to_idx[src]
            j = name_to_idx[dst]
            if i == j:
                continue
            matrix[i, j] = 1
            applied.append({"from": src, "to": dst, "active": True})

    try:
        geo_model.structural_frame.fault_relations = matrix
    except Exception:
        # Some GemPy builds expect bool; try that too.
        geo_model.structural_frame.fault_relations = matrix.astype(bool)

    return {
        "enabled": True,
        "applied": True,
        "group_order": names,
        "matrix": matrix.tolist(),
        "relations": applied,
    }


def _extract_element_names_from_geo_model(geo_model: Any) -> List[str]:
    names: List[str] = []
    def add(value: Any) -> None:
        if value is None:
            return
        s = str(value).strip()
        if s and s not in names and s not in {"default_formation"}:
            names.append(s)
    try:
        frame = geo_model.structural_frame
        for group in getattr(frame, "structural_groups", []) or []:
            for element in getattr(group, "elements", []) or []:
                add(getattr(element, "name", None))
    except Exception:
        pass
    return names

def _default_series_name_for_element(element_name: str) -> str:
    raw = str(element_name or "").strip()
    if not raw:
        return "Series"
    lower = raw.lower()
    if "fault" in lower or lower.startswith("f"):
        return f"{raw}_series"
    if "basement" in lower or lower in {"base", "crystallinebasement", "crystalline_basement"}:
        return "Basement_series"
    return f"{raw}_series"

def _auto_mapping_from_geo_model(geo_model: Any) -> Dict[str, List[str]]:
    elements = _extract_element_names_from_geo_model(geo_model)
    mapping: Dict[str, List[str]] = {}
    used_names: set[str] = set()
    for element in elements:
        base = _default_series_name_for_element(element)
        name = base
        n = 2
        while name in used_names:
            name = f"{base}_{n}"
            n += 1
        used_names.add(name)
        mapping[name] = [element]
    return mapping

def _auto_groups_from_mapping(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for idx, (series, elements) in enumerate(mapping.items()):
        elems = elements if isinstance(elements, list) else [elements]
        lower = f"{series} {','.join(str(e) for e in elems)}".lower()
        if "fault" in lower:
            relation = "FAULT"
        elif "basement" in lower or "base" in lower:
            relation = "BASEMENT"
        else:
            relation = "ERODE"
        groups.append({"index": idx, "name": str(series), "elements": [str(e) for e in elems], "relation": relation})
    return groups

def _is_effectively_empty_json(value: Any, empty_kind: str = "dict") -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return True
        return s in {"{}", "[]"}
    return value in ({}, [])

class ConfigureStructuralFrameNode(BaseNode):
    type_name = "ConfigureStructuralFrame"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"ConfigureStructuralFrame expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        if params.get("anisotropy") == "NONE":
            geo_model.input_transform.apply_anisotropy(gp.data.GlobalAnisotropy.NONE)

        auto_mapping_used = False
        auto_groups_used = False

        # Generic default behavior:
        # If the mapping/group editors are empty, infer them from the connected
        # GeoModel instead of using a project-specific fixed default.
        mapping_user_overridden = _as_bool(params.get("mapping_user_overridden"), False)
        groups_user_overridden = _as_bool(params.get("groups_user_overridden"), False)

        mapping_auto_initialized = _as_bool(params.get("mapping_auto_initialized"), False)

        if (
            _as_bool(params.get("auto_mapping_from_geo_model"), True)
            and not mapping_auto_initialized
            and not mapping_user_overridden
            and _is_effectively_empty_json(params.get("mapping_json"), "dict")
        ):
            mapping = _auto_mapping_from_geo_model(geo_model)
            auto_mapping_used = bool(mapping)
        else:
            mapping = _parse_json(params.get("mapping_json"), {})

        if (
            _as_bool(params.get("auto_groups_from_mapping"), True)
            and not groups_user_overridden
            and _is_effectively_empty_json(params.get("groups_json"), "list")
        ):
            groups = _auto_groups_from_mapping(mapping if isinstance(mapping, dict) else {})
            auto_groups_used = bool(groups)
        else:
            groups = _parse_json(params.get("groups_json"), [])

        fault_relations_cfg = _parse_json(params.get("fault_relations_json"), {})
        relation_type = gp.data.StackRelationType

        def _relation_name(group: Dict[str, Any], default: str = "ERODE") -> str:
            rel_name = str(group.get("relation", default)).upper()
            if not hasattr(relation_type, rel_name):
                raise NodeExecutionError(f"Unknown StackRelationType: {rel_name}")
            return rel_name

        def _as_element_list(value: Any) -> List[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if str(v).strip()]
            return [str(value).strip()]

        applied = []

        # Important semantics:
        #   Stack mapping defines the series/order and which formations/elements belong to each series.
        #   Structural groups only assign the StackRelationType for those mapped series.
        #
        # Older versions first called gp.map_stack_to_surfaces(...) and then called
        # gp.add_structural_group(...) for the same names/elements, which duplicated
        # formations in the structural frame.  Here we either:
        #   1) use mapping + set relations on the mapped groups; or
        #   2) if no mapping is given, build groups manually from the group editor.
        relation_by_name = {}
        for group in groups if isinstance(groups, list) else []:
            name = str(group.get("name", "")).strip()
            if name:
                relation_by_name[name] = _relation_name(group)

        if mapping:
            if not isinstance(mapping, dict):
                raise NodeExecutionError("Stack mapping must be a series-to-elements object.")

            # Let GemPy create/order the structural groups once.
            gp.map_stack_to_surfaces(gempy_model=geo_model, mapping_object=mapping)

            # Then only set the relation on already-created groups. Do not add the same
            # groups again. This avoids duplicate Elements in GemPy plots.
            mapped_names = set(str(name) for name in mapping.keys())
            for i, group in enumerate(geo_model.structural_frame.structural_groups):
                name = str(getattr(group, "name", ""))
                if name in mapped_names:
                    rel_name = relation_by_name.get(name, "ERODE")
                    group.structural_relation = getattr(relation_type, rel_name)
                    applied.append({
                        "index": i,
                        "name": name,
                        "elements": _as_element_list(mapping.get(name)),
                        "relation": rel_name,
                        "mode": "mapped_set_relation",
                    })

            # Optional fallback: groups that are not in the mapping but still have
            # elements are added manually. This keeps fault-only or extra groups usable.
            extra_index = len(applied)
            for group in groups if isinstance(groups, list) else []:
                name = str(group.get("name", "")).strip()
                if not name or name in mapped_names:
                    continue
                element_names = _as_element_list(group.get("elements", []))
                if not element_names:
                    continue
                elements = [geo_model.structural_frame.get_element_by_name(e) for e in element_names]
                rel_name = _relation_name(group)
                gp.add_structural_group(
                    model=geo_model,
                    group_index=extra_index,
                    structural_group_name=name,
                    elements=elements,
                    structural_relation=getattr(relation_type, rel_name),
                )
                applied.append({"index": extra_index, "name": name, "elements": element_names, "relation": rel_name, "mode": "extra_group"})
                extra_index += 1
        else:
            # Backward-compatible manual mode: no stack mapping was supplied, so
            # the Structural groups editor is responsible for order, elements, and relation.
            for idx, group in enumerate(groups if isinstance(groups, list) else []):
                name = str(group.get("name", "")).strip()
                if not name:
                    continue
                element_names = _as_element_list(group.get("elements", []))
                if not element_names:
                    continue
                elements = [geo_model.structural_frame.get_element_by_name(e) for e in element_names]
                rel_name = _relation_name(group)
                gp.add_structural_group(
                    model=geo_model,
                    group_index=int(group.get("index", idx)),
                    structural_group_name=name,
                    elements=elements,
                    structural_relation=getattr(relation_type, rel_name),
                )
                applied.append({"index": int(group.get("index", idx)), "name": name, "elements": element_names, "relation": rel_name, "mode": "manual_group"})

        if _as_bool(params.get("remove_default_formation"), True):
            try:
                gp.remove_structural_group_by_name(model=geo_model, group_name="default_formation")
            except Exception:
                pass

        fault_relations_report = _apply_fault_relations_from_config(geo_model, fault_relations_cfg)

        preview = {
            "mapping_applied": bool(mapping),
            "auto_mapping_from_geo_model": bool(locals().get("auto_mapping_used", False)),
            "mapping_user_overridden": bool(locals().get("mapping_user_overridden", False)),
            "mapping_auto_initialized": bool(locals().get("mapping_auto_initialized", False)),
            "auto_groups_from_mapping": bool(locals().get("auto_groups_used", False)),
            "groups_user_overridden": bool(locals().get("groups_user_overridden", False)),
            "auto_mapping": mapping if locals().get("auto_mapping_used", False) else None,
            "auto_groups": groups if locals().get("auto_groups_used", False) else None,
            "available_elements_from_geo_model": _extract_element_names_from_geo_model(geo_model),
            "groups_applied": applied,
            "fault_relations": fault_relations_report,
            "structural_group_summary": _structural_group_summary(geo_model),
            "note": "When Stack mapping is empty, this node can infer a generic mapping from the connected GeoModel. Mapping defines order/elements; Structural groups set relations. Fault relations are applied after the final group order is established.",
        }
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview)}



def _parse_number_list(value: Any) -> List[float]:
    """Parse comma/space/newline separated numbers or a JSON list."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, np.ndarray)):
        return [float(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [float(v) for v in parsed]
    except Exception:
        pass
    text = text.replace(";", ",").replace("\n", ",").replace("\t", ",")
    out = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out




def _call_gempy_candidates(candidates: List[Any], error_prefix: str) -> Any:
    """Try several GemPy API call variants for version compatibility."""
    errors: List[str] = []
    for label, func in candidates:
        try:
            return func()
        except TypeError as exc:
            errors.append(f"{label}: {exc}")
        except AttributeError as exc:
            errors.append(f"{label}: {exc}")
        except Exception as exc:
            # Keep trying for known API mismatch cases; otherwise also record
            # the message and continue to the next candidate.
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    raise NodeExecutionError(error_prefix + " Tried variants: " + " | ".join(errors[-8:]))


def _geo_model_extent_values(geo_model: Any) -> List[float]:
    candidates = []
    try:
        candidates.append(getattr(geo_model.grid.regular_grid, "extent", None))
    except Exception:
        pass
    try:
        candidates.append(getattr(geo_model.grid, "extent", None))
    except Exception:
        pass
    try:
        candidates.append(getattr(geo_model, "extent", None))
    except Exception:
        pass

    for value in candidates:
        if value is None:
            continue
        try:
            arr = np.asarray(value, dtype=float).ravel()
            if arr.size == 6 and np.all(np.isfinite(arr)):
                return [float(v) for v in arr.tolist()]
        except Exception:
            pass
    raise NodeExecutionError("Could not infer model extent from geo_model.grid. Set an explicit extent in Create GemPy Model first.")


def _parse_xyz_points(value: Any) -> np.ndarray:
    data = _parse_json(value, [])
    if not isinstance(data, list):
        raise NodeExecutionError("Point JSON must be a list of [x,y,z] lists or objects with x/y/z.")
    points: List[List[float]] = []
    for row in data:
        if isinstance(row, dict):
            x = row.get("x", row.get("X"))
            y = row.get("y", row.get("Y"))
            z = row.get("z", row.get("Z"))
            points.append([float(x), float(y), float(z)])
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            points.append([float(row[0]), float(row[1]), float(row[2])])
    arr = np.asarray(points, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] == 0:
        raise NodeExecutionError("No valid XYZ points were provided.")
    return arr


def _xyz_from_table(df: pd.DataFrame) -> np.ndarray:
    cols = _find_xyz_columns(df)
    if not cols:
        raise NodeExecutionError("Input table needs X, Y, Z columns.")
    out = df[[cols["X"], cols["Y"], cols["Z"]]].copy()
    for c in out.columns:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna()
    arr = out.to_numpy(dtype=float)
    if arr.shape[0] == 0:
        raise NodeExecutionError("Input table contains no valid XYZ rows.")
    return arr


def _get_topography_xyz(geo_model: Any) -> Optional[np.ndarray]:
    topo = None
    for expr in [
        lambda: geo_model.grid.topography,
        lambda: geo_model.grid.topography.values,
        lambda: geo_model.grid.topography.xyz,
        lambda: geo_model.grid.topography.xyz_vertices,
        lambda: geo_model.grid.topography.vertices,
        lambda: geo_model.grid.values_topography,
    ]:
        try:
            topo = expr()
            if topo is not None:
                break
        except Exception:
            pass
    if topo is None:
        return None
    try:
        arr = np.asarray(topo, dtype=float)
    except Exception:
        return None
    arr = arr.reshape((-1, arr.shape[-1])) if arr.ndim > 2 else arr
    if arr.ndim == 2 and arr.shape[1] >= 3 and arr.shape[0] > 0:
        return arr[:, :3]
    return None


def _topography_preview(geo_model: Any, label: str = "topography") -> Dict[str, Any]:
    xyz = _get_topography_xyz(geo_model)
    preview: Dict[str, Any] = {"preview_type": "topography", "label": label, "has_topography_points": xyz is not None}
    if xyz is None:
        preview["message"] = "No topography XYZ array could be extracted from geo_model.grid.topography."
        return preview

    preview.update({
        "n_points": int(xyz.shape[0]),
        "bounds": {
            "x": [float(np.nanmin(xyz[:, 0])), float(np.nanmax(xyz[:, 0]))],
            "y": [float(np.nanmin(xyz[:, 1])), float(np.nanmax(xyz[:, 1]))],
            "z": [float(np.nanmin(xyz[:, 2])), float(np.nanmax(xyz[:, 2]))],
        },
    })

    try:
        import pyvista as pv
        points = pv.PolyData(xyz)
        if xyz.shape[0] >= 3:
            try:
                surf = points.delaunay_2d()
            except Exception:
                surf = points
        else:
            surf = points
        record, _poly = _register_web_surface_preview(surf, f"{label}_preview")
        if record is not None:
            preview["web_mesh_url"] = f"/api/raw/{record['file_id']}"
            preview["web_mesh_file_id"] = record["file_id"]
            preview["web_mesh_format"] = "vtp"
            preview["web_viewer_available"] = True
            preview["web_viewer_label"] = "Topography preview"
            preview["inline_display"] = "web_3d_viewer"
    except Exception as exc:
        preview["mesh_preview_warning"] = str(exc)

    return preview


def _indices_from_param(value: Any) -> List[int]:
    if value is None or str(value).strip() == "":
        return []
    if isinstance(value, (list, tuple, np.ndarray)):
        return [int(v) for v in value]
    text = str(value).replace(";", ",").replace("\n", ",")
    return [int(v.strip()) for v in text.split(",") if v.strip()]


def _orientation_vectors_from_df(df: pd.DataFrame) -> tuple[np.ndarray, str]:
    cols = {str(c).strip().lower(): str(c) for c in df.columns}
    gx = cols.get("g_x") or cols.get("gx")
    gy = cols.get("g_y") or cols.get("gy")
    gz = cols.get("g_z") or cols.get("gz")
    if gx and gy and gz:
        vec = df[[gx, gy, gz]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        return vec, "gradient_vector"

    # Fallback: convert azimuth/dip/polarity to a pole vector approximation.
    az_col = cols.get("azimuth")
    dip_col = cols.get("dip")
    pol_col = cols.get("polarity")
    if az_col and dip_col:
        az = np.deg2rad(pd.to_numeric(df[az_col], errors="coerce").to_numpy(dtype=float))
        dip = np.deg2rad(pd.to_numeric(df[dip_col], errors="coerce").to_numpy(dtype=float))
        polarity = pd.to_numeric(df[pol_col], errors="coerce").fillna(1.0).to_numpy(dtype=float) if pol_col else np.ones_like(az)
        # Normal/pole-vector convention used here is a pragmatic conversion for
        # API compatibility. Users who need exact geological convention should
        # provide G_x/G_y/G_z directly.
        gxv = np.sin(dip) * np.sin(az) * polarity
        gyv = np.sin(dip) * np.cos(az) * polarity
        gzv = np.cos(dip) * polarity
        return np.column_stack([gxv, gyv, gzv]), "azimuth_dip_converted"

    raise NodeExecutionError("Orientation input needs G_x/G_y/G_z (or Gx/Gy/Gz), or azimuth/dip(/polarity).")


def _normalize_formation_series(series: pd.Series) -> pd.Series:
    def normalize(v: Any) -> str:
        if pd.isna(v):
            return ""
        text = str(v).strip()
        try:
            f = float(text)
            return str(int(f)) if f.is_integer() else str(f)
        except Exception:
            return text
    return series.map(normalize).astype(object)



def _geo_editor_prepare_surface_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    cols = _find_xyz_columns(out)
    if not cols:
        raise NodeExecutionError("Surface-points table needs X, Y, Z columns.")
    out = out.rename(columns={cols["X"]: "X", cols["Y"]: "Y", cols["Z"]: "Z"})
    formation_col = None
    for c in ["formation", "surface", "element", "elements_names", "name"]:
        if c in out.columns:
            formation_col = c
            break
    if formation_col is None:
        raise NodeExecutionError("Surface-points table needs a formation/surface/element column.")
    if formation_col != "formation":
        out = out.rename(columns={formation_col: "formation"})
    for c in ["X", "Y", "Z"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["formation"] = _normalize_formation_series(out["formation"])
    out = out.dropna(subset=["X", "Y", "Z", "formation"]).reset_index(drop=True)
    if "_editor_uid" not in out.columns:
        out["_editor_uid"] = [f"s_{i}" for i in range(len(out))]
    if "_editor_kind" not in out.columns:
        out["_editor_kind"] = "surface"
    return out


def _geo_editor_prepare_orientation_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame(columns=["X", "Y", "Z", "formation", "G_x", "G_y", "G_z", "_editor_uid", "_editor_kind"])
    out = df.copy()
    cols = _find_xyz_columns(out)
    if not cols:
        raise NodeExecutionError("Orientations table needs X, Y, Z columns.")
    out = out.rename(columns={cols["X"]: "X", cols["Y"]: "Y", cols["Z"]: "Z"})
    formation_col = None
    for c in ["formation", "surface", "element", "elements_names", "name"]:
        if c in out.columns:
            formation_col = c
            break
    if formation_col is None:
        raise NodeExecutionError("Orientations table needs a formation/surface/element column.")
    if formation_col != "formation":
        out = out.rename(columns={formation_col: "formation"})
    for c in ["X", "Y", "Z"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["formation"] = _normalize_formation_series(out["formation"])
    vectors, _source = _orientation_vectors_from_df(out)
    out["G_x"] = vectors[:, 0]
    out["G_y"] = vectors[:, 1]
    out["G_z"] = vectors[:, 2]
    out = out.dropna(subset=["X", "Y", "Z", "formation", "G_x", "G_y", "G_z"]).reset_index(drop=True)
    if "_editor_uid" not in out.columns:
        out["_editor_uid"] = [f"o_{i}" for i in range(len(out))]
    if "_editor_kind" not in out.columns:
        out["_editor_kind"] = "orientation"
    return out


def _geo_editor_action_uid(action: Dict[str, Any]) -> str:
    return str(action.get("uid") or action.get("_editor_uid") or "").strip()


def _geo_editor_apply_operations(surface_df: pd.DataFrame, orientation_df: pd.DataFrame, operations: List[Dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, List[Dict[str, Any]]]:
    s = surface_df.copy()
    o = orientation_df.copy()
    applied: List[Dict[str, Any]] = []

    for i, action in enumerate(operations or []):
        if not isinstance(action, dict):
            continue
        op = str(action.get("op") or "").strip().lower()
        kind = str(action.get("kind") or action.get("type") or "surface").strip().lower()
        uid = _geo_editor_action_uid(action)

        try:
            if op == "delete":
                if kind.startswith("ori"):
                    before = len(o)
                    o = o[o["_editor_uid"].astype(str) != uid].reset_index(drop=True)
                    applied.append({"op": "delete", "kind": "orientation", "uid": uid, "removed": int(before - len(o))})
                else:
                    before = len(s)
                    s = s[s["_editor_uid"].astype(str) != uid].reset_index(drop=True)
                    applied.append({"op": "delete", "kind": "surface", "uid": uid, "removed": int(before - len(s))})

            elif op == "modify":
                target = o if kind.startswith("ori") else s
                mask = target["_editor_uid"].astype(str) == uid
                if not mask.any():
                    applied.append({"op": "modify", "kind": kind, "uid": uid, "warning": "uid not found"})
                    continue
                idxs = target.index[mask].tolist()
                fields = {}
                for col in ["X", "Y", "Z"]:
                    val = action.get(col, action.get(col.lower()))
                    if val not in (None, ""):
                        fields[col] = float(val)
                element = action.get("formation", action.get("element"))
                if element not in (None, ""):
                    fields["formation"] = _normalize_formation_series(pd.Series([element])).iloc[0]
                if kind.startswith("ori"):
                    for col in ["G_x", "G_y", "G_z"]:
                        val = action.get(col, action.get(col.lower().replace("_", "")))
                        if val not in (None, ""):
                            fields[col] = float(val)
                for col, val in fields.items():
                    target.loc[idxs, col] = val
                if kind.startswith("ori"):
                    o = target
                    applied.append({"op": "modify", "kind": "orientation", "uid": uid, "fields": list(fields.keys())})
                else:
                    s = target
                    applied.append({"op": "modify", "kind": "surface", "uid": uid, "fields": list(fields.keys())})

            elif op in {"add", "add_surface", "add_orientation"}:
                is_orientation = kind.startswith("ori") or op == "add_orientation"
                element = action.get("formation", action.get("element"))
                if element in (None, ""):
                    raise ValueError("element/formation is required for add")
                row = {
                    "X": float(action.get("X", action.get("x"))),
                    "Y": float(action.get("Y", action.get("y"))),
                    "Z": float(action.get("Z", action.get("z"))),
                    "formation": _normalize_formation_series(pd.Series([element])).iloc[0],
                    "_editor_uid": str(action.get("new_uid") or action.get("uid") or f"{'o' if is_orientation else 's'}_new_{i}"),
                    "_editor_kind": "orientation" if is_orientation else "surface",
                }
                if is_orientation:
                    row["G_x"] = float(action.get("G_x", action.get("gx", 0.0)))
                    row["G_y"] = float(action.get("G_y", action.get("gy", 0.0)))
                    row["G_z"] = float(action.get("G_z", action.get("gz", 1.0)))
                    o = pd.concat([o, pd.DataFrame([row])], ignore_index=True)
                    applied.append({"op": "add", "kind": "orientation", "uid": row["_editor_uid"], "element": row["formation"]})
                else:
                    s = pd.concat([s, pd.DataFrame([row])], ignore_index=True)
                    applied.append({"op": "add", "kind": "surface", "uid": row["_editor_uid"], "element": row["formation"]})
        except Exception as exc:
            applied.append({"op": op, "kind": kind, "uid": uid, "error": str(exc)})

    return s.reset_index(drop=True), o.reset_index(drop=True), applied


def _geo_editor_preview(surface_df: pd.DataFrame, orientation_df: pd.DataFrame, name: str = "geological_points_editor") -> Dict[str, Any]:
    preview: Dict[str, Any] = {
        "preview_type": "interactive_geological_point_editor",
        "web_viewer_label": "Interactive geological points",
        "editor_picker_enabled": True,
        "surface_count": int(len(surface_df)),
        "orientation_count": int(len(orientation_df)),
        "formations": sorted(set(surface_df.get("formation", pd.Series(dtype=str)).astype(str).tolist() + orientation_df.get("formation", pd.Series(dtype=str)).astype(str).tolist())),
    }

    editor_points: List[Dict[str, Any]] = []
    points: List[List[float]] = []
    point_kind: List[int] = []
    point_row: List[int] = []
    formation_ids: List[int] = []
    formations = preview["formations"]
    formation_id_map = {name: i for i, name in enumerate(formations)}

    for row_idx, row in surface_df.reset_index(drop=True).iterrows():
        item = {
            "uid": str(row.get("_editor_uid", f"s_{row_idx}")),
            "kind": "surface",
            "row_index": int(row_idx),
            "x": float(row["X"]),
            "y": float(row["Y"]),
            "z": float(row["Z"]),
            "element": str(row["formation"]),
        }
        editor_points.append(item)
        points.append([item["x"], item["y"], item["z"]])
        point_kind.append(0)
        point_row.append(len(editor_points) - 1)
        formation_ids.append(int(formation_id_map.get(item["element"], -1)))

    for row_idx, row in orientation_df.reset_index(drop=True).iterrows():
        item = {
            "uid": str(row.get("_editor_uid", f"o_{row_idx}")),
            "kind": "orientation",
            "row_index": int(row_idx),
            "x": float(row["X"]),
            "y": float(row["Y"]),
            "z": float(row["Z"]),
            "element": str(row["formation"]),
            "gx": float(row.get("G_x", 0.0)),
            "gy": float(row.get("G_y", 0.0)),
            "gz": float(row.get("G_z", 1.0)),
        }
        editor_points.append(item)
        points.append([item["x"], item["y"], item["z"]])
        point_kind.append(1)
        point_row.append(len(editor_points) - 1)
        formation_ids.append(int(formation_id_map.get(item["element"], -1)))

    preview["editor_points"] = editor_points
    preview["formation_id_map"] = formation_id_map

    try:
        import pyvista as pv
        if points:
            xyz = np.asarray(points, dtype=float)
            cloud = pv.PolyData(xyz)
            cloud.verts = np.hstack([
                np.ones((xyz.shape[0], 1), dtype=np.int64),
                np.arange(xyz.shape[0], dtype=np.int64).reshape(-1, 1),
            ])
            cloud.point_data["editor_point_id"] = np.asarray(point_row, dtype=np.int32)
            cloud.point_data["editor_kind"] = np.asarray(point_kind, dtype=np.int32)
            cloud.point_data["formation_id"] = np.asarray(formation_ids, dtype=np.int32)
            record, _poly = _register_web_surface_preview(cloud, name)
            if record is not None:
                preview["web_mesh_url"] = f"/api/raw/{record['file_id']}"
                preview["web_mesh_file_id"] = record["file_id"]
                preview["web_mesh_format"] = "vtp"
                preview["web_scalar"] = "formation_id"
                preview["web_show_edges"] = False
                preview["web_opacity"] = 1
                preview["web_viewer_available"] = True
    except Exception as exc:
        preview["web_viewer_warning"] = str(exc)

    return preview



def _table_like_to_dataframe(obj: Any) -> Optional[pd.DataFrame]:
    """Best-effort conversion of GemPy table-like objects to a pandas DataFrame."""
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    for attr in ["df", "data", "_data"]:
        try:
            value = getattr(obj, attr)
            if isinstance(value, pd.DataFrame):
                return value.copy()
        except Exception:
            pass
    try:
        if hasattr(obj, "to_pandas"):
            out = obj.to_pandas()
            if isinstance(out, pd.DataFrame):
                return out.copy()
    except Exception:
        pass
    return None


def _extract_xyz_from_gempy_points(obj: Any) -> Optional[np.ndarray]:
    if obj is None:
        return None
    for attr in ["xyz", "xyz_view", "xyz_coords", "coordinates"]:
        try:
            value = getattr(obj, attr)
            if value is not None:
                arr = np.asarray(value, dtype=float)
                if arr.ndim == 2 and arr.shape[1] >= 3:
                    return arr[:, :3]
        except Exception:
            pass
    df = _table_like_to_dataframe(obj)
    if df is not None:
        try:
            cols = _find_xyz_columns(df)
            if cols:
                return df[[cols["X"], cols["Y"], cols["Z"]]].to_numpy(dtype=float)
        except Exception:
            pass
    return None


def _extract_grads_from_gempy_orientations(obj: Any) -> Optional[np.ndarray]:
    if obj is None:
        return None
    for attr in ["grads", "gradients", "pole_vector", "pole_vectors"]:
        try:
            value = getattr(obj, attr)
            if value is not None:
                arr = np.asarray(value, dtype=float)
                if arr.ndim == 2 and arr.shape[1] >= 3:
                    return arr[:, :3]
        except Exception:
            pass
    df = _table_like_to_dataframe(obj)
    if df is not None:
        try:
            vectors, _src = _orientation_vectors_from_df(df)
            return vectors
        except Exception:
            pass
    return None


def _extract_gempy_model_editor_tables(geo_model: Any) -> tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """Extract editable rows from an existing GeoModel.

    Important: GemPy's modify/delete APIs expect the *real internal table index*.
    v64 used synthetic positional indices from structural elements, which could
    display points correctly but edit the wrong row or no row at all. v65 first
    tries model-level GemPy tables such as ``geo_model.surface_points_copy`` and
    preserves their actual DataFrame index as ``_gempy_index``. Structural-element
    extraction is now only a fallback.
    """
    report: Dict[str, Any] = {
        "source": "",
        "index_policy": "preserve GemPy model-level table index when available",
        "elements": [],
    }

    def element_id_name_map() -> Dict[Any, str]:
        out: Dict[Any, str] = {}
        try:
            elements = list(getattr(geo_model.structural_frame, "structural_elements", []) or [])
        except Exception:
            elements = []
        for pos, el in enumerate(elements):
            name = str(getattr(el, "name", "") or f"element_{pos}")
            out[pos] = name
            out[pos + 1] = name
            for attr in ["id", "id_name", "element_id", "surface_id"]:
                try:
                    value = getattr(el, attr)
                    out[value] = name
                    try:
                        out[int(value)] = name
                    except Exception:
                        pass
                except Exception:
                    pass
        return out

    id_to_name = element_id_name_map()

    def formation_from_row(row: pd.Series, fallback_name: str = "") -> str:
        for c in ["formation", "surface", "element", "elements_names", "name", "surface_name", "element_name"]:
            if c in row.index and row[c] not in (None, "") and not pd.isna(row[c]):
                return str(_normalize_formation_series(pd.Series([row[c]])).iloc[0])
        for c in ["id", "ID", "surface_id", "element_id", "id_name"]:
            if c in row.index and row[c] not in (None, "") and not pd.isna(row[c]):
                value = row[c]
                if value in id_to_name:
                    return id_to_name[value]
                try:
                    ivalue = int(value)
                    if ivalue in id_to_name:
                        return id_to_name[ivalue]
                    return f"element_{ivalue}"
                except Exception:
                    return str(value)
        return str(fallback_name or "")

    def parse_surface_df(df: pd.DataFrame, source: str, fallback_name: str = "") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if df is None or df.empty:
            return rows
        cols = _find_xyz_columns(df)
        if not cols:
            return rows
        for real_idx, row in df.iterrows():
            try:
                x = float(row[cols["X"]])
                y = float(row[cols["Y"]])
                z = float(row[cols["Z"]])
                if not (np.isfinite(x) and np.isfinite(y) and np.isfinite(z)):
                    continue
                formation = formation_from_row(row, fallback_name=fallback_name)
                rows.append({
                    "X": x,
                    "Y": y,
                    "Z": z,
                    "formation": formation,
                    "_editor_uid": f"s_{real_idx}",
                    "_editor_kind": "surface",
                    "_gempy_index": int(real_idx) if str(real_idx).lstrip("-").isdigit() else real_idx,
                    "_local_index": int(len(rows)),
                    "_source": source,
                })
            except Exception:
                continue
        return rows

    def parse_orientation_df(df: pd.DataFrame, source: str, fallback_name: str = "") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if df is None or df.empty:
            return rows
        cols = _find_xyz_columns(df)
        if not cols:
            return rows
        try:
            vectors, _vector_source = _orientation_vectors_from_df(df)
        except Exception:
            vectors = np.zeros((len(df), 3), dtype=float)
            vectors[:, 2] = 1.0
        for j, (real_idx, row) in enumerate(df.iterrows()):
            try:
                x = float(row[cols["X"]])
                y = float(row[cols["Y"]])
                z = float(row[cols["Z"]])
                if not (np.isfinite(x) and np.isfinite(y) and np.isfinite(z)):
                    continue
                grad = vectors[j] if j < vectors.shape[0] else np.array([0.0, 0.0, 1.0])
                formation = formation_from_row(row, fallback_name=fallback_name)
                rows.append({
                    "X": x,
                    "Y": y,
                    "Z": z,
                    "formation": formation,
                    "G_x": float(grad[0]),
                    "G_y": float(grad[1]),
                    "G_z": float(grad[2]),
                    "_editor_uid": f"o_{real_idx}",
                    "_editor_kind": "orientation",
                    "_gempy_index": int(real_idx) if str(real_idx).lstrip("-").isdigit() else real_idx,
                    "_local_index": int(len(rows)),
                    "_source": source,
                })
            except Exception:
                continue
        return rows

    surface_rows: List[Dict[str, Any]] = []
    orientation_rows: List[Dict[str, Any]] = []

    # 1) Prefer model-level tables because they normally preserve the indices
    # used by gp.modify_* and gp.delete_*.
    for attr_chain in [
        ("surface_points_copy",),
        ("structural_frame", "surface_points"),
        ("input_data_descriptor", "surface_points"),
    ]:
        try:
            obj = geo_model
            for attr in attr_chain:
                obj = getattr(obj, attr)
            df = _table_like_to_dataframe(obj)
            rows = parse_surface_df(df, ".".join(attr_chain))
            if rows:
                surface_rows = rows
                report["surface_source"] = ".".join(attr_chain)
                break
        except Exception as exc:
            report.setdefault("surface_source_errors", []).append(f"{'.'.join(attr_chain)}: {exc}")

    for attr_chain in [
        ("orientations_copy",),
        ("structural_frame", "orientations"),
        ("input_data_descriptor", "orientations"),
    ]:
        try:
            obj = geo_model
            for attr in attr_chain:
                obj = getattr(obj, attr)
            df = _table_like_to_dataframe(obj)
            rows = parse_orientation_df(df, ".".join(attr_chain))
            if rows:
                orientation_rows = rows
                report["orientation_source"] = ".".join(attr_chain)
                break
        except Exception as exc:
            report.setdefault("orientation_source_errors", []).append(f"{'.'.join(attr_chain)}: {exc}")

    # 2) Fallback to structural elements. Use the element-local DataFrame index
    # when available; otherwise use a clear synthetic index and report this.
    if not surface_rows or not orientation_rows:
        try:
            elements = list(getattr(geo_model.structural_frame, "structural_elements", []) or [])
        except Exception:
            elements = []

        s_global = 0
        o_global = 0

        for element in elements:
            element_name = str(getattr(element, "name", "") or "")
            e_report = {"name": element_name, "surface_points": 0, "orientations": 0}

            if not surface_rows:
                sp = getattr(element, "surface_points", None)
                sp_df = _table_like_to_dataframe(sp)
                rows = parse_surface_df(sp_df, "structural_element.surface_points", fallback_name=element_name) if sp_df is not None else []
                if not rows:
                    sp_xyz = _extract_xyz_from_gempy_points(sp)
                    if sp_xyz is not None:
                        for local_idx, xyz in enumerate(sp_xyz):
                            rows.append({
                                "X": float(xyz[0]), "Y": float(xyz[1]), "Z": float(xyz[2]),
                                "formation": element_name,
                                "_editor_uid": f"s_{s_global}",
                                "_editor_kind": "surface",
                                "_gempy_index": int(s_global),
                                "_local_index": int(local_idx),
                                "_source": "structural_element.surface_points.xyz_synthetic_index",
                            })
                            s_global += 1
                surface_rows.extend(rows)
                e_report["surface_points"] = int(len(rows))

            if not orientation_rows:
                ori = getattr(element, "orientations", None)
                ori_df = _table_like_to_dataframe(ori)
                rows = parse_orientation_df(ori_df, "structural_element.orientations", fallback_name=element_name) if ori_df is not None else []
                if not rows:
                    ori_xyz = _extract_xyz_from_gempy_points(ori)
                    ori_grads = _extract_grads_from_gempy_orientations(ori)
                    if ori_xyz is not None:
                        if ori_grads is None or ori_grads.shape[0] != ori_xyz.shape[0]:
                            ori_grads = np.zeros_like(ori_xyz)
                            ori_grads[:, 2] = 1.0
                        for local_idx, (xyz, grad) in enumerate(zip(ori_xyz, ori_grads)):
                            rows.append({
                                "X": float(xyz[0]), "Y": float(xyz[1]), "Z": float(xyz[2]),
                                "formation": element_name,
                                "G_x": float(grad[0]), "G_y": float(grad[1]), "G_z": float(grad[2]),
                                "_editor_uid": f"o_{o_global}",
                                "_editor_kind": "orientation",
                                "_gempy_index": int(o_global),
                                "_local_index": int(local_idx),
                                "_source": "structural_element.orientations.xyz_synthetic_index",
                            })
                            o_global += 1
                orientation_rows.extend(rows)
                e_report["orientations"] = int(len(rows))

            report["elements"].append(e_report)

    surface_df = pd.DataFrame(surface_rows)
    if surface_df.empty:
        surface_df = pd.DataFrame(columns=["X", "Y", "Z", "formation", "_editor_uid", "_editor_kind", "_gempy_index", "_local_index", "_source"])
    orientation_df = pd.DataFrame(orientation_rows)
    if orientation_df.empty:
        orientation_df = pd.DataFrame(columns=["X", "Y", "Z", "formation", "G_x", "G_y", "G_z", "_editor_uid", "_editor_kind", "_gempy_index", "_local_index", "_source"])

    report["surface_count"] = int(len(surface_df))
    report["orientation_count"] = int(len(orientation_df))
    report["source"] = f"surface={report.get('surface_source', 'structural_elements_fallback')}; orientation={report.get('orientation_source', 'structural_elements_fallback')}"
    return surface_df, orientation_df, report


def _gempy_api_index_from_uid(geo_model: Any, kind: str, uid: str) -> Optional[int]:
    surface_df, orientation_df, _ = _extract_gempy_model_editor_tables(geo_model)
    df = orientation_df if kind.startswith("ori") else surface_df
    if df.empty or "_editor_uid" not in df.columns:
        return None
    mask = df["_editor_uid"].astype(str) == str(uid)
    if not mask.any():
        return None
    return int(df.loc[mask, "_gempy_index"].iloc[0])


def _gempy_element_names(geo_model: Any) -> List[str]:
    try:
        return [str(getattr(e, "name", "")) for e in list(getattr(geo_model.structural_frame, "structural_elements", []) or []) if str(getattr(e, "name", ""))]
    except Exception:
        return []


def _try_create_missing_gempy_element(gp: Any, geo_model: Any, element_name: str, relation: str = "ERODE") -> Dict[str, Any]:
    """Create a missing GemPy element if the installed GemPy API supports it.

    Different GemPy versions expose this differently. This helper deliberately
    tries several official-data-object paths and reports a clear fallback if the
    current version cannot create an empty element.
    """
    existing = set(_gempy_element_names(geo_model))
    if element_name in existing:
        return {"created": False, "reason": "already_exists"}

    errors: List[str] = []
    try:
        from gempy_engine.core.data.stack_relation_type import StackRelationType
    except Exception:
        StackRelationType = None

    try:
        relation_value = getattr(StackRelationType, str(relation).upper()) if StackRelationType is not None else None
    except Exception:
        relation_value = None

    element_candidates: List[Any] = []

    # Try public GemPy data class constructors first.
    try:
        element_candidates.append(gp.data.StructuralElement(name=element_name))
    except Exception as exc:
        errors.append(f"gp.data.StructuralElement(name): {exc}")

    try:
        from gempy.core.data import StructuralElement
        element_candidates.append(StructuralElement(name=element_name))
    except Exception as exc:
        errors.append(f"gempy.core.data.StructuralElement(name): {exc}")

    group_index = len(_gempy_element_names(geo_model)) + 1
    for element in element_candidates:
        if element is None:
            continue
        try:
            gp.add_structural_group(
                model=geo_model,
                group_index=group_index,
                structural_group_name=f"{element_name}_series",
                elements=[element],
                structural_relation=relation_value,
            )
            return {"created": True, "method": "gp.add_structural_group", "group_name": f"{element_name}_series"}
        except Exception as exc:
            errors.append(f"gp.add_structural_group: {exc}")

    return {
        "created": False,
        "reason": "not_supported_by_current_gempy_api",
        "errors": errors[-5:],
    }


def _gempy_api_add_surface_point(gp: Any, geo_model: Any, action: Dict[str, Any], auto_create_missing_element: bool, relation: str) -> Dict[str, Any]:
    element = str(action.get("element") or action.get("formation") or "").strip()
    if not element:
        raise NodeExecutionError("Adding a surface point requires an element/formation name.")

    create_report = {"created": False}
    if element not in set(_gempy_element_names(geo_model)) and auto_create_missing_element:
        create_report = _try_create_missing_gempy_element(gp, geo_model, element, relation=relation)

    xyz = [float(action.get("X", action.get("x"))), float(action.get("Y", action.get("y"))), float(action.get("Z", action.get("z")))]
    _call_gempy_candidates([
        ("add_surface_points(list name)", lambda: gp.add_surface_points(
            geo_model=geo_model,
            x=[xyz[0]], y=[xyz[1]], z=[xyz[2]],
            elements_names=[element],
        )),
        ("add_surface_points(single name)", lambda: gp.add_surface_points(
            geo_model=geo_model,
            x=[xyz[0]], y=[xyz[1]], z=[xyz[2]],
            elements_names=element,
        )),
        ("add_surface_points(positional)", lambda: gp.add_surface_points(geo_model, [xyz[0]], [xyz[1]], [xyz[2]], [element])),
    ], f"GemPy add_surface_points failed for element {element}.")
    return {"op": "add_surface", "element": element, "xyz": xyz, "missing_element": create_report}


def _gempy_api_add_orientation(gp: Any, geo_model: Any, action: Dict[str, Any], auto_create_missing_element: bool, relation: str) -> Dict[str, Any]:
    element = str(action.get("element") or action.get("formation") or "").strip()
    if not element:
        raise NodeExecutionError("Adding an orientation requires an element/formation name.")

    create_report = {"created": False}
    if element not in set(_gempy_element_names(geo_model)) and auto_create_missing_element:
        create_report = _try_create_missing_gempy_element(gp, geo_model, element, relation=relation)

    xyz = [float(action.get("X", action.get("x"))), float(action.get("Y", action.get("y"))), float(action.get("Z", action.get("z")))]
    vec = np.asarray([[float(action.get("G_x", action.get("gx", 0.0))), float(action.get("G_y", action.get("gy", 0.0))), float(action.get("G_z", action.get("gz", 1.0)))]], dtype=float)
    _call_gempy_candidates([
        ("add_orientations(pole_vector,list name)", lambda: gp.add_orientations(
            geo_model=geo_model,
            x=[xyz[0]], y=[xyz[1]], z=[xyz[2]],
            pole_vector=vec,
            elements_names=[element],
        )),
        ("add_orientations(pole_vector,single name)", lambda: gp.add_orientations(
            geo_model=geo_model,
            x=[xyz[0]], y=[xyz[1]], z=[xyz[2]],
            pole_vector=vec,
            elements_names=element,
        )),
        ("add_orientations(G_x/G_y/G_z)", lambda: gp.add_orientations(
            geo_model=geo_model,
            x=[xyz[0]], y=[xyz[1]], z=[xyz[2]],
            G_x=[float(vec[0, 0])], G_y=[float(vec[0, 1])], G_z=[float(vec[0, 2])],
            elements_names=[element],
        )),
    ], f"GemPy add_orientations failed for element {element}.")
    return {"op": "add_orientation", "element": element, "xyz": xyz, "vector": vec.ravel().tolist(), "missing_element": create_report}


def _gempy_api_current_row_by_uid(geo_model: Any, kind: str, uid: str) -> Optional[Dict[str, Any]]:
    """Return the current editable row for a UID from the GeoModel editor tables."""
    surface_df, orientation_df, _ = _extract_gempy_model_editor_tables(geo_model)
    df = orientation_df if str(kind).lower().startswith("ori") else surface_df
    if df.empty or "_editor_uid" not in df.columns:
        return None
    mask = df["_editor_uid"].astype(str) == str(uid)
    if not mask.any():
        return None
    row = df.loc[mask].iloc[0].to_dict()
    return row


def _normalized_element_name(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(_normalize_formation_series(pd.Series([value])).iloc[0])


def _element_changed(current_row: Optional[Dict[str, Any]], requested: Any) -> bool:
    if requested in (None, ""):
        return False
    current_name = _normalized_element_name((current_row or {}).get("formation", ""))
    requested_name = _normalized_element_name(requested)
    return bool(requested_name and requested_name != current_name)


def _gempy_api_modify_surface_point(gp: Any, geo_model: Any, action: Dict[str, Any]) -> Dict[str, Any]:
    uid = _geo_editor_action_uid(action)
    idx = _gempy_api_index_from_uid(geo_model, "surface", uid)
    if idx is None:
        return {"op": "modify_surface", "uid": uid, "warning": "uid not found after current edits"}

    current_row = _gempy_api_current_row_by_uid(geo_model, "surface", uid)
    requested_element = action.get("element", action.get("formation"))

    # GemPy stable versions generally do not allow changing the element via
    # modify_surface_points. Passing elements_names is interpreted as a field
    # update and can fail with "no field of name elements_names".
    # If the element really changed, implement re-assignment as add new point
    # to the requested element, then delete the original point.
    if _element_changed(current_row, requested_element):
        add_action = dict(action)
        add_action["element"] = _normalized_element_name(requested_element)
        add_report = _gempy_api_add_surface_point(gp, geo_model, add_action, auto_create_missing_element=False, relation="ERODE")
        delete_report = _gempy_api_delete_surface_point(gp, geo_model, {"uid": uid})
        return {
            "op": "reassign_surface_by_add_delete",
            "uid": uid,
            "old_index": idx,
            "old_element": (current_row or {}).get("formation"),
            "new_element": add_action["element"],
            "add": add_report,
            "delete": delete_report,
        }

    fields: Dict[str, Any] = {}
    for col in ["X", "Y", "Z"]:
        value = action.get(col, action.get(col.lower()))
        if value not in (None, ""):
            fields[col] = float(value)

    _call_gempy_candidates([
        ("modify_surface_points(model, idx, fields)", lambda: gp.modify_surface_points(geo_model, idx, **fields)),
        ("modify_surface_points(model, [idx], fields)", lambda: gp.modify_surface_points(geo_model, [idx], **fields)),
        ("modify_surface_points(geo_model=, indices=)", lambda: gp.modify_surface_points(geo_model=geo_model, indices=[idx], **fields)),
        ("modify_surface_points(geo_model=, index=)", lambda: gp.modify_surface_points(geo_model=geo_model, index=[idx], **fields)),
        ("modify_surface_points(model, idx, list fields)", lambda: gp.modify_surface_points(geo_model, idx, **{k: [v] for k, v in fields.items()})),
    ], "GemPy modify_surface_points failed.")
    return {"op": "modify_surface", "uid": uid, "index": idx, "fields": list(fields.keys())}



def _gempy_api_modify_orientation(gp: Any, geo_model: Any, action: Dict[str, Any]) -> Dict[str, Any]:
    uid = _geo_editor_action_uid(action)
    idx = _gempy_api_index_from_uid(geo_model, "orientation", uid)
    if idx is None:
        return {"op": "modify_orientation", "uid": uid, "warning": "uid not found after current edits"}

    current_row = _gempy_api_current_row_by_uid(geo_model, "orientation", uid)
    requested_element = action.get("element", action.get("formation"))

    # GemPy stable versions generally do not support changing orientation
    # element via modify_orientations. If the element changed, reassign by
    # adding a new orientation to the requested element and deleting the old one.
    if _element_changed(current_row, requested_element):
        add_action = dict(action)
        add_action["element"] = _normalized_element_name(requested_element)
        add_report = _gempy_api_add_orientation(gp, geo_model, add_action, auto_create_missing_element=False, relation="ERODE")
        delete_report = _gempy_api_delete_orientation(gp, geo_model, {"uid": uid})
        return {
            "op": "reassign_orientation_by_add_delete",
            "uid": uid,
            "old_index": idx,
            "old_element": (current_row or {}).get("formation"),
            "new_element": add_action["element"],
            "add": add_report,
            "delete": delete_report,
        }

    xyz_fields: Dict[str, Any] = {}
    for col in ["X", "Y", "Z"]:
        value = action.get(col, action.get(col.lower()))
        if value not in (None, ""):
            xyz_fields[col] = float(value)

    gx = action.get("G_x", action.get("gx"))
    gy = action.get("G_y", action.get("gy"))
    gz = action.get("G_z", action.get("gz"))

    # Prefer direct table fields G_x/G_y/G_z for modify_orientations. In the
    # installed stable GemPy API, pole_vector is usually accepted by add but not
    # necessarily by modify.
    g_fields: Dict[str, Any] = {}
    pole_fields: Dict[str, Any] = {}
    if gx not in (None, "") and gy not in (None, "") and gz not in (None, ""):
        g_fields = {"G_x": float(gx), "G_y": float(gy), "G_z": float(gz)}
        pole_fields = {"pole_vector": np.asarray([[float(gx), float(gy), float(gz)]], dtype=float)}

    fields_g = {**xyz_fields, **g_fields}
    fields_pole = {**xyz_fields, **pole_fields}

    _call_gempy_candidates([
        ("modify_orientations(model, idx, G fields)", lambda: gp.modify_orientations(geo_model, idx, **fields_g)),
        ("modify_orientations(model, [idx], G fields)", lambda: gp.modify_orientations(geo_model, [idx], **fields_g)),
        ("modify_orientations(model, idx, pole_vector)", lambda: gp.modify_orientations(geo_model, idx, **fields_pole)),
        ("modify_orientations(model, [idx], pole_vector)", lambda: gp.modify_orientations(geo_model, [idx], **fields_pole)),
        ("modify_orientations(geo_model=, indices=, G fields)", lambda: gp.modify_orientations(geo_model=geo_model, indices=[idx], **fields_g)),
        ("modify_orientations(geo_model=, index=, G fields)", lambda: gp.modify_orientations(geo_model=geo_model, index=[idx], **fields_g)),
    ], "GemPy modify_orientations failed.")
    return {"op": "modify_orientation", "uid": uid, "index": idx, "fields": list(fields_g.keys())}



def _gempy_api_delete_surface_point(gp: Any, geo_model: Any, action: Dict[str, Any]) -> Dict[str, Any]:
    uid = _geo_editor_action_uid(action)
    idx = _gempy_api_index_from_uid(geo_model, "surface", uid)
    if idx is None:
        return {"op": "delete_surface", "uid": uid, "warning": "uid not found after current edits"}

    _call_gempy_candidates([
        ("delete_surface_points(model, idx)", lambda: gp.delete_surface_points(geo_model, idx)),
        ("delete_surface_points(model, [idx])", lambda: gp.delete_surface_points(geo_model, [idx])),
        ("delete_surface_points(indices)", lambda: gp.delete_surface_points(geo_model=geo_model, indices=[idx])),
        ("delete_surface_points(index)", lambda: gp.delete_surface_points(geo_model=geo_model, index=[idx])),
    ], "GemPy delete_surface_points failed.")
    return {"op": "delete_surface", "uid": uid, "index": idx}



def _gempy_api_delete_orientation(gp: Any, geo_model: Any, action: Dict[str, Any]) -> Dict[str, Any]:
    uid = _geo_editor_action_uid(action)
    idx = _gempy_api_index_from_uid(geo_model, "orientation", uid)
    if idx is None:
        return {"op": "delete_orientation", "uid": uid, "warning": "uid not found after current edits"}

    _call_gempy_candidates([
        ("delete_orientations(model, idx)", lambda: gp.delete_orientations(geo_model, idx)),
        ("delete_orientations(model, [idx])", lambda: gp.delete_orientations(geo_model, [idx])),
        ("delete_orientations(indices)", lambda: gp.delete_orientations(geo_model=geo_model, indices=[idx])),
        ("delete_orientations(index)", lambda: gp.delete_orientations(geo_model=geo_model, index=[idx])),
    ], "GemPy delete_orientations failed.")
    return {"op": "delete_orientation", "uid": uid, "index": idx}



def _gempy_api_editor_preview(geo_model: Any, api_report: Dict[str, Any], applied: List[Dict[str, Any]]) -> Dict[str, Any]:
    surface_df, orientation_df, extract_report = _extract_gempy_model_editor_tables(geo_model)
    preview = _geo_editor_preview(surface_df, orientation_df, name="gempy_api_data_editor")
    preview.update({
        "preview_type": "interactive_gempy_api_data_editor",
        "editor_mode": "gempy_api_after_model_creation",
        "api_report": api_report,
        "applied_operations": applied,
        "message": "This editor modifies the existing GeoModel through GemPy add/modify/delete APIs. Run once to load points; queue edits in the right panel; run again to apply them.",
    })
    preview["extraction_report"] = extract_report
    return preview


class InteractiveGemPyModelDataEditorNode(BaseNode):
    type_name = "InteractiveGemPyModelDataEditor"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"InteractiveGemPyModelDataEditor expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        operations = _parse_json(params.get("operations_json"), [])
        if not isinstance(operations, list):
            raise NodeExecutionError("operations_json must be a list. Use the interactive editor UI instead of editing it manually.")

        auto_create_missing_element = _as_bool(params.get("auto_create_missing_element"), False)
        new_element_relation = str(params.get("new_element_relation") or "ERODE").strip().upper()
        fail_on_edit_error = _as_bool(params.get("fail_on_edit_error"), True)

        api_report = {
            "uses_gempy_api": True,
            "api_functions": [
                "gp.add_surface_points",
                "gp.add_orientations",
                "gp.modify_surface_points",
                "gp.modify_orientations",
                "gp.delete_surface_points",
                "gp.delete_orientations",
            ],
            "auto_create_missing_element": auto_create_missing_element,
            "new_element_relation": new_element_relation,
            "fail_on_edit_error": fail_on_edit_error,
        }
        applied: List[Dict[str, Any]] = []

        for action in operations:
            if not isinstance(action, dict):
                continue
            op = str(action.get("op") or "").strip().lower()
            kind = str(action.get("kind") or action.get("type") or "surface").strip().lower()
            try:
                if op == "add_orientation" or (op == "add" and kind.startswith("ori")):
                    applied.append(_gempy_api_add_orientation(gp, geo_model, action, auto_create_missing_element, new_element_relation))
                elif op in {"add", "add_surface"}:
                    applied.append(_gempy_api_add_surface_point(gp, geo_model, action, auto_create_missing_element, new_element_relation))
                elif op == "modify" and kind.startswith("ori"):
                    applied.append(_gempy_api_modify_orientation(gp, geo_model, action))
                elif op == "modify":
                    applied.append(_gempy_api_modify_surface_point(gp, geo_model, action))
                elif op == "delete" and kind.startswith("ori"):
                    applied.append(_gempy_api_delete_orientation(gp, geo_model, action))
                elif op == "delete":
                    applied.append(_gempy_api_delete_surface_point(gp, geo_model, action))
                else:
                    applied.append({"op": op, "kind": kind, "warning": "unknown operation"})
            except Exception as exc:
                applied.append({"op": op, "kind": kind, "uid": _geo_editor_action_uid(action), "error": str(exc)})

        if fail_on_edit_error:
            failures = [item for item in applied if item.get("error") or item.get("warning")]
            if failures:
                raise NodeExecutionError("Some GeoModel edits were not applied through the GemPy API: " + json.dumps(failures, ensure_ascii=False, default=str))
        preview = _gempy_api_editor_preview(geo_model, api_report, applied)
        return {
            "geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview, metadata=preview),
            "report": RuntimeValue("report", preview, name="interactive_gempy_api_data_editor", preview=preview, metadata=preview),
        }



class InteractiveGeoDataEditorNode(BaseNode):
    type_name = "InteractiveGeoDataEditor"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        sp_rv = _single(inputs, "surface_points")
        if sp_rv.kind != "table":
            raise NodeExecutionError(f"InteractiveGeoDataEditor surface_points input must be table, got {sp_rv.kind}")
        surface_df = _geo_editor_prepare_surface_df(sp_rv.value)

        ori_rv = inputs.get("orientations")
        if isinstance(ori_rv, list):
            ori_rv = ori_rv[0] if ori_rv else None
        orientation_df = _geo_editor_prepare_orientation_df(ori_rv.value if ori_rv is not None and ori_rv.kind == "table" else None)

        operations = _parse_json(params.get("operations_json"), [])
        if not isinstance(operations, list):
            raise NodeExecutionError("operations_json must be a list. Use the interactive editor instead of editing this manually.")

        edited_surface, edited_orientations, applied = _geo_editor_apply_operations(surface_df, orientation_df, operations)

        # Keep only user-facing columns on output, but preserve orientation vector columns.
        surface_out = edited_surface[[c for c in ["X", "Y", "Z", "formation"] if c in edited_surface.columns]].copy()
        orientation_cols = [c for c in ["X", "Y", "Z", "formation", "G_x", "G_y", "G_z"] if c in edited_orientations.columns]
        orientation_out = edited_orientations[orientation_cols].copy() if orientation_cols else edited_orientations.copy()

        preview = _geo_editor_preview(edited_surface, edited_orientations)
        preview["applied_operations"] = applied
        preview["operation_count"] = len(operations)
        preview["message"] = "Use the right-panel interactive editor to select, modify, delete or add points. Run this node again to apply queued edits to the output tables."

        return {
            "surface_points": RuntimeValue("table", surface_out, name="edited_surface_points", preview=_table_preview(surface_out, "edited_surface_points")),
            "orientations": RuntimeValue("table", orientation_out, name="edited_orientations", preview=_table_preview(orientation_out, "edited_orientations")),
            "report": RuntimeValue("report", preview, name="interactive_geological_point_editor", preview=preview, metadata=preview),
        }



class AddSurfacePointsNode(BaseNode):
    type_name = "AddSurfacePoints"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"AddSurfacePoints expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        # Optional table input with columns X, Y, Z, formation. This is useful when
        # the manually edited points were saved as a CSV/XLSX first.
        table_rv = inputs.get("points_table")
        if isinstance(table_rv, list):
            table_rv = table_rv[0] if table_rv else None

        added = []
        if table_rv is not None:
            if table_rv.kind != "table":
                raise NodeExecutionError(f"AddSurfacePoints points_table input must be a table, got {table_rv.kind}")
            df = table_rv.value.copy()
            required = ["X", "Y", "Z", "formation"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                raise NodeExecutionError(f"points_table is missing required columns: {missing}")
            for col in ["X", "Y", "Z"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["X", "Y", "Z", "formation"])
            for formation, group in df.groupby("formation", dropna=True):
                xs = group["X"].astype(float).tolist()
                ys = group["Y"].astype(float).tolist()
                zs = group["Z"].astype(float).tolist()
                self._add_points(gp, geo_model, xs, ys, zs, str(formation))
                added.append({"source": "table", "element": str(formation), "count": len(xs)})

        rows = _parse_json(params.get("points_json"), [])
        if not isinstance(rows, list):
            raise NodeExecutionError("Manual surface points parameter is invalid. Use the point editor in the right panel.")

        for row in rows:
            if not isinstance(row, dict):
                continue
            element = str(row.get("element") or "").strip()
            if not element:
                continue
            mode = str(row.get("mode") or "single").lower()
            if mode == "grid":
                xs = _parse_number_list(row.get("xs"))
                ys = _parse_number_list(row.get("ys"))
                z = _as_float(row.get("z"), None)
                if not xs or not ys or z is None:
                    raise NodeExecutionError(f"Grid row for {element} needs xs, ys and z.")
                x_values = []
                y_values = []
                z_values = []
                for y in ys:
                    for x in xs:
                        x_values.append(float(x))
                        y_values.append(float(y))
                        z_values.append(float(z))
                self._add_points(gp, geo_model, x_values, y_values, z_values, element)
                added.append({"source": "manual_grid", "element": element, "count": len(x_values), "xs": len(xs), "ys": len(ys), "z": z})
            else:
                x = _as_float(row.get("x"), None)
                y = _as_float(row.get("y"), None)
                z = _as_float(row.get("z"), None)
                if x is None or y is None or z is None:
                    raise NodeExecutionError(f"Single point row for {element or '<missing element>'} needs x, y and z.")
                self._add_points(gp, geo_model, [x], [y], [z], element)
                added.append({"source": "manual_single", "element": element, "count": 1, "x": x, "y": y, "z": z})

        preview = {"added_surface_point_groups": added, "total_added_points": int(sum(item["count"] for item in added))}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview)}

    @staticmethod
    def _add_points(gp: Any, geo_model: Any, xs: List[float], ys: List[float], zs: List[float], element: str) -> None:
        if not xs:
            return
        try:
            gp.add_surface_points(
                geo_model=geo_model,
                x=xs,
                y=ys,
                z=zs,
                elements_names=[element] * len(xs),
            )
        except Exception:
            # Some GemPy versions/examples pass a single-element list for one
            # point. Fallback to point-by-point calls for maximum compatibility.
            for x, y, z in zip(xs, ys, zs):
                gp.add_surface_points(
                    geo_model=geo_model,
                    x=[float(x)],
                    y=[float(y)],
                    z=[float(z)],
                    elements_names=[element],
                )


def _clean_layer_styles(raw: Any) -> List[Dict[str, Any]]:
    layer_styles = _parse_json(raw, [])
    if not isinstance(layer_styles, list):
        raise NodeExecutionError("Layer style parameter is invalid. Use the layer style editor in the right panel.")
    cleaned_styles: List[Dict[str, Any]] = []
    for row in layer_styles:
        if not isinstance(row, dict):
            continue
        try:
            layer_id = int(row.get("id"))
        except Exception:
            continue
        cleaned_styles.append({
            "id": layer_id,
            "label": str(row.get("label") or f"layer_{layer_id}"),
            "color": str(row.get("color") or ""),
            "opacity": float(row.get("opacity", 0.5)),
        })
    return cleaned_styles


def _patch_pyvista_add_mesh_positional_color_for_nodes() -> None:
    try:
        import pyvista as pv
        BasePlotter = pv.plotting.plotter.BasePlotter
        original = getattr(BasePlotter, "_gempy_node_editor_original_add_mesh", None)
        if original is None:
            original = BasePlotter.add_mesh
            setattr(BasePlotter, "_gempy_node_editor_original_add_mesh", original)

        def add_mesh_compat(self, *args, **kwargs):
            if len(args) >= 2 and "color" not in kwargs:
                maybe_color = args[1]
                if isinstance(maybe_color, (str, tuple, list)):
                    kwargs["color"] = maybe_color
                    args = (args[0],) + tuple(args[2:])
            return original(self, *args, **kwargs)

        BasePlotter.add_mesh = add_mesh_compat
    except Exception:
        pass


def _get_gempy_regular_grid_mesh(geo_model: Any):
    try:
        import pyvista as pv
        import gempy_viewer as gpv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista/gempy_viewer is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    _patch_pyvista_add_mesh_positional_color_for_nodes()
    plot_obj = None
    errors: List[str] = []
    candidate_kwargs = [
        {"show": False, "show_data": False, "show_lith": True, "show_surfaces": False, "show_topography": False},
        {"show_data": False, "show_lith": True, "show_surfaces": False, "show_topography": False},
        {},
    ]
    for kwargs in candidate_kwargs:
        try:
            plot_obj = gpv.plot_3d(geo_model, **kwargs)
            break
        except TypeError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
    if plot_obj is None:
        raise NodeExecutionError("Could not obtain GemPy 3D plot object. " + " | ".join(errors[-3:]))

    actor = getattr(plot_obj, "regular_grid_actor", None)
    if actor is None and hasattr(plot_obj, "plotter"):
        actor = getattr(plot_obj.plotter, "regular_grid_actor", None)
    if actor is None:
        raise NodeExecutionError("The GemPy 3D plot object has no regular_grid_actor. Use a GemPy/gempy_viewer version that exposes regular_grid_actor.")
    return pv.wrap(actor.GetMapper().GetInput())


def _crop_mesh_to_xy_bounds(mesh: Any, bounds: Sequence[float]):
    try:
        centers = mesh.cell_centers().points
        ids = np.where(
            (centers[:, 0] >= float(bounds[0])) & (centers[:, 0] <= float(bounds[1])) &
            (centers[:, 1] >= float(bounds[2])) & (centers[:, 1] <= float(bounds[3]))
        )[0]
        if ids.size:
            return mesh.extract_cells(ids)
    except Exception:
        pass
    return mesh


def _build_clipped_gempy_layer_mesh(
    geo_model: Any,
    *,
    clip_path: Optional[Path] = None,
    top_path: Optional[Path] = None,
    cell_data_name: str = "id",
    layer_styles: Optional[List[Dict[str, Any]]] = None,
    invert: bool = False,
    crinkle: bool = True,
    topography_clip_enabled: bool = True,
    topography_invert: bool = False,
    crop_to_topography_xy: bool = True,
    clean_output_mesh: bool = True,
):
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    vol = _get_gempy_regular_grid_mesh(geo_model)
    operations: List[str] = ["regular_grid_actor"]

    if clip_path is not None:
        shell = pv.read(str(clip_path))
        vol = vol.clip_surface(shell, invert=bool(invert), crinkle=bool(crinkle))
        operations.append(f"clip_surface({clip_path.name}, invert={bool(invert)})")

    if top_path is not None and bool(topography_clip_enabled):
        top = pv.read(str(top_path))
        if crop_to_topography_xy:
            vol = _crop_mesh_to_xy_bounds(vol, top.bounds)
            operations.append(f"crop_to_topography_xy_bounds({top_path.name})")
        vol = vol.clip_surface(top, invert=bool(topography_invert), crinkle=bool(crinkle))
        operations.append(f"clip_surface_topography({top_path.name}, invert={bool(topography_invert)})")

    if cell_data_name not in vol.cell_data:
        available = list(vol.cell_data.keys())
        raise NodeExecutionError(f"Clipped volume has no cell_data '{cell_data_name}'. Available cell arrays: {available}")

    styles_local = list(layer_styles or [])
    if styles_local:
        layer_ids = [int(row.get("id")) for row in styles_local if str(row.get("id", "")).strip() != ""]
    else:
        layer_ids = [int(v) for v in np.unique(vol.cell_data[cell_data_name])]
        styles_local = [{"id": int(v), "label": f"layer_{int(v)}", "color": "", "opacity": 0.5} for v in layer_ids]

    parts = []
    extracted_counts: Dict[str, int] = {}
    for row in styles_local:
        try:
            layer_id = int(row.get("id"))
        except Exception:
            continue
        idx = np.where(vol.cell_data[cell_data_name] == layer_id)[0]
        extracted_counts[str(layer_id)] = int(idx.size)
        if idx.size == 0:
            continue
        layer = vol.extract_cells(idx)
        # Keep the material/layer id explicit for later export/conversion.
        try:
            layer.cell_data[cell_data_name] = np.full(layer.n_cells, layer_id, dtype=np.asarray(vol.cell_data[cell_data_name]).dtype)
        except Exception:
            pass
        parts.append(layer.cast_to_unstructured_grid())

    if parts:
        combined = parts[0].copy()
        for part in parts[1:]:
            combined = combined.merge(part, merge_points=False)
    else:
        combined = vol.cast_to_unstructured_grid()

    if clean_output_mesh:
        try:
            combined = combined.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
        except TypeError:
            combined = combined.clean()
        except Exception:
            pass

    metadata = {
        "operations": operations,
        "n_cells": int(combined.n_cells),
        "n_points": int(combined.n_points),
        "bounds": [float(v) for v in combined.bounds],
        "cell_data": list(combined.cell_data.keys()),
        "extracted_layer_cell_counts": extracted_counts,
    }
    return combined, metadata



def _mesh_from_runtime_value(rv: RuntimeValue) -> tuple[Any, str, Dict[str, Any]]:
    """Resolve a RuntimeValue into a PyVista mesh."""
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    if rv.kind == "mesh":
        return rv.value, rv.name or "mesh", {"source_kind": "mesh", "source_name": rv.name or "mesh"}

    if rv.kind == "file":
        path = Path(rv.value)
        if not path.exists() and rv.metadata and rv.metadata.get("file_id"):
            path = get_file_path(str(rv.metadata["file_id"]))
        try:
            mesh = pv.read(str(path))
        except Exception as exc:
            raise NodeExecutionError(f"Could not read mesh file input {path}: {exc}") from exc
        return mesh, path.name, {"source_kind": "file", "source_name": path.name}

    raise NodeExecutionError(f"Combine Meshes accepts mesh/file inputs, got {rv.kind} from {rv.name}.")


def _ensure_mesh_numeric_cell_array(mesh: Any, name: str, value: float = np.nan) -> None:
    try:
        if name not in mesh.cell_data:
            mesh.cell_data[name] = np.full(int(mesh.n_cells), value, dtype=float)
    except Exception:
        pass


def _ensure_mesh_numeric_point_array(mesh: Any, name: str, value: float = np.nan) -> None:
    try:
        if name not in mesh.point_data:
            mesh.point_data[name] = np.full(int(mesh.n_points), value, dtype=float)
    except Exception:
        pass


def _numeric_data_keys(data: Any) -> List[str]:
    keys: List[str] = []
    try:
        for key, arr in data.items():
            a = np.asarray(arr)
            if np.issubdtype(a.dtype, np.number) and a.ndim == 1:
                keys.append(str(key))
    except Exception:
        pass
    return keys



def _choose_reindex_scalar_for_mesh(mesh: Any, requested: str = "auto") -> tuple[str, str]:
    """Return (association, scalar_name) for reindexing element IDs."""
    req = str(requested or "auto").strip()
    cell_keys = [str(k) for k in getattr(mesh, "cell_data", {}).keys()]
    point_keys = [str(k) for k in getattr(mesh, "point_data", {}).keys()]

    def numeric_in(keys: List[str], data: Any, name: str) -> bool:
        for k in keys:
            if k == name or k.lower() == name.lower():
                try:
                    arr = np.asarray(data[k])
                    return np.issubdtype(arr.dtype, np.number)
                except Exception:
                    return False
        return False

    if req and req.lower() not in {"auto", "none", ""}:
        for k in cell_keys:
            if k == req or k.lower() == req.lower():
                return "cell", k
        for k in point_keys:
            if k == req or k.lower() == req.lower():
                return "point", k
        return "", ""

    priority = [
        # First priority: already reindexed IDs from a previous Combine Meshes run.
        # This makes hierarchical combining stable:
        #   combine 14 meshes -> combined_element_id = 1..14
        #   combine that result with 2 more meshes -> 1..16, not 1..3.
        "combined_element_id", "combinedElementId", "combined_element_ids",
        "global_element_id", "globalElementId", "merged_element_id", "mergedElementId",
        "unique_element_id", "uniqueElementId",
        # Original/native IDs from GemPy/PyVista/VTK outputs.
        "element_id", "ElementID", "element_ids", "ElementIDs",
        "formation_id", "FormationID",
        "layer_id", "LayerID",
        "lith_id", "LithID",
        "id", "ids",
        "MaterialIDs", "MaterialID", "material_ids", "material_id",
        "lith_block", "lithology", "layer",
    ]
    for p in priority:
        for k in cell_keys:
            if (k == p or k.lower() == p.lower()) and numeric_in([k], mesh.cell_data, k):
                return "cell", k
        for k in point_keys:
            if (k == p or k.lower() == p.lower()) and numeric_in([k], mesh.point_data, k):
                return "point", k

    # Last numeric cell scalar fallback, avoiding source_id/cell_ids.
    for k in cell_keys:
        if k.lower() in {"source_id", "cell_id", "cell_ids", "vtkoriginalcellids", "vtkoriginalpointids"}:
            continue
        try:
            arr = np.asarray(mesh.cell_data[k])
            if np.issubdtype(arr.dtype, np.number) and arr.ndim == 1:
                return "cell", k
        except Exception:
            pass
    for k in point_keys:
        if k.lower() in {"source_id", "point_id", "point_ids", "vtkoriginalcellids", "vtkoriginalpointids"}:
            continue
        try:
            arr = np.asarray(mesh.point_data[k])
            if np.issubdtype(arr.dtype, np.number) and arr.ndim == 1:
                return "point", k
        except Exception:
            pass
    return "", ""


def _mesh_cell_point_ids(mesh: Any, cell_index: int) -> List[int]:
    try:
        cell = mesh.get_cell(int(cell_index))
        ids = getattr(cell, "point_ids", None)
        if ids is not None:
            return [int(v) for v in ids]
    except Exception:
        pass
    try:
        # PolyData faces/lines fallback
        if hasattr(mesh, "faces") and np.asarray(mesh.faces).size:
            raw = np.asarray(mesh.faces, dtype=np.int64).ravel()
            i = 0
            c = 0
            while i < raw.size:
                n = int(raw[i])
                ids = raw[i + 1:i + 1 + n].astype(int).tolist()
                if c == cell_index:
                    return ids
                i += n + 1
                c += 1
        if hasattr(mesh, "lines") and np.asarray(mesh.lines).size:
            raw = np.asarray(mesh.lines, dtype=np.int64).ravel()
            i = 0
            c = 0
            while i < raw.size:
                n = int(raw[i])
                ids = raw[i + 1:i + 1 + n].astype(int).tolist()
                if c == cell_index:
                    return ids
                i += n + 1
                c += 1
    except Exception:
        pass
    return []


def _point_scalar_to_cell_scalar(mesh: Any, scalar_name: str, missing_value: int = -1) -> np.ndarray:
    values = np.asarray(mesh.point_data[scalar_name])
    out = np.full(int(mesh.n_cells), int(missing_value), dtype=np.int32)
    for ci in range(int(mesh.n_cells)):
        ids = _mesh_cell_point_ids(mesh, ci)
        if not ids:
            continue
        vals = []
        for pid in ids:
            if 0 <= int(pid) < values.shape[0]:
                try:
                    v = float(values[int(pid)])
                    if np.isfinite(v):
                        vals.append(v)
                except Exception:
                    pass
        if vals:
            # For categorical IDs use majority vote; ties fall back to smallest.
            unique, counts = np.unique(np.asarray(vals), return_counts=True)
            out[ci] = int(unique[np.argmax(counts)])
    return out


def _apply_unique_reindexed_scalar_to_meshes(
    meshes: List[Any],
    sources: List[Dict[str, Any]],
    *,
    source_scalar: str = "auto",
    output_scalar: str = "combined_element_id",
    scope: str = "source_and_element",
    start_id: int = 1,
    missing_value: int = -1,
) -> Dict[str, Any]:
    """Create a new cell-data scalar with unique IDs across input meshes.

    Default scope is source_and_element: (input mesh index, original element id)
    gets a unique new id. Therefore element_id=1 in mesh A and element_id=1 in
    mesh B become different combined IDs.
    """
    mapping: Dict[str, int] = {}
    reverse_rows: List[Dict[str, Any]] = []
    next_id = int(start_id)

    output_scalar = str(output_scalar or "combined_element_id").strip() or "combined_element_id"
    scope = str(scope or "source_and_element").strip().lower()
    if scope not in {"source_and_element", "global_element"}:
        scope = "source_and_element"

    already_combined_inputs: List[Dict[str, Any]] = []

    for mesh_i, mesh in enumerate(meshes):
        assoc, scalar_name = _choose_reindex_scalar_for_mesh(mesh, source_scalar)
        if str(scalar_name).lower() in {"combined_element_id", "combinedelementid", "global_element_id", "merged_element_id", "unique_element_id"}:
            already_combined_inputs.append({
                "source_index": int(mesh_i),
                "scalar": str(scalar_name),
                "association": str(assoc),
            })
        assigned = np.full(int(mesh.n_cells), int(missing_value), dtype=np.int32)

        if assoc == "cell" and scalar_name:
            raw = np.asarray(mesh.cell_data[scalar_name])
            if raw.shape[0] != int(mesh.n_cells):
                raw = np.resize(raw, int(mesh.n_cells))
        elif assoc == "point" and scalar_name:
            raw = _point_scalar_to_cell_scalar(mesh, scalar_name, missing_value=missing_value)
        else:
            raw = np.full(int(mesh.n_cells), int(missing_value), dtype=np.int32)

        for ci, original_value in enumerate(raw):
            try:
                fv = float(original_value)
                if not np.isfinite(fv):
                    assigned[ci] = int(missing_value)
                    continue
                # Keep integer-looking values tidy, but support non-integer scalar ids too.
                original_key_value = str(int(fv)) if abs(fv - int(fv)) < 1e-8 else repr(float(fv))
            except Exception:
                assigned[ci] = int(missing_value)
                continue

            if int(float(original_key_value)) == int(missing_value) if original_key_value.lstrip("-").isdigit() else False:
                assigned[ci] = int(missing_value)
                continue

            if scope == "global_element":
                key = f"element={original_key_value}"
            else:
                key = f"source={mesh_i}|element={original_key_value}"

            if key not in mapping:
                mapping[key] = int(next_id)
                reverse_rows.append({
                    "new_id": int(next_id),
                    "source_index": int(mesh_i) if scope == "source_and_element" else None,
                    "source_name": sources[mesh_i].get("source_name") if mesh_i < len(sources) else "",
                    "original_scalar": scalar_name,
                    "original_association": assoc,
                    "original_value": original_key_value,
                    "scope": scope,
                })
                next_id += 1
            assigned[ci] = int(mapping[key])

        try:
            mesh.cell_data[output_scalar] = assigned
        except Exception:
            pass

        if mesh_i < len(sources):
            sources[mesh_i]["reindex_scalar_source"] = scalar_name
            sources[mesh_i]["reindex_scalar_association"] = assoc
            sources[mesh_i]["reindexed_scalar_name"] = output_scalar
            sources[mesh_i]["reindexed_unique_ids"] = sorted([int(v) for v in np.unique(assigned) if int(v) != int(missing_value)])

    return {
        "enabled": True,
        "output_scalar": output_scalar,
        "source_scalar_requested": source_scalar,
        "scope": scope,
        "start_id": int(start_id),
        "missing_value": int(missing_value),
        "mapping": reverse_rows,
        "n_unique_ids": int(len(reverse_rows)),
        "already_combined_inputs": already_combined_inputs,
        "note": "Auto source-scalar detection prefers existing combined_element_id values. This preserves previously combined element subdivisions during hierarchical Combine Meshes workflows.",
    }



class CombineMeshesNode(BaseNode):
    type_name = "CombineMeshes"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import pyvista as pv
        except Exception as exc:
            raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

        mesh_values: List[RuntimeValue] = []
        # Allow both many connections into the same "meshes" port and explicit
        # mesh_1 ... mesh_8 ports. This makes the node usable even if the
        # frontend only allows one visible edge per port in some layouts.
        for port_name in ["meshes", "mesh", "mesh_1", "mesh_2", "mesh_3", "mesh_4", "mesh_5", "mesh_6", "mesh_7", "mesh_8"]:
            mesh_values.extend(_many(inputs, port_name))

        # Optional uploaded/output mesh file ids, comma-separated.
        file_ids_text = str(params.get("mesh_file_ids") or "").strip()
        if file_ids_text:
            for file_id in [x.strip() for x in file_ids_text.replace(";", ",").split(",") if x.strip()]:
                try:
                    path = get_file_path(file_id)
                    mesh_values.append(RuntimeValue("file", path, name=path.name, metadata={"file_id": file_id}))
                except Exception as exc:
                    raise NodeExecutionError(f"Could not resolve mesh file id {file_id}: {exc}") from exc

        if not mesh_values:
            raise NodeExecutionError("Combine Meshes needs at least one connected mesh/file input or mesh_file_ids.")

        extract_surface = _as_bool(params.get("extract_surface"), False)
        clean_inputs = _as_bool(params.get("clean_inputs"), False)
        clean_output = _as_bool(params.get("clean_output"), True)
        merge_points = _as_bool(params.get("merge_points"), False)
        add_source_id = _as_bool(params.get("add_source_id"), True)
        preserve_numeric_arrays = _as_bool(params.get("preserve_numeric_arrays"), True)
        create_reindexed_scalar = _as_bool(params.get("create_reindexed_element_id"), True)
        reindex_source_scalar = str(params.get("reindex_source_scalar") or "auto").strip() or "auto"
        reindexed_scalar_name = str(params.get("reindexed_scalar_name") or "combined_element_id").strip() or "combined_element_id"
        reindex_scope = str(params.get("reindex_scope") or "source_and_element").strip()
        reindex_start_id = _as_int(params.get("reindex_start_id"), 1) or 1
        reindex_missing_value = _as_int(params.get("reindex_missing_value"), -1)
        if reindex_missing_value is None:
            reindex_missing_value = -1
        tolerance = _as_float(params.get("tolerance"), 0.0) or 0.0
        output_name = str(params.get("output_file_name") or "combined_mesh.vtu").strip() or "combined_mesh.vtu"
        if not Path(output_name).suffix:
            output_name += ".vtu"
        suffix = Path(output_name).suffix.lower()
        if suffix not in {".vtk", ".vtu", ".vtp", ".ply", ".stl"}:
            raise NodeExecutionError("Output file name must end with .vtu, .vtp, .vtk, .ply, or .stl.")

        meshes: List[Any] = []
        sources: List[Dict[str, Any]] = []

        for i, rv in enumerate(mesh_values):
            mesh, source_name, meta = _mesh_from_runtime_value(rv)
            if mesh is None or int(getattr(mesh, "n_points", 0)) == 0:
                sources.append({**meta, "index": i, "source_name": source_name, "skipped": True, "reason": "empty mesh"})
                continue

            try:
                m = mesh.copy(deep=True)
            except Exception:
                m = mesh.copy()

            if extract_surface:
                try:
                    m = m.extract_surface()
                except Exception as exc:
                    raise NodeExecutionError(f"Could not extract surface for mesh {source_name}: {exc}") from exc

            if clean_inputs:
                try:
                    m = m.clean(tolerance=float(tolerance))
                except TypeError:
                    m = m.clean()
                except Exception:
                    pass

            if add_source_id:
                try:
                    m.cell_data["source_id"] = np.full(int(m.n_cells), i, dtype=np.int32)
                    m.point_data["source_id"] = np.full(int(m.n_points), i, dtype=np.int32)
                except Exception:
                    pass

            meshes.append(m)
            sources.append({
                **meta,
                "index": i,
                "source_name": source_name,
                "n_points": int(getattr(m, "n_points", 0)),
                "n_cells": int(getattr(m, "n_cells", 0)),
                "class": m.__class__.__name__,
                "cell_data": list(getattr(m, "cell_data", {}).keys()),
                "point_data": list(getattr(m, "point_data", {}).keys()),
            })

        if not meshes:
            raise NodeExecutionError("All mesh inputs were empty or invalid.")

        reindex_report: Dict[str, Any] = {"enabled": False}
        if create_reindexed_scalar:
            reindex_report = _apply_unique_reindexed_scalar_to_meshes(
                meshes,
                sources,
                source_scalar=reindex_source_scalar,
                output_scalar=reindexed_scalar_name,
                scope=reindex_scope,
                start_id=int(reindex_start_id),
                missing_value=int(reindex_missing_value),
            )

        if preserve_numeric_arrays:
            cell_keys: List[str] = sorted(set(k for m in meshes for k in _numeric_data_keys(getattr(m, "cell_data", {}))))
            point_keys: List[str] = sorted(set(k for m in meshes for k in _numeric_data_keys(getattr(m, "point_data", {}))))
            for m in meshes:
                for key in cell_keys:
                    _ensure_mesh_numeric_cell_array(m, key, np.nan)
                for key in point_keys:
                    _ensure_mesh_numeric_point_array(m, key, np.nan)

        combine_mode = str(params.get("combine_mode") or "merge").strip().lower()
        try:
            if len(meshes) == 1:
                combined = meshes[0].copy(deep=True)
            elif combine_mode == "multiblock_combine":
                combined = pv.MultiBlock(meshes).combine(merge_points=bool(merge_points), tolerance=float(tolerance))
            else:
                combined = meshes[0]
                for m in meshes[1:]:
                    try:
                        combined = combined.merge(m, merge_points=bool(merge_points), tolerance=float(tolerance))
                    except TypeError:
                        combined = combined.merge(m, merge_points=bool(merge_points))
        except Exception as exc:
            raise NodeExecutionError(f"Could not combine meshes: {exc}") from exc

        if clean_output:
            try:
                combined = combined.clean(tolerance=float(tolerance))
            except TypeError:
                combined = combined.clean()
            except Exception:
                pass

        # PyVista has strict writer/extension rules. The Plot GemPy 3D output is
        # PolyData surface mesh, which cannot be saved as .vtu. The v72 default
        # output name was combined_mesh.vtu, so combining PlotGemPy3D surfaces
        # failed. In auto mode, adapt the extension to the actual mesh type.
        output_extension_adjustment = ""
        try:
            is_polydata = isinstance(combined, pv.PolyData)
            is_unstructured = isinstance(combined, pv.UnstructuredGrid)
        except Exception:
            is_polydata = "polydata" in combined.__class__.__name__.lower()
            is_unstructured = "unstructured" in combined.__class__.__name__.lower()

        if is_polydata and suffix == ".vtu":
            output_extension_adjustment = "Combined mesh is PolyData/surface data, so output extension was changed from .vtu to .vtp."
            output_name = str(Path(output_name).with_suffix(".vtp"))
            suffix = ".vtp"
        elif (not is_polydata) and suffix == ".vtp":
            if _as_bool(params.get("auto_extract_surface_for_vtp"), True):
                try:
                    combined = combined.extract_surface()
                    output_extension_adjustment = "Output extension is .vtp, so a surface was extracted before saving."
                except Exception as exc:
                    raise NodeExecutionError(f"Output is .vtp but combined mesh is not PolyData, and surface extraction failed: {exc}") from exc
            else:
                raise NodeExecutionError("Output file ends with .vtp, but combined mesh is not PolyData. Enable auto_extract_surface_for_vtp or choose .vtu/.vtk output.")

        out_path = make_runtime_path(Path(output_name).stem, suffix)
        try:
            combined.save(out_path)
        except ValueError as exc:
            # Last-resort fallback: if the chosen VTK XML extension is still not
            # compatible with the data object, save as legacy .vtk. This supports
            # most PyVista datasets and prevents losing the combined result.
            fallback_name = str(Path(output_name).with_suffix(".vtk"))
            fallback_path = make_runtime_path(Path(fallback_name).stem, ".vtk")
            try:
                combined.save(fallback_path)
                output_extension_adjustment = (output_extension_adjustment + " " if output_extension_adjustment else "") + f"Requested extension {suffix} was not accepted by PyVista; saved as .vtk fallback."
                output_name = fallback_name
                suffix = ".vtk"
                out_path = fallback_path
            except Exception:
                raise NodeExecutionError(f"Could not save combined mesh: {exc}") from exc
        except Exception as exc:
            raise NodeExecutionError(f"Could not save combined mesh: {exc}") from exc

        record = register_output_file(out_path, display_name=Path(output_name).name)
        file_id = record["file_id"]

        scalar = _choose_mesh_scalar(combined, str(params.get("mesh_scalars") or (reindexed_scalar_name if create_reindexed_scalar else "auto")))
        if create_reindexed_scalar and reindexed_scalar_name in getattr(combined, "cell_data", {}):
            scalar = reindexed_scalar_name
        preview = _mesh_preview(combined, Path(output_name).name, file_id=file_id)
        preview.update({
            "preview_type": "mesh",
            "operation": "combine_meshes",
            "combine_mode": combine_mode,
            "source_count": len(meshes),
            "sources": sources,
            "merge_points": bool(merge_points),
            "extract_surface": bool(extract_surface),
            "clean_inputs": bool(clean_inputs),
            "clean_output": bool(clean_output),
            "preserve_numeric_arrays": bool(preserve_numeric_arrays),
            "reindex_report": reindex_report,
            "scalar_used": scalar,
            "output_extension_adjustment": output_extension_adjustment,
            "saved_output_file": Path(output_name).name,
            "saved_output_suffix": suffix,
            "combined_mesh_class": combined.__class__.__name__,
            "pyvista_preview_url": f"/api/pyvista/mesh/{file_id}?{urlencode({'show_edges': str(_as_bool(params.get('show_edges'), False)).lower(), 'scalars': scalar or ''})}",
            "pyvista_button_label": "Enlarge / Open Combined Mesh",
        })
        _attach_web_surface_preview(preview, combined, Path(output_name).stem, preferred_scalar=scalar or "", show_edges=_as_bool(params.get("show_edges"), False))
        preview["web_viewer_label"] = "Combined mesh"
        preview["inline_display"] = "web_3d_viewer"

        return {
            "mesh": RuntimeValue("mesh", combined, name=Path(output_name).name, preview=preview, metadata={"file_id": file_id}),
            "file": RuntimeValue("file", out_path, name=Path(output_name).name, preview=preview, metadata={"file_id": file_id}),
            "report": RuntimeValue("report", preview, name="combine_meshes_report", preview=preview, metadata=preview),
        }





def _clip_pyvista_dataset_with_surfaces(
    base_mesh: Any,
    *,
    clip_path: Optional[Path],
    top_path: Optional[Path],
    invert: bool = False,
    crinkle: bool = True,
    topography_clip_enabled: bool = True,
    topography_invert: bool = False,
    crop_to_topography_xy: bool = True,
    clean_output_mesh: bool = True,
) -> tuple[Any, Dict[str, Any]]:
    """Clip an already-existing PyVista mesh with optional shell/topography surfaces."""
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    try:
        mesh = base_mesh.copy(deep=True)
    except Exception:
        mesh = base_mesh.copy()

    operations: List[str] = ["input_mesh"]

    if clip_path is not None:
        shell = pv.read(str(clip_path))
        try:
            mesh = mesh.clip_surface(shell, invert=bool(invert), crinkle=bool(crinkle))
        except TypeError:
            mesh = mesh.clip_surface(shell, invert=bool(invert))
        operations.append(f"clip_surface({clip_path.name}, invert={bool(invert)})")

    if top_path is not None and bool(topography_clip_enabled):
        top = pv.read(str(top_path))
        if crop_to_topography_xy:
            mesh = _crop_mesh_to_xy_bounds(mesh, top.bounds)
            operations.append(f"crop_to_topography_xy_bounds({top_path.name})")
        try:
            mesh = mesh.clip_surface(top, invert=bool(topography_invert), crinkle=bool(crinkle))
        except TypeError:
            mesh = mesh.clip_surface(top, invert=bool(topography_invert))
        operations.append(f"clip_surface_topography({top_path.name}, invert={bool(topography_invert)})")

    if clean_output_mesh:
        try:
            mesh = mesh.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
        except TypeError:
            try:
                mesh = mesh.clean()
            except Exception:
                pass
        except Exception:
            pass

    metadata = {
        "operations": operations,
        "input_mode": "mesh",
        "n_cells": int(getattr(mesh, "n_cells", 0)),
        "n_points": int(getattr(mesh, "n_points", 0)),
        "bounds": [float(v) for v in getattr(mesh, "bounds", [])],
        "cell_data": list(getattr(mesh, "cell_data", {}).keys()),
        "point_data": list(getattr(mesh, "point_data", {}).keys()),
    }
    return mesh, metadata


def _save_pyvista_mesh_compatible(mesh: Any, requested_name: str) -> tuple[Path, str, str]:
    """Save a PyVista mesh while adapting extensions to dataset type.

    Returns (path, final_display_name, adjustment_note).
    """
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    out_name = str(requested_name or "mesh.vtu").strip() or "mesh.vtu"
    if not Path(out_name).suffix:
        out_name += ".vtu"
    suffix = Path(out_name).suffix.lower()
    adjustment = ""

    try:
        is_polydata = isinstance(mesh, pv.PolyData)
    except Exception:
        is_polydata = "polydata" in mesh.__class__.__name__.lower()

    if is_polydata and suffix == ".vtu":
        out_name = str(Path(out_name).with_suffix(".vtp"))
        suffix = ".vtp"
        adjustment = "Output mesh is PolyData/surface data, so output extension was changed from .vtu to .vtp."
    elif (not is_polydata) and suffix == ".vtp":
        try:
            mesh = mesh.extract_surface()
            adjustment = "Requested output is .vtp, so a surface was extracted before saving."
        except Exception as exc:
            raise NodeExecutionError(f"Output is .vtp but mesh is not PolyData, and surface extraction failed: {exc}") from exc

    path = make_runtime_path(Path(out_name).stem, suffix)
    try:
        mesh.save(path)
    except ValueError as exc:
        fallback_name = str(Path(out_name).with_suffix(".vtk"))
        fallback_path = make_runtime_path(Path(fallback_name).stem, ".vtk")
        try:
            mesh.save(fallback_path)
            adjustment = (adjustment + " " if adjustment else "") + f"Requested extension {suffix} was not accepted by PyVista; saved as .vtk fallback."
            return fallback_path, Path(fallback_name).name, adjustment
        except Exception:
            raise NodeExecutionError(f"Could not save mesh: {exc}") from exc
    except Exception as exc:
        raise NodeExecutionError(f"Could not save mesh: {exc}") from exc

    return path, Path(out_name).name, adjustment





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
    for ci, face in enumerate(faces):
        if len(face) < 3:
            continue
        for j, a in enumerate(face):
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


def _build_normal_thickened_mesh(
    mesh: Any,
    *,
    distance: float,
    mode: str = "symmetric",
    close_sides: bool = True,
    triangulate: bool = True,
    consistent_normals: bool = True,
    auto_orient_normals: bool = True,
    flip_normals: bool = False,
    clean_input: bool = True,
    clean_output: bool = True,
) -> tuple[Any, Dict[str, Any]]:
    """Thicken a surface mesh by offsetting points along point normals.

    The result is a PolyData shell surface. For open surfaces, boundary side
    faces are generated so the buffer becomes a closed shell surface.
    """
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    d = float(distance)
    if not np.isfinite(d) or abs(d) <= 0:
        raise NodeExecutionError("Buffer distance must be a non-zero finite number.")

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

    if int(getattr(surf, "n_points", 0)) == 0 or int(getattr(surf, "n_cells", 0)) == 0:
        raise NodeExecutionError("Input mesh has no surface points/cells to thicken.")

    try:
        surf_n = surf.compute_normals(
            point_normals=True,
            cell_normals=False,
            consistent_normals=bool(consistent_normals),
            auto_orient_normals=bool(auto_orient_normals),
            flip_normals=bool(flip_normals),
            inplace=False,
        )
    except TypeError:
        surf_n = surf.compute_normals(point_normals=True, cell_normals=False, inplace=False)
    except Exception as exc:
        raise NodeExecutionError(f"Could not compute mesh normals for thickening: {exc}") from exc

    normals = None
    for key in ["Normals", "PointNormals"]:
        try:
            if key in surf_n.point_data:
                normals = np.asarray(surf_n.point_data[key], dtype=float)
                break
        except Exception:
            pass
    if normals is None or normals.shape[0] != int(surf_n.n_points):
        raise NodeExecutionError("Could not obtain point normals from the input mesh.")

    lens = np.linalg.norm(normals, axis=1)
    lens[lens == 0] = 1.0
    normals = normals / lens[:, None]

    pts = np.asarray(surf_n.points, dtype=float)
    mode = str(mode or "symmetric").strip().lower()
    if mode in {"symmetric", "both", "centered"}:
        inner_pts = pts - normals * (d * 0.5)
        outer_pts = pts + normals * (d * 0.5)
    elif mode in {"outward", "positive", "one_sided_outward"}:
        inner_pts = pts.copy()
        outer_pts = pts + normals * d
    elif mode in {"inward", "negative", "one_sided_inward"}:
        inner_pts = pts - normals * d
        outer_pts = pts.copy()
    else:
        raise NodeExecutionError("Thicken mode must be symmetric, outward, or inward.")

    faces = _polydata_face_lists(surf_n)
    if not faces:
        raise NodeExecutionError("Could not parse polygon faces from the input surface mesh.")

    n_pts = int(surf_n.n_points)
    n_faces = len(faces)

    # Inner surface normals should point opposite to outer surface normals for a shell.
    inner_faces = [[int(v) for v in reversed(face)] for face in faces]
    outer_faces = [[int(v) + n_pts for v in face] for face in faces]

    boundary_edges = _boundary_edges_with_owner(faces) if close_sides else []
    side_faces: List[List[int]] = []
    side_owner_indices: List[int] = []
    for a, b, owner in boundary_edges:
        side_faces.append([int(a), int(b), int(b) + n_pts, int(a) + n_pts])
        side_owner_indices.append(int(owner))

    all_points = np.vstack([inner_pts, outer_pts])
    all_faces = inner_faces + outer_faces + side_faces
    poly = pv.PolyData(all_points, _faces_to_pyvista_array(all_faces))

    # Preserve cell data. Original values are copied to inner and outer surfaces.
    # For side walls, the value of the original boundary-owner cell is used.
    for key, arr in getattr(surf_n, "cell_data", {}).items():
        combined_arr = _safe_cell_array_concat_for_thickening(arr, side_owner_indices, n_faces)
        if combined_arr is not None and combined_arr.shape[0] == poly.n_cells:
            try:
                poly.cell_data[str(key)] = combined_arr
            except Exception:
                pass

    # Preserve point data where possible by duplicating it for inner and outer points.
    for key, arr in getattr(surf_n, "point_data", {}).items():
        try:
            a = np.asarray(arr)
            if a.shape[0] == n_pts and str(key) not in {"Normals", "PointNormals"}:
                poly.point_data[str(key)] = np.concatenate([a, a], axis=0)
        except Exception:
            pass

    part = np.concatenate([
        np.full(n_faces, 0, dtype=np.int32),
        np.full(n_faces, 1, dtype=np.int32),
        np.full(len(side_faces), 2, dtype=np.int32),
    ])
    poly.cell_data["thickening_part"] = part
    poly.cell_data["buffer_distance"] = np.full(poly.n_cells, float(d), dtype=float)
    # Preserve the centre sheet and physical half-width through VTK save/load.
    # Distance voxelization must fill this band, not trace the two shell skins.
    poly.field_data["thickening_reference_points"] = (inner_pts + outer_pts) * 0.5
    poly.field_data["thickening_reference_faces"] = np.asarray(surf_n.faces, dtype=np.int64)
    poly.field_data["thickening_half_width"] = np.asarray([abs(d) * 0.5])

    if clean_output:
        try:
            poly = poly.clean(tolerance=0.0)
        except Exception:
            pass

    metadata = {
        "operation": "thicken_mesh_along_normals",
        "input_surface_cells": int(n_faces),
        "input_surface_points": int(n_pts),
        "output_cells": int(getattr(poly, "n_cells", 0)),
        "output_points": int(getattr(poly, "n_points", 0)),
        "buffer_distance": float(d),
        "mode": mode,
        "close_sides": bool(close_sides),
        "boundary_edges_closed": int(len(side_faces)),
        "triangulate_input": bool(triangulate),
        "cell_data": list(getattr(poly, "cell_data", {}).keys()),
        "point_data": list(getattr(poly, "point_data", {}).keys()),
        "bounds": [float(v) for v in getattr(poly, "bounds", [])],
        "note": "Output is a thickened PolyData shell surface. Boundary side faces are added for open surfaces when Close sides is enabled.",
    }
    return poly, metadata


class ThickenMeshNode(BaseNode):
    type_name = "ThickenMesh"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "mesh")
        base_mesh, base_name, base_meta = _mesh_from_runtime_value(rv)

        distance = _as_float(params.get("buffer_distance"), None)
        if distance is None:
            raise NodeExecutionError("Buffer distance is required.")

        mesh, meta = _build_normal_thickened_mesh(
            base_mesh,
            distance=float(distance),
            mode=str(params.get("mode") or "symmetric"),
            close_sides=_as_bool(params.get("close_sides"), True),
            triangulate=_as_bool(params.get("triangulate_input"), True),
            consistent_normals=_as_bool(params.get("consistent_normals"), True),
            auto_orient_normals=_as_bool(params.get("auto_orient_normals"), True),
            flip_normals=_as_bool(params.get("flip_normals"), False),
            clean_input=_as_bool(params.get("clean_input"), True),
            clean_output=_as_bool(params.get("clean_output"), True),
        )

        out_name = str(params.get("output_file_name") or "thickened_mesh.vtp")
        mesh_path, final_out_name, save_adjustment = _save_pyvista_mesh_compatible(mesh, out_name)
        record = register_output_file(mesh_path, display_name=final_out_name)

        scalar = _choose_mesh_scalar(mesh, str(params.get("mesh_scalars") or "auto"))
        preview = _mesh_preview(mesh, final_out_name, file_id=record["file_id"])
        preview.update({
            "preview_type": "mesh",
            "operation": "thicken_mesh",
            "base_mesh": base_meta,
            "base_mesh_name": base_name,
            "scalar_used": scalar,
            "saved_output_file": final_out_name,
            "output_extension_adjustment": save_adjustment,
            "mesh_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), True)).lower()}&scalars={scalar or ''}",
            "pyvista_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), True)).lower()}&scalars={scalar or ''}",
            "pyvista_button_label": "Open Thickened Mesh 3D Popup",
            "web_scalar": scalar,
            "web_show_edges": _as_bool(params.get("show_edges"), True),
            "web_viewer_label": "Thickened mesh",
            **meta,
        })
        _attach_web_surface_preview(
            preview,
            mesh,
            Path(final_out_name).stem,
            preferred_scalar=scalar or "",
            show_edges=_as_bool(params.get("show_edges"), True),
        )
        preview["inline_display"] = "web_3d_viewer"

        return {
            "mesh": RuntimeValue("mesh", mesh, name=final_out_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "file": RuntimeValue("file", mesh_path, name=final_out_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "report": RuntimeValue("report", preview, name="thicken_mesh_report", preview=preview, metadata=preview),
        }



class PyVistaClippedLayerViewerNode(BaseNode):
    type_name = "PyVistaClippedLayerViewer"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        shared_grid = _shared_grid_input(inputs)
        if shared_grid is not None and not _as_bool(params.get("crinkle"), True):
            raise NodeExecutionError("Shared-grid clipping requires crinkle=True to preserve whole voxel cells.")
        clip_file_id = params.get("clip_mesh_file_id") or ""
        top_id = params.get("topography_mesh_file_id") or ""
        clip_path, clip_file_id_for_preview, clip_input_preview = _resolve_optional_mesh_input(
            inputs, "clip_mesh", str(clip_file_id), "kadi_or_connected_clipping_shell"
        )
        top_path, top_id_for_preview, top_input_preview = _resolve_optional_mesh_input(
            inputs, "topography_mesh", str(top_id), "kadi_or_connected_topography_dem"
        )

        cleaned_styles = _clean_layer_styles(params.get("layer_styles_json"))
        cell_data_name = str(params.get("cell_data_name") or "id")
        clean_output_mesh = _as_bool(params.get("clean_output_mesh"), True)

        input_mesh_values = _many(inputs, "input_mesh")
        use_mesh_input = bool(input_mesh_values)

        if use_mesh_input:
            base_rv = input_mesh_values[0]
            base_mesh, base_name, base_meta = _mesh_from_runtime_value(base_rv)
            mesh, mesh_meta = _clip_pyvista_dataset_with_surfaces(
                base_mesh,
                clip_path=clip_path,
                top_path=top_path,
                invert=_as_bool(params.get("invert"), False),
                crinkle=_as_bool(params.get("crinkle"), True),
                topography_clip_enabled=_as_bool(params.get("topography_clip_enabled"), True),
                topography_invert=_as_bool(params.get("topography_invert"), False),
                crop_to_topography_xy=_as_bool(params.get("crop_to_topography_xy"), True),
                clean_output_mesh=clean_output_mesh,
            )
            mesh_meta["base_mesh"] = base_meta
            mesh_meta["base_mesh_name"] = base_name
            input_mode = "mesh"
            popup_url = None
            pyvista_button_label = "Open Clipped Mesh 3D Popup"
            pyvista_note = "This run clipped an existing mesh input, not a GeoModel. Use Visualization/PyVista popup for inspection."
        else:
            rv = _single(inputs, "geo_model")
            if rv.kind != "geo_model":
                raise NodeExecutionError(f"Clipping Tool expects either input_mesh=mesh or geo_model=geo_model, got {rv.kind}")

            mesh, mesh_meta = _build_clipped_gempy_layer_mesh(
                rv.value,
                clip_path=clip_path,
                top_path=top_path,
                cell_data_name=cell_data_name,
                layer_styles=cleaned_styles,
                invert=_as_bool(params.get("invert"), False),
                crinkle=_as_bool(params.get("crinkle"), True),
                topography_clip_enabled=_as_bool(params.get("topography_clip_enabled"), True),
                topography_invert=_as_bool(params.get("topography_invert"), False),
                crop_to_topography_xy=_as_bool(params.get("crop_to_topography_xy"), True),
                clean_output_mesh=clean_output_mesh,
            )
            input_mode = "geo_model"
            token = put_runtime_object(rv.value, kind="geo_model", name=rv.name)
            query = urlencode({
                "clip_file_id": str(clip_file_id_for_preview),
                "topography_file_id": str(top_id_for_preview),
                "cell_data_name": cell_data_name,
                "layer_styles": json.dumps(cleaned_styles),
                "invert": str(_as_bool(params.get("invert"), False)).lower(),
                "crinkle": str(_as_bool(params.get("crinkle"), True)).lower(),
                "topography_clip_enabled": str(_as_bool(params.get("topography_clip_enabled"), True)).lower(),
                "topography_invert": str(_as_bool(params.get("topography_invert"), False)).lower(),
                "crop_to_topography_xy": str(_as_bool(params.get("crop_to_topography_xy"), True)).lower(),
                "show_edges": str(_as_bool(params.get("show_edges"), False)).lower(),
                "show_base_gempy": str(_as_bool(params.get("show_base_gempy"), False)).lower(),
                "show_clip_mesh": str(_as_bool(params.get("show_clip_mesh"), False)).lower(),
                "show_topography_mesh": str(_as_bool(params.get("show_topography_mesh"), False)).lower(),
            })
            popup_url = f"/api/pyvista/gempy-clipped-layers/{token}?{query}"
            pyvista_button_label = "Open Clipped Voxel/Layers 3D Popup"
            pyvista_note = "DEM/topography and shell meshes are optional. DEM is used for clipping by default and is not shown unless 'Show DEM/topography mesh' is enabled."

        if shared_grid is not None:
            centers = np.asarray(mesh.cell_centers().points, dtype=float)
            if not centers_are_aligned(centers, shared_grid) or not voxel_cells_are_aligned(mesh, shared_grid):
                raise NodeExecutionError("Clipped cells are not regular voxels on the connected shared grid. Check GemPy extent/resolution and keep crinkle enabled.")
            mesh_meta["shared_grid"] = shared_grid
            mesh_meta["grid_aligned"] = True

        # If a mesh input is used, prefer its best scalar. If a GeoModel is used,
        # keep the configured cell_data_name.
        if use_mesh_input and (not cell_data_name or cell_data_name not in getattr(mesh, "cell_data", {})):
            scalar_for_preview = _choose_mesh_scalar(mesh, str(params.get("mesh_scalars") or "auto"))
        else:
            scalar_for_preview = cell_data_name if cell_data_name in getattr(mesh, "cell_data", {}) else _choose_mesh_scalar(mesh, "auto")

        out_name = str(params.get("output_file_name") or ("clipped_mesh.vtu" if use_mesh_input else "clipped_gempy_layers.vtu"))
        mesh_path, final_out_name, save_adjustment = _save_pyvista_mesh_compatible(mesh, out_name)
        record = register_output_file(mesh_path, display_name=final_out_name)

        preview = {
            "plot_type": "PyVista clipping tool",
            "input_mode": input_mode,
            "clipping_mesh": str(clip_path.name) if clip_path else None,
            "topography_mesh": str(top_path.name) if top_path else None,
            "clipping_mesh_source": "input port" if inputs.get("clip_mesh") is not None else ("file selector" if clip_path else None),
            "topography_mesh_source": "input port" if inputs.get("topography_mesh") is not None else ("file selector" if top_path else None),
            "clipping_mesh_input_preview": clip_input_preview,
            "topography_mesh_input_preview": top_input_preview,
            "topography_used_for_clipping": bool(top_path and _as_bool(params.get("topography_clip_enabled"), True)),
            "topography_mesh_displayed": _as_bool(params.get("show_topography_mesh"), False),
            "cell_data_name": cell_data_name,
            "scalar_used": scalar_for_preview,
            "layer_styles": cleaned_styles if input_mode == "geo_model" else [],
            "mesh_file_id": record["file_id"],
            "download_url": f"/api/download/{record['file_id']}",
            "mesh_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), False)).lower()}&scalars={scalar_for_preview or ''}",
            "web_scalar": scalar_for_preview,
            "web_show_edges": _as_bool(params.get("show_edges"), False),
            "web_viewer_label": "Clipped mesh" if use_mesh_input else "Clipped layer mesh",
            "pyvista_preview_url": popup_url or f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), False)).lower()}&scalars={scalar_for_preview or ''}",
            "pyvista_button_label": pyvista_button_label,
            "pyvista_note": pyvista_note,
            "saved_output_file": final_out_name,
            "output_extension_adjustment": save_adjustment,
            **mesh_meta,
        }
        _attach_web_surface_preview(
            preview,
            mesh,
            Path(final_out_name).stem,
            preferred_scalar=scalar_for_preview or "",
            show_edges=_as_bool(params.get("show_edges"), False),
        )
        preview["web_viewer_label"] = "Clipped mesh" if use_mesh_input else "Clipped layer mesh"

        return {
            "report": RuntimeValue(
                "report",
                preview,
                name="clipping_tool",
                preview=preview,
            ),
            "mesh": RuntimeValue(
                "mesh",
                mesh,
                name=final_out_name,
                preview=preview,
                metadata={
                    "file_id": record["file_id"],
                    **preview,
                },
            ),
            "file": RuntimeValue(
                "file",
                Path(record["path"]),
                name=final_out_name,
                preview=preview,
                metadata={
                    "file_id": record["file_id"],
                    **preview,
                },
            ),
        }




class SetFiniteFaultNode(BaseNode):
    type_name = "SetFiniteFault"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"SetFiniteFault expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        rows = _parse_json(params.get("finite_faults_json"), [])
        if not isinstance(rows, list):
            raise NodeExecutionError("Finite fault settings must be a list created by the finite-fault editor.")

        clear_existing = _as_bool(params.get("clear_existing"), False)
        if clear_existing:
            for group in getattr(geo_model.structural_frame, "structural_groups", []):
                try:
                    group.faults_input_data = None
                except Exception:
                    pass

        applied = []
        skipped = []
        groups = list(getattr(geo_model.structural_frame, "structural_groups", []))
        name_to_idx = {str(getattr(g, "name", f"group_{i}")): i for i, g in enumerate(groups)}

        def _vec3(value: Any, default: Sequence[float], label: str) -> np.ndarray:
            vals = _parse_number_list(value)
            if not vals:
                vals = list(default)
            if len(vals) == 1:
                vals = vals * 3
            if len(vals) != 3:
                raise NodeExecutionError(f"{label} must contain one value or three values.")
            return np.asarray(vals, dtype=float)

        for row_idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if not _as_bool(row.get("enabled"), True):
                skipped.append({"row": row_idx, "reason": "disabled"})
                continue

            group_name = str(row.get("group_name") or "").strip()
            group_index_raw = row.get("group_index")
            group_index = None
            if group_name and group_name in name_to_idx:
                group_index = name_to_idx[group_name]
            elif group_index_raw not in (None, ""):
                group_index = _as_int(group_index_raw, None)

            if group_index is None or group_index < 0 or group_index >= len(groups):
                skipped.append({"row": row_idx, "reason": "group not found", "group_name": group_name, "group_index": group_index_raw})
                continue

            center = _vec3(row.get("center"), [0.0, 0.0, 0.0], "center")
            radius = _vec3(row.get("radius"), [1.0, 1.0, 1.0], "radius")
            max_slope = _vec3(row.get("max_slope"), [1.0, 1.0, 1.0], "max_slope")
            transform_position = _vec3(row.get("transform_position"), [0.0, 0.0, 0.0], "transform_position")
            transform_rotation = _vec3(row.get("transform_rotation"), [0.0, 0.0, 0.0], "transform_rotation")
            transform_scale = _vec3(row.get("transform_scale"), [1.0, 1.0, 1.0], "transform_scale")

            scaled_center = geo_model.input_transform.apply(center.reshape(1, -1))[0]
            scaled_radius = geo_model.input_transform.scale_points(radius.reshape(1, -1))[0]

            try:
                scalar_function = gp.implicit_functions.ellipsoid_3d_factory(
                    center=scaled_center,
                    radius=scaled_radius,
                    max_slope=max_slope,
                )
            except Exception as exc:
                raise NodeExecutionError(f"Could not create finite-fault ellipsoid for row {row_idx}: {exc}") from exc

            try:
                transform = gp.data.Transform(
                    position=transform_position,
                    rotation=transform_rotation,
                    scale=transform_scale,
                )
                ffd = gp.data.FiniteFaultData(
                    implicit_function=scalar_function,
                    implicit_function_transform=transform,
                    pivot=scaled_center,
                )
                faults_data = gp.data.FaultsData(
                    fault_values_everywhere=np.zeros(0),
                    fault_values_on_sp=np.zeros(0),
                    thickness=None,
                    fault_values_ref=np.zeros(0),
                    fault_values_rest=np.zeros(0),
                    finite_fault_data=ffd,
                )
                groups[group_index].faults_input_data = faults_data
            except Exception as exc:
                raise NodeExecutionError(f"Could not assign finite-fault data to group {group_index}: {exc}") from exc

            applied.append({
                "row": row_idx,
                "group_index": int(group_index),
                "group_name": str(getattr(groups[group_index], "name", group_name or group_index)),
                "center": center.tolist(),
                "radius": radius.tolist(),
                "max_slope": max_slope.tolist(),
                "transform_position": transform_position.tolist(),
                "transform_rotation": transform_rotation.tolist(),
                "transform_scale": transform_scale.tolist(),
            })

        preview = {
            "finite_faults_applied": applied,
            "skipped": skipped,
            "clear_existing": clear_existing,
            "structural_groups": _structural_group_summary(geo_model),
            "note": "Finite fault data is assigned to structural_group.faults_input_data. It affects GemPy computation but is not displayed as a separate fault plane in the voxel viewer.",
        }
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview)}


class AutoOrientationsNode(BaseNode):
    type_name = "AutoOrientations"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"AutoOrientations expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        names = [x.strip() for x in str(params.get("element_names", "")).split(",") if x.strip()]
        applied = []
        for name in names:
            element = geo_model.structural_frame.get_element_by_name(name)
            new_orientations = gp.create_orientations_from_surface_points_coords(xyz_coords=element.surface_points.xyz)
            gp.add_orientations(
                geo_model=geo_model,
                x=new_orientations.data["X"],
                y=new_orientations.data["Y"],
                z=new_orientations.data["Z"],
                pole_vector=new_orientations.grads,
                elements_names=name,
            )
            applied.append(name)
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview={"auto_orientations_for": applied})}



class AddOrientationsNode(BaseNode):
    type_name = "AddOrientations"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"AddOrientations expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        rows: List[Dict[str, Any]] = []
        table_rv = inputs.get("orientations_table")
        if isinstance(table_rv, list):
            table_rv = table_rv[0] if table_rv else None

        if table_rv is not None:
            if table_rv.kind != "table":
                raise NodeExecutionError(f"orientations_table input must be a table, got {table_rv.kind}")
            df = table_rv.value.copy()
            cols = _find_xyz_columns(df)
            if not cols:
                raise NodeExecutionError("orientations_table needs X, Y, Z columns.")
            formation_col = "formation" if "formation" in df.columns else "surface" if "surface" in df.columns else "element" if "element" in df.columns else None
            if not formation_col:
                raise NodeExecutionError("orientations_table needs a formation/surface/element column.")
            for c in [cols["X"], cols["Y"], cols["Z"]]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df[formation_col] = _normalize_formation_series(df[formation_col])
            df = df.dropna(subset=[cols["X"], cols["Y"], cols["Z"], formation_col])
            vectors, vector_source = _orientation_vectors_from_df(df)
            df["_gx"] = vectors[:, 0]
            df["_gy"] = vectors[:, 1]
            df["_gz"] = vectors[:, 2]
            for formation, group in df.groupby(formation_col, dropna=True):
                xyz = group[[cols["X"], cols["Y"], cols["Z"]]].to_numpy(dtype=float)
                vec = group[["_gx", "_gy", "_gz"]].to_numpy(dtype=float)
                self._add_orientations(gp, geo_model, xyz, vec, str(formation))
                rows.append({"source": "table", "element": str(formation), "count": int(len(group)), "vector_source": vector_source})

        manual = _parse_json(params.get("orientations_json"), [])
        if not isinstance(manual, list):
            raise NodeExecutionError("orientations_json must be a list.")
        if manual:
            df = pd.DataFrame(manual)
            if not df.empty:
                required = ["x", "y", "z", "element"]
                missing = [c for c in required if c not in df.columns]
                if missing:
                    raise NodeExecutionError(f"Manual orientations are missing columns: {missing}")
                df = df.rename(columns={"x": "X", "y": "Y", "z": "Z", "element": "formation", "gx": "G_x", "gy": "G_y", "gz": "G_z"})
                for c in ["X", "Y", "Z"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df["formation"] = _normalize_formation_series(df["formation"])
                df = df.dropna(subset=["X", "Y", "Z", "formation"])
                vectors, vector_source = _orientation_vectors_from_df(df)
                df["_gx"] = vectors[:, 0]
                df["_gy"] = vectors[:, 1]
                df["_gz"] = vectors[:, 2]
                for formation, group in df.groupby("formation", dropna=True):
                    xyz = group[["X", "Y", "Z"]].to_numpy(dtype=float)
                    vec = group[["_gx", "_gy", "_gz"]].to_numpy(dtype=float)
                    self._add_orientations(gp, geo_model, xyz, vec, str(formation))
                    rows.append({"source": "manual", "element": str(formation), "count": int(len(group)), "vector_source": vector_source})

        report = {"added_orientation_groups": rows, "total_added_orientations": int(sum(item["count"] for item in rows))}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report)}

    @staticmethod
    def _add_orientations(gp: Any, geo_model: Any, xyz: np.ndarray, vectors: np.ndarray, element: str) -> None:
        if xyz.shape[0] == 0:
            return
        _call_gempy_candidates([
            ("add_orientations(pole_vector,list_names)", lambda: gp.add_orientations(
                geo_model=geo_model,
                x=xyz[:, 0].tolist(),
                y=xyz[:, 1].tolist(),
                z=xyz[:, 2].tolist(),
                pole_vector=vectors,
                elements_names=[element] * xyz.shape[0],
            )),
            ("add_orientations(pole_vector,single_name)", lambda: gp.add_orientations(
                geo_model=geo_model,
                x=xyz[:, 0].tolist(),
                y=xyz[:, 1].tolist(),
                z=xyz[:, 2].tolist(),
                pole_vector=vectors,
                elements_names=element,
            )),
            ("add_orientations(G_x,G_y,G_z)", lambda: gp.add_orientations(
                geo_model=geo_model,
                x=xyz[:, 0].tolist(),
                y=xyz[:, 1].tolist(),
                z=xyz[:, 2].tolist(),
                G_x=vectors[:, 0].tolist(),
                G_y=vectors[:, 1].tolist(),
                G_z=vectors[:, 2].tolist(),
                elements_names=[element] * xyz.shape[0],
            )),
        ], f"GemPy add_orientations failed for element {element}.")


class ModifySurfacePointsNode(BaseNode):
    type_name = "ModifySurfacePoints"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"ModifySurfacePoints expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        rows = _parse_json(params.get("modifications_json"), [])
        if not rows:
            rows = [{
                "indices": params.get("indices"),
                "X": params.get("X"),
                "Y": params.get("Y"),
                "Z": params.get("Z"),
                "element": params.get("element"),
                "nugget": params.get("nugget"),
            }]
        if not isinstance(rows, list):
            raise NodeExecutionError("modifications_json must be a list.")

        applied = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            indices = _indices_from_param(row.get("indices", row.get("index")))
            if not indices:
                continue
            kwargs: Dict[str, Any] = {"geo_model": geo_model, "indices": indices}
            for col in ["X", "Y", "Z"]:
                value = row.get(col, row.get(col.lower()))
                if value not in (None, ""):
                    kwargs[col] = float(value)
            element = row.get("element", row.get("formation", row.get("surface")))
            if element not in (None, ""):
                kwargs["elements_names"] = str(element)
            nugget = row.get("nugget")
            if nugget not in (None, ""):
                kwargs["nugget"] = float(nugget)

            _call_gempy_candidates([
                ("modify_surface_points(indices)", lambda kw=kwargs: gp.modify_surface_points(**kw)),
                ("modify_surface_points(index)", lambda kw=kwargs: gp.modify_surface_points(**{**{k: v for k, v in kw.items() if k != "indices"}, "index": kw["indices"]})),
                ("modify_surface_points(positional)", lambda kw=kwargs: gp.modify_surface_points(geo_model, kw["indices"])),
            ], "GemPy modify_surface_points failed.")
            applied.append({"indices": indices, "fields": [k for k in kwargs.keys() if k not in {"geo_model", "indices"}]})

        report = {"modified_surface_points": applied, "count_groups": len(applied)}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report)}


class ModifyOrientationsNode(BaseNode):
    type_name = "ModifyOrientations"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"ModifyOrientations expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        rows = _parse_json(params.get("modifications_json"), [])
        if not rows:
            rows = [{
                "indices": params.get("indices"),
                "X": params.get("X"),
                "Y": params.get("Y"),
                "Z": params.get("Z"),
                "element": params.get("element"),
                "G_x": params.get("G_x"),
                "G_y": params.get("G_y"),
                "G_z": params.get("G_z"),
                "nugget": params.get("nugget"),
            }]
        if not isinstance(rows, list):
            raise NodeExecutionError("modifications_json must be a list.")

        applied = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            indices = _indices_from_param(row.get("indices", row.get("index")))
            if not indices:
                continue
            kwargs: Dict[str, Any] = {"geo_model": geo_model, "indices": indices}
            for col in ["X", "Y", "Z"]:
                value = row.get(col, row.get(col.lower()))
                if value not in (None, ""):
                    kwargs[col] = float(value)
            element = row.get("element", row.get("formation", row.get("surface")))
            if element not in (None, ""):
                kwargs["elements_names"] = str(element)

            gx = row.get("G_x", row.get("gx"))
            gy = row.get("G_y", row.get("gy"))
            gz = row.get("G_z", row.get("gz"))
            if gx not in (None, "") and gy not in (None, "") and gz not in (None, ""):
                kwargs["pole_vector"] = np.asarray([[float(gx), float(gy), float(gz)]], dtype=float)

            nugget = row.get("nugget")
            if nugget not in (None, ""):
                kwargs["nugget"] = float(nugget)

            _call_gempy_candidates([
                ("modify_orientations(indices)", lambda kw=kwargs: gp.modify_orientations(**kw)),
                ("modify_orientations(index)", lambda kw=kwargs: gp.modify_orientations(**{**{k: v for k, v in kw.items() if k != "indices"}, "index": kw["indices"]})),
                ("modify_orientations(positional)", lambda kw=kwargs: gp.modify_orientations(geo_model, kw["indices"])),
            ], "GemPy modify_orientations failed.")
            applied.append({"indices": indices, "fields": [k for k in kwargs.keys() if k not in {"geo_model", "indices"}]})

        report = {"modified_orientations": applied, "count_groups": len(applied)}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report)}


class DeleteSurfacePointsNode(BaseNode):
    type_name = "DeleteSurfacePoints"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"DeleteSurfacePoints expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        indices = _indices_from_param(params.get("indices"))
        if not indices:
            raise NodeExecutionError("DeleteSurfacePoints requires indices, e.g. 0,1,2.")

        _call_gempy_candidates([
            ("delete_surface_points(indices)", lambda: gp.delete_surface_points(geo_model=geo_model, indices=indices)),
            ("delete_surface_points(index)", lambda: gp.delete_surface_points(geo_model=geo_model, index=indices)),
            ("delete_surface_points(positional)", lambda: gp.delete_surface_points(geo_model, indices)),
        ], "GemPy delete_surface_points failed.")
        report = {"deleted_surface_point_indices": indices}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report)}


class DeleteOrientationsNode(BaseNode):
    type_name = "DeleteOrientations"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"DeleteOrientations expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        indices = _indices_from_param(params.get("indices"))
        if not indices:
            raise NodeExecutionError("DeleteOrientations requires indices, e.g. 0,1,2.")

        _call_gempy_candidates([
            ("delete_orientations(indices)", lambda: gp.delete_orientations(geo_model=geo_model, indices=indices)),
            ("delete_orientations(index)", lambda: gp.delete_orientations(geo_model=geo_model, index=indices)),
            ("delete_orientations(positional)", lambda: gp.delete_orientations(geo_model, indices)),
        ], "GemPy delete_orientations failed.")
        report = {"deleted_orientation_indices": indices}
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report)}



class SetTopographyNode(BaseNode):
    type_name = "SetTopography"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"SetTopography expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        mode = str(params.get("mode") or "file").strip().lower()

        operations: List[str] = []
        source_info: Dict[str, Any] = {"mode": mode}

        if mode == "preview":
            preview = _topography_preview(geo_model, label="topography")
            preview["operation"] = "preview_only"
            return {
                "geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview),
                "report": RuntimeValue("report", preview, name="topography_preview", preview=preview, metadata=preview),
            }

        if mode == "file":
            connected_file = inputs.get("topography_file")
            if isinstance(connected_file, list):
                connected_file = connected_file[0] if connected_file else None
            filepath = str(params.get("filepath") or "").strip()
            file_id = str(params.get("topography_file_id") or "").strip()
            if connected_file is not None:
                if connected_file.kind != "file":
                    raise NodeExecutionError(f"topography_file input must be a file, got {connected_file.kind}")
                filepath = str(connected_file.value)
            elif file_id:
                filepath = str(get_file_path(file_id))
            if not filepath:
                raise NodeExecutionError("SetTopography in file mode requires a connected file, uploaded topography file, or local filepath.")

            _call_gempy_candidates([
                ("set_topography_from_file(grid, filepath)", lambda: gp.set_topography_from_file(grid=geo_model.grid, filepath=str(filepath))),
                ("set_topography_from_file(grid, source)", lambda: gp.set_topography_from_file(grid=geo_model.grid, source=str(filepath))),
                ("set_topography_from_file(positional)", lambda: gp.set_topography_from_file(geo_model.grid, str(filepath))),
            ], "GemPy set_topography_from_file failed.")
            operations.append(f"set_topography_from_file({filepath})")
            source_info["filepath"] = filepath

        elif mode == "arrays":
            table_rv = inputs.get("topography_table")
            if isinstance(table_rv, list):
                table_rv = table_rv[0] if table_rv else None

            if table_rv is not None:
                if table_rv.kind != "table":
                    raise NodeExecutionError(f"topography_table input must be a table, got {table_rv.kind}")
                xyz = _xyz_from_table(table_rv.value)
                source_info["source"] = "table"
            else:
                xyz = _parse_xyz_points(params.get("topography_points_json"))
                source_info["source"] = "topography_points_json"

            _call_gempy_candidates([
                ("set_topography_from_arrays(xyz_vertices)", lambda: gp.set_topography_from_arrays(grid=geo_model.grid, xyz_vertices=xyz)),
                ("set_topography_from_arrays(xyz_coords)", lambda: gp.set_topography_from_arrays(grid=geo_model.grid, xyz_coords=xyz)),
                ("set_topography_from_arrays(values)", lambda: gp.set_topography_from_arrays(grid=geo_model.grid, values=xyz)),
                ("set_topography_from_arrays(x,y,z)", lambda: gp.set_topography_from_arrays(grid=geo_model.grid, x=xyz[:, 0], y=xyz[:, 1], z=xyz[:, 2])),
                ("set_topography_from_arrays(positional)", lambda: gp.set_topography_from_arrays(geo_model.grid, xyz)),
            ], "GemPy set_topography_from_arrays failed.")
            operations.append(f"set_topography_from_arrays(n={xyz.shape[0]})")
            source_info["n_points"] = int(xyz.shape[0])

        elif mode == "random":
            extent = _geo_model_extent_values(geo_model)
            zmin, zmax = float(extent[4]), float(extent[5])
            z_span = zmax - zmin
            z_fraction_min = _as_float(params.get("random_z_fraction_min"), 0.6)
            z_fraction_max = _as_float(params.get("random_z_fraction_max"), 1.0)
            dz = [
                zmin + z_span * float(z_fraction_min),
                zmin + z_span * float(z_fraction_max),
            ]
            # Keep random topography inside the current model z-range.
            dz[0] = max(zmin, min(zmax, dz[0]))
            dz[1] = max(zmin, min(zmax, dz[1]))
            if dz[0] > dz[1]:
                dz = [dz[1], dz[0]]

            fractal_dimension = _as_float(params.get("fractal_dimension"), 2.0)
            topo_res = _parse_json(params.get("topography_resolution"), None)
            if topo_res is not None:
                topo_res = np.asarray(topo_res, dtype=int)

            _call_gempy_candidates([
                ("set_topography_from_random(d_z,resolution,fractal)", lambda: gp.set_topography_from_random(grid=geo_model.grid, d_z=np.asarray(dz, dtype=float), topography_resolution=topo_res, fractal_dimension=float(fractal_dimension))),
                ("set_topography_from_random(d_z,resolution)", lambda: gp.set_topography_from_random(grid=geo_model.grid, d_z=np.asarray(dz, dtype=float), topography_resolution=topo_res)),
                ("set_topography_from_random(d_z)", lambda: gp.set_topography_from_random(grid=geo_model.grid, d_z=np.asarray(dz, dtype=float))),
                ("set_topography_from_random(positional)", lambda: gp.set_topography_from_random(geo_model.grid)),
            ], "GemPy set_topography_from_random failed.")
            operations.append(f"set_topography_from_random(d_z={dz})")
            source_info.update({"extent": extent, "d_z": dz, "fractal_dimension": fractal_dimension, "topography_resolution": topo_res.tolist() if topo_res is not None else None})

        else:
            raise NodeExecutionError("Topography mode must be one of: file, arrays, random, preview.")

        preview = _topography_preview(geo_model, label="topography")
        preview.update({"operations": operations, "source_info": source_info})
        return {
            "geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview),
            "report": RuntimeValue("report", preview, name="topography_report", preview=preview, metadata=preview),
        }


class SetGemPyGridNode(BaseNode):
    type_name = "SetGemPyGrid"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc

        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"SetGemPyGrid expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        mode = str(params.get("mode") or "activate").strip().lower()

        report: Dict[str, Any] = {"mode": mode, "operations": []}

        if mode == "section":
            name = str(params.get("section_name") or "section").strip()
            start_xy = _parse_json(params.get("section_start"), [0.0, 0.0])
            end_xy = _parse_json(params.get("section_end"), [1.0, 1.0])
            resolution = _parse_json(params.get("section_resolution"), [100, 80])
            section_dict = {name: ([float(start_xy[0]), float(start_xy[1])], [float(end_xy[0]), float(end_xy[1])], [int(resolution[0]), int(resolution[1])])}

            _call_gempy_candidates([
                ("set_section_grid(grid, section_dict)", lambda: gp.set_section_grid(grid=geo_model.grid, section_dict=section_dict)),
                ("set_section_grid(positional)", lambda: gp.set_section_grid(geo_model.grid, section_dict)),
                ("grid.set_section_grid", lambda: geo_model.grid.set_section_grid(section_dict)),
            ], "GemPy set_section_grid failed.")
            report["section_dict"] = section_dict
            report["operations"].append("set_section_grid")

        elif mode == "custom":
            table_rv = inputs.get("grid_points")
            if isinstance(table_rv, list):
                table_rv = table_rv[0] if table_rv else None
            if table_rv is not None:
                if table_rv.kind != "table":
                    raise NodeExecutionError(f"grid_points input must be a table, got {table_rv.kind}")
                xyz = _xyz_from_table(table_rv.value)
                source = "table"
            else:
                xyz = _parse_xyz_points(params.get("custom_points_json"))
                source = "custom_points_json"

            _call_gempy_candidates([
                ("set_custom_grid(xyz_coord)", lambda: gp.set_custom_grid(grid=geo_model.grid, xyz_coord=xyz)),
                ("set_custom_grid(xyz_coords)", lambda: gp.set_custom_grid(grid=geo_model.grid, xyz_coords=xyz)),
                ("set_custom_grid(values)", lambda: gp.set_custom_grid(grid=geo_model.grid, values=xyz)),
                ("set_custom_grid(positional)", lambda: gp.set_custom_grid(geo_model.grid, xyz)),
            ], "GemPy set_custom_grid failed.")
            report.update({"source": source, "n_points": int(xyz.shape[0])})
            report["operations"].append("set_custom_grid")

        elif mode == "centered":
            table_rv = inputs.get("grid_points")
            if isinstance(table_rv, list):
                table_rv = table_rv[0] if table_rv else None
            if table_rv is not None:
                if table_rv.kind != "table":
                    raise NodeExecutionError(f"grid_points input must be a table, got {table_rv.kind}")
                centers = _xyz_from_table(table_rv.value)
                source = "table"
            else:
                centers = _parse_xyz_points(params.get("centers_json"))
                source = "centers_json"

            radius = _parse_json(params.get("centered_radius"), [100.0, 100.0, 100.0])
            resolution = _parse_json(params.get("centered_resolution"), [10, 10, 10])
            _call_gempy_candidates([
                ("set_centered_grid(grid, centers, radius, resolution)", lambda: gp.set_centered_grid(grid=geo_model.grid, centers=centers, radius=radius, resolution=resolution)),
                ("set_centered_grid(centered_grid_centers)", lambda: gp.set_centered_grid(grid=geo_model.grid, centers=centers, radius=radius, resolution=resolution)),
                ("set_centered_grid(positional)", lambda: gp.set_centered_grid(geo_model.grid, centers, radius, resolution)),
            ], "GemPy set_centered_grid failed.")
            report.update({"source": source, "n_centers": int(centers.shape[0]), "radius": radius, "resolution": resolution})
            report["operations"].append("set_centered_grid")

        elif mode == "activate":
            grid_types = [x.strip() for x in str(params.get("active_grids") or "regular").replace(";", ",").split(",") if x.strip()]
            reset = _as_bool(params.get("reset_active_grids"), True)
            _call_gempy_candidates([
                ("set_active_grid(grid_type, reset)", lambda: gp.set_active_grid(grid=geo_model.grid, grid_type=grid_types, reset=reset)),
                ("set_active_grid(grid_type)", lambda: gp.set_active_grid(grid=geo_model.grid, grid_type=grid_types)),
                ("set_active_grid(positional)", lambda: gp.set_active_grid(geo_model.grid, grid_types)),
            ], "GemPy set_active_grid failed.")
            report.update({"active_grids": grid_types, "reset": reset})
            report["operations"].append("set_active_grid")
        else:
            raise NodeExecutionError("Grid mode must be one of: section, custom, centered, activate.")

        try:
            report["active_grids_after"] = str(getattr(geo_model.grid, "active_grids", ""))
        except Exception:
            pass
        return {
            "geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=report),
            "report": RuntimeValue("report", report, name="grid_report", preview=report, metadata=report),
        }




class SetGemPyOptionsNode(BaseNode):
    type_name = "SetGemPyOptions"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"SetGemPyOptions expects geo_model input, got {rv.kind}")
        geo_model = rv.value
        opts = geo_model.interpolation_options
        number_octree = _as_int(params.get("number_octree_levels_surface"), None)
        if number_octree is not None:
            opts.number_octree_levels_surface = number_octree
        kernel_range = _as_float(params.get("kernel_range"), None)
        if kernel_range is not None:
            opts.kernel_options.range = kernel_range
        octree_error = _as_float(params.get("octree_error_threshold"), None)
        if octree_error is not None:
            opts.evaluation_options.octree_error_threshold = octree_error
        chunk_size = _as_int(params.get("evaluation_chunk_size"), None)
        if chunk_size is not None:
            opts.evaluation_options.evaluation_chunk_size = chunk_size
        verbose_param = str(params.get("verbose") or "unchanged").strip().lower()
        if verbose_param not in {"", "unchanged"}:
            opts.evaluation_options.verbose = verbose_param == "true"

        condition_param = str(params.get("compute_condition_number") or "unchanged").strip().lower()
        if condition_param not in {"", "unchanged"}:
            opts.kernel_options.compute_condition_number = condition_param == "true"

        uni_degree = _as_int(params.get("uni_degree"), None)
        if uni_degree is not None:
            opts.kernel_options.uni_degree = uni_degree

        mesh_param = str(params.get("mesh_extraction") or "unchanged").strip().lower()
        if mesh_param not in {"", "unchanged"}:
            opts.mesh_extraction = mesh_param == "true"

        kernel_function = str(params.get("kernel_function") or "").strip()
        if kernel_function:
            try:
                from gempy_engine.core.data.kernel_classes.kernel_functions import AvailableKernelFunctions
                opts.kernel_function = getattr(AvailableKernelFunctions, str(kernel_function))
            except Exception as exc:
                raise NodeExecutionError(f"Cannot set kernel_function={kernel_function}: {exc}") from exc
        preview = {
            "message": "Only non-empty/non-unchanged fields were applied. Blank fields keep GemPy internal defaults.",
            "number_octree_levels_surface": getattr(opts, "number_octree_levels_surface", None),
            "kernel_range": getattr(opts.kernel_options, "range", None),
            "octree_error_threshold": getattr(opts.evaluation_options, "octree_error_threshold", None),
            "evaluation_chunk_size": getattr(opts.evaluation_options, "evaluation_chunk_size", None),
            "verbose": getattr(opts.evaluation_options, "verbose", None),
            "compute_condition_number": getattr(opts.kernel_options, "compute_condition_number", None),
            "uni_degree": getattr(opts.kernel_options, "uni_degree", None),
            "mesh_extraction": getattr(opts, "mesh_extraction", None),
            "kernel_function": str(getattr(opts, "kernel_function", "")),
        }
        return {"geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview=preview)}


class ComputeGemPyModelNode(BaseNode):
    type_name = "ComputeGemPyModel"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import gempy as gp
            _patch_gempy_stringarray_compat()
        except Exception as exc:
            raise NodeExecutionError("GemPy is not installed/importable. Install requirements-gempy.txt to run this node.") from exc
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"ComputeGemPyModel expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        backend_name = params.get("backend") or "PYTORCH"
        dtype = params.get("dtype") or "float64"
        backend = None
        for candidate in [backend_name, str(backend_name).upper(), str(backend_name).lower()]:
            try:
                backend = getattr(gp.data.AvailableBackends, candidate)
                break
            except Exception:
                pass
        if backend is None:
            raise NodeExecutionError(f"Unknown GemPy backend: {backend_name}")

        # GemPy's dual-contouring surface mesh extraction can fail for some
        # structural-frame configurations, especially when ONLAP/FAULT groups
        # produce a scalar-field/mask mismatch. The geological block model and
        # raw arrays can still be computed, so the editor exposes mesh_extraction
        # explicitly and can automatically retry without it. This avoids the
        # opaque engine error: IndexError: list index out of range in
        # dual_contouring/mask_generation.
        opts = getattr(geo_model, "interpolation_options", None)
        requested_mesh_extraction = _as_bool(params.get("mesh_extraction"), False)
        retry_without_mesh_extraction = _as_bool(params.get("retry_without_mesh_extraction"), True)
        if opts is not None and hasattr(opts, "mesh_extraction"):
            try:
                opts.mesh_extraction = requested_mesh_extraction
            except Exception:
                pass

        def _group_summary() -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            try:
                for i, group in enumerate(geo_model.structural_frame.structural_groups):
                    elements = []
                    try:
                        elements = [getattr(el, "name", str(el)) for el in group.elements]
                    except Exception:
                        pass
                    relation = getattr(group, "structural_relation", None)
                    out.append({
                        "index": i,
                        "name": getattr(group, "name", str(group)),
                        "relation": getattr(relation, "name", str(relation)),
                        "elements": elements,
                    })
            except Exception:
                pass
            return out

        def _compute_once():
            return gp.compute_model(
                geo_model,
                engine_config=gp.data.GemPyEngineConfig(backend=backend, dtype=dtype),
            )

        used_mesh_extraction = requested_mesh_extraction
        retry_note = ""
        try:
            solution = _compute_once()
        except Exception as exc:
            message = str(exc)
            can_retry = (
                requested_mesh_extraction
                and retry_without_mesh_extraction
                and ("list index out of range" in message or "dual_contouring" in message or "mask_generation" in message)
                and opts is not None
                and hasattr(opts, "mesh_extraction")
            )
            if not can_retry:
                groups = _group_summary()
                hint = (
                    "GemPy compute_model failed. If the traceback mentions dual_contouring/mask_generation "
                    "or 'list index out of range', disable 'Extract surface meshes' in the Compute GemPy Model node, "
                    "or set 'Retry without surface meshes' to True. This usually still computes lith_block/scalar arrays "
                    "for voxel viewing. Also check that Configure Structural Frame contains only existing formations and "
                    "that ONLAP is used only where needed."
                )
                raise NodeExecutionError(f"{hint}\n\nOriginal error: {message}\nStructural groups: {groups}") from exc
            try:
                opts.mesh_extraction = False
            except Exception:
                pass
            used_mesh_extraction = False
            retry_note = (
                "First compute failed during GemPy surface mesh extraction; "
                "retried automatically with mesh_extraction=False. "
                "Voxel/raw-array outputs are available, but GemPy extracted surface meshes may be missing."
            )
            try:
                solution = _compute_once()
            except Exception as exc2:
                groups = _group_summary()
                raise NodeExecutionError(
                    "GemPy compute_model failed even after retrying without surface mesh extraction. "
                    "Please check the structural frame order/relations, formation names, and orientation/surface-point data. "
                    f"Original retry error: {exc2}\nStructural groups: {groups}"
                ) from exc2

        preview = summarize_gempy_solution(solution)
        preview["mesh_extraction_used"] = bool(used_mesh_extraction)
        preview["retry_note"] = retry_note
        preview["structural_groups"] = _group_summary()
        return {
            "geo_model": RuntimeValue("geo_model", geo_model, name=rv.name, preview={"computed": True, "mesh_extraction_used": bool(used_mesh_extraction), "retry_note": retry_note}),
            "solution": RuntimeValue("gempy_solution", solution, name=f"solution_{rv.name}", preview=preview),
        }


class PlotGemPy2DNode(BaseNode):
    type_name = "PlotGemPy2D"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        geo_rv = inputs.get("geo_model")
        sol_rv = inputs.get("solution")
        if isinstance(geo_rv, list):
            geo_rv = geo_rv[0] if geo_rv else None
        if isinstance(sol_rv, list):
            sol_rv = sol_rv[0] if sol_rv else None
        if geo_rv is None and sol_rv is None:
            raise NodeExecutionError("PlotGemPy2D needs a geo_model input, or a solution input for the fallback array slice plot.")
        if geo_rv is not None and geo_rv.kind != "geo_model":
            raise NodeExecutionError(f"PlotGemPy2D expects geo_model input, got {geo_rv.kind}")
        if sol_rv is not None and sol_rv.kind != "gempy_solution":
            raise NodeExecutionError(f"PlotGemPy2D expects gempy_solution input, got {sol_rv.kind}")

        direction = str(params.get("direction") or "z").lower()
        cell_number = _as_int(params.get("cell_number"), None)
        show_data = _as_bool(params.get("show_data"), True)
        show_lith = _as_bool(params.get("show_lith"), True)
        show_boundaries = _as_bool(params.get("show_boundaries"), True)
        show_scalar = _as_bool(params.get("show_scalar"), False)
        output_name = params.get("file_name") or "gempy_2d_plot.png"
        if not str(output_name).lower().endswith(".png"):
            output_name = f"{output_name}.png"
        path = make_runtime_path(Path(str(output_name)).stem, ".png")

        used = "gempy_viewer.plot_2d"
        error_from_gpv = None
        try:
            if geo_rv is None:
                raise RuntimeError("No geo_model connected.")
            import matplotlib
            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
            import gempy_viewer as gpv

            # gpv.plot_2d has changed slightly across GemPy/GemPy-viewer versions.
            # Try a rich call first, then degrade to minimal arguments.
            kwargs = {
                "direction": direction,
                "show_data": show_data,
                "show_lith": show_lith,
                "show_boundaries": show_boundaries,
                "show_scalar": show_scalar,
            }
            if cell_number is not None:
                kwargs["cell_number"] = cell_number
            try:
                out = gpv.plot_2d(geo_rv.value, **kwargs)
            except TypeError:
                kwargs.pop("show_scalar", None)
                out = gpv.plot_2d(geo_rv.value, **kwargs)
            # Some versions return a GemPyPlot2D object with a figure; others
            # draw into the current matplotlib figure.
            fig = getattr(out, "fig", None) or plt.gcf()
            fig.savefig(path, dpi=int(_as_int(params.get("dpi"), 160)), bbox_inches="tight")
            plt.close(fig)
        except Exception as exc:
            error_from_gpv = str(exc)
            if sol_rv is None:
                raise NodeExecutionError(f"gempy_viewer 2D plot failed and no solution fallback was connected: {exc}") from exc
            used = "fallback raw array slice"
            self._plot_solution_slice(sol_rv.value, params, path)

        record = register_output_file(path, display_name=str(output_name))
        preview = {
            "plot_type": "GemPy 2D",
            "backend_used": used,
            "direction": direction,
            "cell_number": cell_number,
            "image_url": f"/api/raw/{record['file_id']}",
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": str(output_name),
        }
        if error_from_gpv:
            preview["gempy_viewer_fallback_reason"] = error_from_gpv
        return {"file": RuntimeValue("file", Path(record["path"]), name=str(output_name), preview=preview, metadata=preview)}

    def _plot_solution_slice(self, solution: Any, params: Dict[str, Any], path: Path) -> None:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        array_name = params.get("fallback_array") or "lith_block"
        arr = extract_array_from_solution(solution, str(array_name))
        reshape = _parse_json(params.get("reshape"), None)
        arr = np.asarray(arr)
        if reshape:
            arr = arr.reshape(tuple(int(x) for x in reshape))
        elif arr.ndim == 1:
            cube = round(arr.size ** (1 / 3))
            if cube ** 3 == arr.size:
                arr = arr.reshape((cube, cube, cube))
        if arr.ndim != 3:
            raise NodeExecutionError(f"Fallback 2D plotting needs a 3D array. {array_name} has shape {arr.shape}.")
        direction = str(params.get("direction") or "z").lower()
        axis = {"x": 0, "y": 1, "z": 2}.get(direction, 2)
        idx = _as_int(params.get("cell_number"), None)
        if idx is None:
            idx = arr.shape[axis] // 2
        idx = max(0, min(int(idx), arr.shape[axis] - 1))
        if axis == 0:
            sl = arr[idx, :, :]
        elif axis == 1:
            sl = arr[:, idx, :]
        else:
            sl = arr[:, :, idx]
        fig, ax = plt.subplots(figsize=(7, 6))
        im = ax.imshow(np.asarray(sl).T, origin="lower", aspect="auto")
        ax.set_title(f"{array_name} slice | direction={direction}, index={idx}")
        ax.set_xlabel("i")
        ax.set_ylabel("j")
        fig.colorbar(im, ax=ax, shrink=0.8)
        fig.savefig(path, dpi=int(_as_int(params.get("dpi"), 160)), bbox_inches="tight")
        plt.close(fig)



def _vtk_faces_from_element_faces(faces: Any) -> Optional[np.ndarray]:
    """Convert common GemPy face/triangle arrays to a VTK PolyData cell array."""
    if faces is None:
        return None
    try:
        arr = np.asarray(faces)
    except Exception:
        return None
    if arr.size == 0:
        return None

    if arr.ndim == 2:
        if arr.shape[1] == 3:
            return np.hstack([np.full((arr.shape[0], 1), 3, dtype=np.int64), arr.astype(np.int64)]).ravel()
        if arr.shape[1] == 4:
            # Either already VTK style [3, i, j, k] or quad ids.
            if np.all(arr[:, 0] == 3):
                return arr.astype(np.int64).ravel()
            return np.hstack([np.full((arr.shape[0], 1), 4, dtype=np.int64), arr.astype(np.int64)]).ravel()

    cells: List[int] = []
    try:
        for face in faces:
            ids = [int(v) for v in face]
            if len(ids) >= 3:
                cells.extend([len(ids), *ids])
    except Exception:
        return None

    return np.asarray(cells, dtype=np.int64) if cells else None


def _polydata_from_gempy_element(element: Any, element_id: int = 0) -> Optional[Any]:
    """Build a PyVista PolyData from one computed GemPy structural element."""
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is required to display GemPy 3D surfaces inline: {exc}") from exc

    vertices = getattr(element, "vertices", None)
    if vertices is None:
        return None

    try:
        vertices = np.asarray(vertices, dtype=float)
    except Exception:
        return None

    if vertices.ndim != 2 or vertices.shape[0] == 0 or vertices.shape[1] != 3:
        return None

    faces_vtk = None
    for attr in ["edges", "simplices", "faces", "triangles"]:
        faces_vtk = _vtk_faces_from_element_faces(getattr(element, attr, None))
        if faces_vtk is not None:
            break

    if faces_vtk is not None:
        mesh = pv.PolyData(vertices, faces_vtk)
    else:
        # Fallback: show vertices as points if the GemPy build stores vertices
        # but no surface topology under the common attribute names.
        mesh = pv.PolyData(vertices)
        try:
            mesh.verts = np.hstack([
                np.ones((vertices.shape[0], 1), dtype=np.int64),
                np.arange(vertices.shape[0], dtype=np.int64).reshape(-1, 1),
            ])
        except Exception:
            pass

    name = str(getattr(element, "name", f"element_{element_id}"))
    if mesh.n_cells > 0:
        mesh.cell_data["element_id"] = np.full(mesh.n_cells, int(element_id), dtype=np.int32)
    if mesh.n_points > 0:
        mesh.point_data["element_id"] = np.full(mesh.n_points, int(element_id), dtype=np.int32)
    try:
        mesh.field_data["element_name"] = np.array([name])
    except Exception:
        pass

    return mesh


def _extract_gempy_surface_mesh_for_inline_viewer(geo_model: Any) -> tuple[Optional[Any], Dict[str, Any]]:
    """Extract computed GemPy surfaces into one browser-viewable PyVista mesh."""
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is required to display GemPy 3D surfaces inline: {exc}") from exc

    report: Dict[str, Any] = {
        "source": "geo_model.structural_frame.structural_elements",
        "elements": [],
        "mesh_count": 0,
        "message": "",
    }

    try:
        elements = list(getattr(geo_model.structural_frame, "structural_elements", []) or [])
    except Exception:
        elements = []

    meshes = []
    for i, element in enumerate(elements):
        name = str(getattr(element, "name", f"element_{i}"))
        vertices = getattr(element, "vertices", None)
        try:
            n_vertices = int(len(vertices)) if vertices is not None else 0
        except Exception:
            n_vertices = 0

        mesh = _polydata_from_gempy_element(element, i)
        info = {
            "id": i,
            "name": name,
            "vertices": n_vertices,
            "exported": bool(mesh is not None and getattr(mesh, "n_points", 0) > 0),
        }

        if mesh is not None and mesh.n_points > 0:
            info["cells"] = int(mesh.n_cells)
            info["points"] = int(mesh.n_points)
            meshes.append(mesh)

        report["elements"].append(info)

    if not meshes:
        report["message"] = (
            "No computed surface meshes were found in the GeoModel. "
            "Run Compute GemPy Model with Extract surface meshes enabled before running Plot GemPy 3D."
        )
        return None, report

    combined = meshes[0]
    for mesh in meshes[1:]:
        try:
            combined = combined.merge(mesh)
        except Exception:
            combined = pv.MultiBlock([combined, mesh]).combine()

    try:
        combined = combined.clean(tolerance=0.0)
    except Exception:
        pass

    report["mesh_count"] = len(meshes)
    report["combined_cells"] = int(combined.n_cells)
    report["combined_points"] = int(combined.n_points)
    report["message"] = "Computed GemPy surface meshes are available for inline 3D display."
    return combined, report



class PlotGemPy3DNode(BaseNode):
    type_name = "PlotGemPy3D"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"PlotGemPy3D expects geo_model input, got {rv.kind}")

        geo_model = rv.value
        token = put_runtime_object(geo_model, kind="geo_model", name=rv.name)
        query = urlencode({
            "show_data": str(_as_bool(params.get("show_data"), True)).lower(),
            "show_lith": str(_as_bool(params.get("show_lith"), True)).lower(),
            "show_surfaces": str(_as_bool(params.get("show_surfaces"), True)).lower(),
            "show_topography": str(_as_bool(params.get("show_topography"), True)).lower(),
            "show_boundaries": str(_as_bool(params.get("show_boundaries"), True)).lower(),
        })
        viewer_flags = {
            "show_data": _as_bool(params.get("show_data"), True),
            "show_lith": _as_bool(params.get("show_lith"), True),
            "show_surfaces": _as_bool(params.get("show_surfaces"), True),
            "show_topography": _as_bool(params.get("show_topography"), True),
            "show_boundaries": _as_bool(params.get("show_boundaries"), True),
        }

        preview = {
            "plot_type": "GemPy 3D",
            "runtime_token": token,
            "viewer_flags": viewer_flags,
            "pyvista_preview_url": f"/api/pyvista/gempy-model/{token}?{query}",
            "pyvista_button_label": "Open GemPy 3D Popup",
            "pyvista_note": "The right panel shows computed surface meshes directly. The popup button still uses gempy_viewer.plot_3d on the in-memory GeoModel.",
        }

        mesh, surface_report = _extract_gempy_surface_mesh_for_inline_viewer(geo_model)
        preview["surface_mesh_report"] = surface_report

        outputs: Dict[str, RuntimeValue] = {}

        if mesh is not None and getattr(mesh, "n_points", 0) > 0:
            _attach_web_surface_preview(
                preview,
                mesh,
                "gempy_3d_surfaces",
                preferred_scalar="element_id",
                show_edges=False,
            )
            preview["web_viewer_label"] = "GemPy 3D surfaces"
            preview["inline_display"] = "web_3d_viewer"
            preview["element_id_map"] = {
                str(item["id"]): item["name"]
                for item in surface_report.get("elements", [])
                if item.get("exported")
            }
            outputs["mesh"] = RuntimeValue(
                "mesh",
                mesh,
                name="gempy_3d_surfaces",
                preview=preview,
                metadata=preview,
            )

            if _as_bool(params.get("save_surface_mesh"), True):
                requested_name = str(
                    params.get("surface_mesh_file_name")
                    or "gempy_3d_surfaces.vtp"
                ).strip() or "gempy_3d_surfaces.vtp"

                suffix = Path(requested_name).suffix.lower()
                if suffix not in {".vtp", ".vtu", ".vtk"}:
                    requested_name = (
                        f"{Path(requested_name).stem}.vtp"
                    )
                    suffix = ".vtp"

                output_path = make_runtime_path(
                    Path(requested_name).stem,
                    suffix,
                )

                if suffix == ".vtu":
                    mesh_to_save = mesh.cast_to_unstructured_grid()
                elif suffix == ".vtp":
                    mesh_to_save = mesh.extract_surface()
                else:
                    mesh_to_save = mesh

                mesh_to_save.save(output_path)
                record = register_output_file(
                    output_path,
                    display_name=requested_name,
                )

                preview.update(
                    {
                        "surface_mesh_file_name": requested_name,
                        "surface_mesh_download_url": (
                            f"/api/download/{record['file_id']}"
                        ),
                    }
                )

                outputs["file"] = RuntimeValue(
                    "file",
                    Path(record["path"]),
                    name=requested_name,
                    preview=preview,
                    metadata={
                        "file_id": record["file_id"],
                        **preview,
                    },
                )
        else:
            preview["web_viewer_available"] = False
            preview["web_viewer_reason"] = surface_report.get("message") or "No GemPy surface mesh available for inline display."
            preview["inline_display"] = "message"

        outputs["report"] = RuntimeValue("report", preview, name="gempy_3d_plot", preview=preview, metadata=preview)
        return outputs


class VoxelModelViewerNode(BaseNode):
    type_name = "VoxelModelViewer"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        array_name = params.get("array_name") or "lith_block"
        arr_rv = inputs.get("array")
        sol_rv = inputs.get("solution")
        if isinstance(arr_rv, list):
            arr_rv = arr_rv[0] if arr_rv else None
        if isinstance(sol_rv, list):
            sol_rv = sol_rv[0] if sol_rv else None

        if arr_rv is not None:
            if arr_rv.kind != "array":
                raise NodeExecutionError(f"VoxelModelViewer expects array input, got {arr_rv.kind}")
            arr = np.asarray(arr_rv.value)
            array_name = arr_rv.name or array_name
        elif sol_rv is not None:
            if sol_rv.kind != "gempy_solution":
                raise NodeExecutionError(f"VoxelModelViewer expects gempy_solution input, got {sol_rv.kind}")
            arr = extract_array_from_solution(sol_rv.value, str(array_name))
        else:
            raise NodeExecutionError("VoxelModelViewer needs either a solution input or an array input.")

        reshape = _parse_json(params.get("reshape"), None)
        arr = np.asarray(arr)
        if reshape:
            arr = arr.reshape(tuple(int(x) for x in reshape))
        elif arr.ndim == 1:
            cube = round(arr.size ** (1 / 3))
            if cube ** 3 == arr.size:
                reshape = [cube, cube, cube]
                arr = arr.reshape((cube, cube, cube))
        if arr.ndim != 3:
            raise NodeExecutionError(f"VoxelModelViewer needs a 3D array. {array_name} has shape {arr.shape}. Set Reshape, optional, for example [64,64,64].")

        file_name = params.get("file_name") or f"{array_name}_voxel_model.npy"
        if not str(file_name).lower().endswith(".npy"):
            file_name = f"{file_name}.npy"
        path = make_runtime_path(Path(str(file_name)).stem, ".npy")
        np.save(path, arr)
        record = register_output_file(path, display_name=str(file_name))
        extent = _parse_json(params.get("extent"), None)
        query_args = {
            "reshape": json.dumps(list(arr.shape)),
            "show_edges": str(_as_bool(params.get("show_edges"), False)).lower(),
            "threshold_background": str(_as_bool(params.get("threshold_background"), False)).lower(),
        }
        if extent:
            query_args["extent"] = json.dumps(extent)
        preview = {
            "plot_type": "Voxel model",
            "array_name": str(array_name),
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "min": float(np.nanmin(arr)) if arr.size else None,
            "max": float(np.nanmax(arr)) if arr.size else None,
            "unique_sample": np.unique(arr[: min(arr.size, 5000)]).tolist()[:50] if arr.size else [],
            "pyvista_preview_url": f"/api/pyvista/voxel/{record['file_id']}?{urlencode(query_args)}",
            "pyvista_button_label": "Open Voxel Model 3D Popup",
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": str(file_name),
        }
        return {"voxel_model": RuntimeValue("voxel_model", Path(record["path"]), name=str(file_name), preview=preview, metadata=preview)}



def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"auto", "none", "null"}:
        return None
    return float(text)


def _infer_voxel_spacing_from_grid(mesh: Any) -> Optional[tuple[float, float, float]]:
    """Infer voxel spacing from an existing regular/voxel-like PyVista grid."""
    try:
        centers = np.asarray(mesh.cell_centers().points, dtype=float)
    except Exception:
        return None
    if centers.size == 0:
        return None

    spacings = []
    for axis in range(3):
        vals = np.unique(np.round(centers[:, axis], 8))
        if vals.size >= 2:
            diffs = np.diff(vals)
            diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
            spacings.append(float(np.median(diffs)) if diffs.size else None)
        else:
            try:
                b = mesh.bounds
                spacings.append(float(abs(b[axis * 2 + 1] - b[axis * 2])))
            except Exception:
                spacings.append(None)
    if all(v is not None and v > 0 for v in spacings):
        return tuple(float(v) for v in spacings)  # type: ignore
    return None


def _infer_voxel_spacing(
    mesh: Any,
    *,
    dx: Optional[float] = None,
    dy: Optional[float] = None,
    dz: Optional[float] = None,
    reference_geo_model: Any = None,
    target_cells_longest_axis: int = 80,
) -> tuple[float, float, float, str]:
    """Determine voxel spacing for mesh-to-voxel conversion."""
    if dx is not None and dx > 0:
        if dy is None or dy <= 0:
            dy = dx
        if dz is None or dz <= 0:
            dz = dx
        return float(dx), float(dy), float(dz), "manual"

    if reference_geo_model is not None:
        try:
            ref_grid = _get_gempy_regular_grid_mesh(reference_geo_model)
            spacing = _infer_voxel_spacing_from_grid(ref_grid)
            if spacing is not None:
                return spacing[0], spacing[1], spacing[2], "reference_geo_model_regular_grid"
        except Exception:
            pass

    # If the input is already a voxel-like/regular grid, preserve its spacing.
    # Do not do this for arbitrary PolyData/surface meshes: their cell-center
    # coordinate differences can be tiny/irregular and would create an enormous
    # candidate voxel grid.
    is_voxel_like = False
    try:
        if hasattr(mesh, "celltypes"):
            ct = set(int(v) for v in np.unique(mesh.celltypes).tolist())
            # VTK_VOXEL=11, VTK_HEXAHEDRON=12
            is_voxel_like = bool(ct) and ct.issubset({11, 12})
    except Exception:
        is_voxel_like = False
    if is_voxel_like:
        spacing = _infer_voxel_spacing_from_grid(mesh)
        if spacing is not None:
            return spacing[0], spacing[1], spacing[2], "input_grid_cell_centers"

    b = [float(v) for v in mesh.bounds]
    lengths = [max(b[1] - b[0], 1e-9), max(b[3] - b[2], 1e-9), max(b[5] - b[4], 1e-9)]
    target = max(int(target_cells_longest_axis or 80), 1)
    base = max(lengths) / target
    return float(base), float(base), float(base), f"auto_longest_axis_{target}_cells"


def _make_voxel_unstructured_grid(
    centers: np.ndarray,
    spacing: tuple[float, float, float],
    cell_arrays: Optional[Dict[str, Any]] = None,
):
    """Build a PyVista UnstructuredGrid with VTK_VOXEL cells from centers."""
    import pyvista as pv

    centers = np.asarray(centers, dtype=float).reshape((-1, 3))
    dx, dy, dz = [float(v) for v in spacing]
    half = np.array([dx / 2.0, dy / 2.0, dz / 2.0], dtype=float)
    voxel_type = pv.CellType.VOXEL

    if centers.shape[0] == 0:
        return pv.UnstructuredGrid()

    points_blocks = []
    cells = []
    cell_types = []
    point_offset = 0

    # PyVista/VTK voxel point ordering:
    # 0:(xmin,ymin,zmin), 1:(xmax,ymin,zmin), 2:(xmin,ymax,zmin), 3:(xmax,ymax,zmin),
    # 4:(xmin,ymin,zmax), 5:(xmax,ymin,zmax), 6:(xmin,ymax,zmax), 7:(xmax,ymax,zmax)
    offsets = np.array([
        [-half[0], -half[1], -half[2]],
        [ half[0], -half[1], -half[2]],
        [-half[0],  half[1], -half[2]],
        [ half[0],  half[1], -half[2]],
        [-half[0], -half[1],  half[2]],
        [ half[0], -half[1],  half[2]],
        [-half[0],  half[1],  half[2]],
        [ half[0],  half[1],  half[2]],
    ], dtype=float)

    for c in centers:
        points_blocks.append(c[None, :] + offsets)
        cells.append(np.hstack([[8], np.arange(point_offset, point_offset + 8)]))
        cell_types.append(voxel_type)
        point_offset += 8

    grid = pv.UnstructuredGrid(np.hstack(cells), np.asarray(cell_types, dtype=np.uint8), np.vstack(points_blocks))
    if cell_arrays:
        for name, values in cell_arrays.items():
            arr = np.asarray(values)
            if arr.shape[0] == grid.n_cells:
                grid.cell_data[str(name)] = arr
    return grid


def _structured_voxel_axes_from_bounds(
    bounds: Sequence[float],
    spacing: tuple[float, float, float],
    padding: float = 0.0,
    max_voxels: int = 2000000,
    shared_grid: Optional[Dict[str, Any]] = None,
) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray], List[float]]:
    b = [float(v) for v in bounds]
    pad = float(padding or 0.0)
    xmin, xmax = b[0] - pad, b[1] + pad
    ymin, ymax = b[2] - pad, b[3] + pad
    zmin, zmax = b[4] - pad, b[5] + pad
    dx, dy, dz = [float(v) for v in spacing]
    if dx <= 0 or dy <= 0 or dz <= 0:
        raise NodeExecutionError("Voxel size must be positive.")
    aligned_counts = None
    if shared_grid is not None:
        try:
            aligned, aligned_counts = snap_bounds([xmin, xmax, ymin, ymax, zmin, zmax], shared_grid)
        except ValueError as exc:
            raise NodeExecutionError(str(exc)) from exc
        xmin, xmax, ymin, ymax, zmin, zmax = aligned

    nx = aligned_counts[0] if aligned_counts is not None else int(np.ceil((xmax - xmin) / dx))
    ny = aligned_counts[1] if aligned_counts is not None else int(np.ceil((ymax - ymin) / dy))
    nz = aligned_counts[2] if aligned_counts is not None else int(np.ceil((zmax - zmin) / dz))
    nx, ny, nz = max(nx, 1), max(ny, 1), max(nz, 1)
    total = nx * ny * nz
    if total > int(max_voxels):
        raise NodeExecutionError(
            f"Mesh to Voxel would create {total:,} candidate voxels ({nx}×{ny}×{nz}), "
            f"above max_voxels={int(max_voxels):,}. Increase voxel size or max_voxels."
        )

    xs = xmin + (np.arange(nx, dtype=float) + 0.5) * dx
    ys = ymin + (np.arange(ny, dtype=float) + 0.5) * dy
    zs = zmin + (np.arange(nz, dtype=float) + 0.5) * dz
    out_bounds = [xmin, xmin + nx * dx, ymin, ymin + ny * dy, zmin, zmin + nz * dz]
    return (xs, ys, zs), out_bounds


def _structured_voxel_centers_from_bounds(
    bounds: Sequence[float],
    spacing: tuple[float, float, float],
    padding: float = 0.0,
    max_voxels: int = 2000000,
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray, np.ndarray], List[float]]:
    axis_values, out_bounds = _structured_voxel_axes_from_bounds(
        bounds,
        spacing,
        padding=padding,
        max_voxels=max_voxels,
    )
    xs, ys, zs = axis_values
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    centers = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    return centers, (xs, ys, zs), out_bounds


def _resolve_mesh_or_file_input(inputs: Dict[str, Any], params: Dict[str, Any], *, input_port: str = "mesh", file_param: str = "mesh_file_id"):
    try:
        import pyvista as pv
    except Exception as exc:
        raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    rv = inputs.get(input_port)
    if isinstance(rv, list):
        rv = rv[0] if rv else None
    file_id = params.get(file_param) or ""
    if rv is not None:
        if rv.kind == "mesh":
            return rv.value, rv.name or "mesh", {"source_kind": "mesh_input", "source_name": rv.name or "mesh"}
        if rv.kind == "file":
            path = Path(rv.value)
            mesh = pv.read(str(path))
            return mesh, path.name, {"source_kind": "file_input", "source_name": path.name}
        raise NodeExecutionError(f"Expected mesh/file input for {input_port}, got {rv.kind}")
    if file_id:
        path = get_file_path(str(file_id))
        mesh = pv.read(str(path))
        return mesh, path.name, {"source_kind": "file_selector", "source_name": path.name, "file_id": str(file_id)}
    raise NodeExecutionError("Connect a mesh input or choose a mesh file.")


class MeshToVoxelModelNode(BaseNode):
    type_name = "MeshToVoxelModel"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import pyvista as pv
        except Exception as exc:
            raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

        mesh, source_name, source_meta = _resolve_mesh_or_file_input(inputs, params, input_port="mesh", file_param="mesh_file_id")

        ref_rv = inputs.get("reference_geo_model")
        if isinstance(ref_rv, list):
            ref_rv = ref_rv[0] if ref_rv else None
        reference_geo_model = ref_rv.value if ref_rv is not None and ref_rv.kind == "geo_model" else None

        dx = _as_optional_float(params.get("voxel_size"))
        dy = _as_optional_float(params.get("voxel_size_y"))
        dz = _as_optional_float(params.get("voxel_size_z"))
        target_axis = _as_int(params.get("target_cells_longest_axis"), 80) or 80
        max_voxels = _as_int(params.get("max_voxels"), 2000000) or 2000000
        padding = _as_float(params.get("padding"), 0.0) or 0.0
        voxelization_mode = str(params.get("voxelization_mode") or "inside_surface").strip().lower()
        distance_buffer = _as_optional_float(params.get("distance_buffer"))
        distance_chunk_size = _as_int(params.get("distance_chunk_size"), 200000) or 200000
        check_surface = _as_bool(params.get("check_surface"), False)
        tolerance = _as_float(params.get("inside_tolerance"), 1e-6) or 1e-6
        clean_output = _as_bool(params.get("clean_output"), True)
        invert = _as_bool(params.get("invert_inside"), False)

        shared_grid = _shared_grid_input(inputs)
        if shared_grid is not None:
            voxel_spacing = tuple(shared_grid["spacing"])
            spacing_source = "shared_grid"
        else:
            spacing = _infer_voxel_spacing(
                mesh,
                dx=dx,
                dy=dy,
                dz=dz,
                reference_geo_model=reference_geo_model,
                target_cells_longest_axis=target_axis,
            )
            voxel_spacing = (spacing[0], spacing[1], spacing[2])
            spacing_source = spacing[3]
        reference_surface = None
        if voxelization_mode in {"distance_to_surface", "surface_distance", "distance"} and "thickening_reference_points" in mesh.field_data:
            reference_surface = pv.PolyData(
                np.asarray(mesh.field_data["thickening_reference_points"]),
                np.asarray(mesh.field_data["thickening_reference_faces"], dtype=np.int64),
            )
            if distance_buffer is None:
                distance_buffer = float(mesh.field_data["thickening_half_width"][0])
        if distance_buffer is None:
            # For distance-to-surface voxelization, one voxel-ish thickness is a
            # useful automatic default. Users can set this explicitly for fault
            # buffer thickness.
            distance_buffer = 0.75 * float(min(voxel_spacing))
        effective_padding = max(float(padding), float(distance_buffer or 0.0)) if voxelization_mode in {"distance_to_surface", "surface_distance", "distance"} else float(padding)

        surface = reference_surface if reference_surface is not None else mesh.extract_surface()
        try:
            surface = surface.triangulate()
        except Exception:
            pass

        voxelization_report = {"mode": voxelization_mode}

        if voxelization_mode in {"distance_to_surface", "surface_distance", "distance"}:
            # This mode is intended for faults/open sheet-like surfaces. It does
            # not use inside/outside ray casting, so it avoids the fixed-direction
            # "fringe" artifacts that select_enclosed_points can create for
            # open/non-manifold fault meshes.
            axis_values, voxel_bounds = _structured_voxel_axes_from_bounds(
                surface.bounds,
                voxel_spacing,
                padding=effective_padding,
                max_voxels=int(max_voxels),
                shared_grid=shared_grid,
            )
            xs, ys, zs = axis_values
            nx, ny, nz = len(xs), len(ys), len(zs)
            candidate_count = int(nx * ny * nz)
            chunk_size = max(int(distance_chunk_size), 1)
            selected_chunks: List[np.ndarray] = []
            distance_min = np.inf
            distance_max = -np.inf
            yz_stride = int(ny * nz)
            try:
                # Use exact unsigned closest-point distances in bounded chunks.
                # Repeated ``compute_implicit_distance`` calls rebuild a VTK
                # implicit-distance pipeline and can terminate the process for
                # large/open or non-manifold sheets.  ``find_closest_cell``
                # reuses the surface locator and avoids that native crash.
                for start in range(0, candidate_count, chunk_size):
                    stop = min(start + chunk_size, candidate_count)
                    flat = np.arange(start, stop, dtype=np.int64)
                    ix = flat // yz_stride
                    remainder = flat % yz_stride
                    iy = remainder // nz
                    iz = remainder % nz
                    chunk_centers = np.column_stack((xs[ix], ys[iy], zs[iz]))
                    _closest_cells, closest_points = surface.find_closest_cell(
                        chunk_centers,
                        return_closest_point=True,
                    )
                    distances = np.linalg.norm(
                        chunk_centers - np.asarray(closest_points, dtype=float),
                        axis=1,
                    )
                    if distances.size:
                        distance_min = min(distance_min, float(np.nanmin(distances)))
                        distance_max = max(distance_max, float(np.nanmax(distances)))
                    chunk_mask = distances <= float(distance_buffer)
                    if np.any(chunk_mask):
                        selected_chunks.append(chunk_centers[chunk_mask].copy())
                selected_centers = (
                    np.concatenate(selected_chunks, axis=0)
                    if selected_chunks else np.empty((0, 3), dtype=float)
                )
                voxelization_report.update({
                    "distance_buffer": float(distance_buffer),
                    "distance_chunk_size": int(chunk_size),
                    "distance_min": float(distance_min) if np.isfinite(distance_min) else None,
                    "distance_max": float(distance_max) if np.isfinite(distance_max) else None,
                    "classification": "unsigned_closest_surface_distance <= distance_buffer",
                })
            except Exception as exc:
                raise NodeExecutionError(
                    f"Could not compute distance-to-surface voxelization: {exc}. "
                    "Try triangulating/cleaning the mesh, increasing Distance buffer, or use inside_surface mode for closed volumes."
                ) from exc
        else:
            axis_values, voxel_bounds = _structured_voxel_axes_from_bounds(
                surface.bounds,
                voxel_spacing,
                padding=effective_padding,
                max_voxels=int(max_voxels),
                shared_grid=shared_grid,
            )
            xs, ys, zs = axis_values
            nx, ny, nz = len(xs), len(ys), len(zs)
            candidate_count = int(nx * ny * nz)
            chunk_size = max(int(distance_chunk_size), 1)
            yz_stride = int(ny * nz)
            selected_chunks: List[np.ndarray] = []
            try:
                for start in range(0, candidate_count, chunk_size):
                    stop = min(start + chunk_size, candidate_count)
                    flat = np.arange(start, stop, dtype=np.int64)
                    ix = flat // yz_stride
                    remainder = flat % yz_stride
                    iy = remainder // nz
                    iz = remainder % nz
                    chunk_centers = np.column_stack((xs[ix], ys[iy], zs[iz]))
                    selected = pv.PolyData(chunk_centers).select_enclosed_points(
                        surface,
                        tolerance=float(tolerance),
                        check_surface=bool(check_surface),
                    )
                    chunk_mask = np.asarray(selected.point_data["SelectedPoints"]).astype(bool)
                    if invert:
                        chunk_mask = ~chunk_mask
                    if np.any(chunk_mask):
                        selected_chunks.append(chunk_centers[chunk_mask].copy())
                selected_centers = (
                    np.concatenate(selected_chunks, axis=0)
                    if selected_chunks else np.empty((0, 3), dtype=float)
                )
                voxelization_report.update({
                    "classification": "select_enclosed_points",
                    "classification_chunk_size": int(chunk_size),
                    "inside_tolerance": float(tolerance),
                    "check_surface": bool(check_surface),
                })
            except Exception as exc:
                raise NodeExecutionError(
                    f"Could not classify voxel centers inside the mesh surface: {exc}. "
                    "For open fault/sheet surfaces, use Voxelization mode = distance_to_surface."
                ) from exc
        if selected_centers.shape[0] == 0:
            raise NodeExecutionError(
                "No voxel centers were selected. For fault surfaces, use Voxelization mode = distance_to_surface and increase Distance buffer. "
                "For closed volumes, check voxel size, mesh closure, invert flag, or use Thicken Mesh first."
            )

        source_scalar = str(params.get("source_scalar") or "auto").strip() or "auto"
        output_scalar = str(params.get("output_scalar_name") or "MaterialIDs").strip() or "MaterialIDs"
        chosen_scalar = _choose_mesh_scalar(mesh, source_scalar)
        cell_arrays: Dict[str, Any] = {}

        if chosen_scalar:
            try:
                closest = np.asarray(mesh.find_closest_cell(selected_centers), dtype=int)
                if chosen_scalar in mesh.cell_data:
                    vals = np.asarray(mesh.cell_data[chosen_scalar])[closest]
                elif chosen_scalar in mesh.point_data:
                    # Use nearest mesh point value for point-data scalars.
                    closest_pts = np.asarray(mesh.find_closest_point(selected_centers), dtype=int)
                    vals = np.asarray(mesh.point_data[chosen_scalar])[closest_pts]
                else:
                    vals = np.ones(selected_centers.shape[0], dtype=np.int32)
                cell_arrays[output_scalar] = vals
                if chosen_scalar != output_scalar:
                    cell_arrays[chosen_scalar] = vals
            except Exception:
                cell_arrays[output_scalar] = np.ones(selected_centers.shape[0], dtype=np.int32)
        else:
            cell_arrays[output_scalar] = np.ones(selected_centers.shape[0], dtype=np.int32)

        cell_arrays["voxel_source_id"] = np.zeros(selected_centers.shape[0], dtype=np.int32)

        voxel_grid = _make_voxel_unstructured_grid(selected_centers, voxel_spacing, cell_arrays)
        if clean_output:
            try:
                voxel_grid = voxel_grid.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
            except TypeError:
                voxel_grid = voxel_grid.clean()
            except Exception:
                pass

        try:
            voxel_grid.set_active_scalars(output_scalar, preference="cell")
        except Exception:
            pass

        file_name = str(params.get("file_name") or "mesh_voxel_model.vtu").strip() or "mesh_voxel_model.vtu"
        if not file_name.lower().endswith(".vtu"):
            file_name = f"{Path(file_name).stem}.vtu"
        path = make_runtime_path(Path(file_name).stem, ".vtu")
        voxel_grid.save(path)
        record = register_output_file(path, display_name=file_name)

        preview_scalar = output_scalar if output_scalar in voxel_grid.cell_data else _choose_mesh_scalar(voxel_grid, "auto")
        preview = {
            "conversion": "mesh_to_voxel_model",
            "source": source_name,
            **source_meta,
            "voxel_size": [float(v) for v in voxel_spacing],
            "voxel_size_source": spacing_source,
            "shared_grid": shared_grid,
            "voxelization_mode": voxelization_mode,
            "distance_buffer": float(distance_buffer) if distance_buffer is not None else None,
            "distance_reference": "thicken_centre_surface" if reference_surface is not None else "input_surface",
            "thicken_input_thickness": float(mesh.field_data["thickening_half_width"][0]) * 2.0 if reference_surface is not None else None,
            "nominal_distance_band_thickness": float(distance_buffer) * 2.0 if voxelization_mode in {"distance_to_surface", "surface_distance", "distance"} else None,
            "thickness_note": "Thicken defines the physical input thickness. An empty distance radius preserves it; a nonempty radius overrides its half-width, rather than adding to the two shell skins. Cell boundaries still have voxel discretization error." if reference_surface is not None else "",
            "padding_requested": float(padding),
            "padding_effective": float(effective_padding),
            "voxelization_report": voxelization_report,
            "candidate_voxels": int(candidate_count),
            "selected_voxels": int(voxel_grid.n_cells),
            "voxel_bounds": [float(v) for v in voxel_bounds],
            "mesh_bounds": [float(v) for v in surface.bounds],
            "source_scalar_used": chosen_scalar or None,
            "output_scalar": preview_scalar or None,
            "cell_data": list(voxel_grid.cell_data.keys()),
            "n_cells": int(voxel_grid.n_cells),
            "n_points": int(voxel_grid.n_points),
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": file_name,
            "pyvista_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), True)).lower()}&scalars={quote(preview_scalar or '')}",
            "pyvista_button_label": "Open Voxel Model 3D Popup",
            "web_scalar": preview_scalar or "",
            "web_show_edges": _as_bool(params.get("show_edges"), True),
            "web_viewer_label": "Mesh-derived voxel model",
            "pyvista_note": "Use inside_surface for closed solids. Use distance_to_surface for open fault/sheet meshes to avoid ray-casting fringe artifacts.",
        }
        _attach_web_surface_preview(preview, voxel_grid, file_name, preferred_scalar=preview_scalar or "", show_edges=_as_bool(params.get("show_edges"), True))

        return {
            "voxel_model": RuntimeValue("mesh", voxel_grid, name=file_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "voxel_grid": RuntimeValue("mesh", voxel_grid, name=file_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview),
            "report": RuntimeValue("report", preview, name="mesh_to_voxel_report", preview=preview, metadata=preview),
        }


def _scalar_value_key(value: Any) -> Any:
    """Stable key for scalar values used in reindex mapping."""
    try:
        if hasattr(value, "item"):
            value = value.item()
    except Exception:
        pass
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        if np.isfinite(f) and abs(f - round(f)) < 1e-9:
            return int(round(f))
        return round(f, 12)
    if isinstance(value, np.ndarray):
        return tuple(_scalar_value_key(v) for v in value.ravel().tolist())
    if isinstance(value, (list, tuple)):
        return tuple(_scalar_value_key(v) for v in value)
    return str(value)


def _scalar_value_label(value: Any) -> str:
    key = _scalar_value_key(value)
    if isinstance(key, tuple):
        return ",".join(str(v) for v in key)
    return str(key)


def _parse_voxel_scalar_map(value: Any) -> List[Dict[str, Any]]:
    if value is None or str(value).strip() == "":
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []
    if isinstance(data, dict):
        out = []
        for k, v in data.items():
            try:
                idx = int(k)
            except Exception:
                continue
            out.append({"index": idx, "scalar": str(v)})
        return out
    if isinstance(data, list):
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("index"))
            except Exception:
                continue
            scalar = str(row.get("scalar") or row.get("cell_data_name") or row.get("name") or "auto").strip() or "auto"
            out.append({**row, "index": idx, "scalar": scalar})
        return out
    return []


def _select_voxel_scalar_for_source(mesh: Any, source_index: int, source_name: str, scalar_map: List[Dict[str, Any]], fallback: str) -> str:
    for row in scalar_map:
        try:
            if int(row.get("index")) == int(source_index):
                requested = str(row.get("scalar") or "auto").strip()
                if requested and requested.lower() not in {"auto", ""}:
                    chosen = _choose_mesh_scalar(mesh, requested)
                    if chosen:
                        return chosen
        except Exception:
            pass
    for row in scalar_map:
        if str(row.get("source_name") or "") == str(source_name):
            requested = str(row.get("scalar") or "auto").strip()
            if requested and requested.lower() not in {"auto", ""}:
                chosen = _choose_mesh_scalar(mesh, requested)
                if chosen:
                    return chosen

    if fallback and fallback.lower() not in {"auto", "none", ""} and fallback in getattr(mesh, "cell_data", {}):
        return fallback
    if fallback and fallback.lower() not in {"auto", "none", ""}:
        chosen = _choose_mesh_scalar(mesh, fallback)
        if chosen:
            return chosen
    return _choose_mesh_scalar(mesh, "auto")


def _choose_merge_target_spacing(
    meshes: List[Any],
    *,
    mode: str,
    manual_spacing: Optional[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], str, List[Dict[str, Any]]]:
    spacings: List[tuple[float, float, float]] = []
    report: List[Dict[str, Any]] = []
    for i, mesh in enumerate(meshes):
        spacing = _infer_voxel_spacing_from_grid(mesh)
        if spacing is None:
            b = [float(v) for v in mesh.bounds]
            spacing = (
                float((b[1] - b[0]) or 1.0),
                float((b[3] - b[2]) or 1.0),
                float((b[5] - b[4]) or 1.0),
            )
        spacing = tuple(float(v) if float(v) > 0 else 1.0 for v in spacing)
        spacings.append(spacing)
        report.append({"source_index": int(i), "inferred_spacing": [float(v) for v in spacing]})

    if manual_spacing is not None:
        return manual_spacing, "manual", report

    mode = str(mode or "smallest_input").strip().lower()
    arr = np.asarray(spacings, dtype=float)

    if mode in {"first", "first_input", "input_0"}:
        spacing = tuple(float(v) for v in arr[0])
        return spacing, "first_input", report
    if mode in {"largest", "largest_input", "coarsest"}:
        spacing = tuple(float(v) for v in np.max(arr, axis=0))
        return spacing, "largest_input", report
    if mode in {"median", "median_input"}:
        spacing = tuple(float(v) for v in np.median(arr, axis=0))
        return spacing, "median_input", report

    # Default: preserve details when input voxel models have different grid sizes.
    spacing = tuple(float(v) for v in np.min(arr, axis=0))
    return spacing, "smallest_input", report


def _aligned_origin_from_union(first_bounds: Sequence[float], union_bounds: Sequence[float], spacing: tuple[float, float, float]) -> tuple[float, float, float]:
    origin = []
    for axis in range(3):
        first_low = float(first_bounds[axis * 2])
        union_low = float(union_bounds[axis * 2])
        h = float(spacing[axis])
        if h <= 0:
            origin.append(union_low)
        else:
            origin.append(first_low + np.floor((union_low - first_low) / h) * h)
    return tuple(float(v) for v in origin)


def _cell_bounds_safe(mesh: Any, cell_index: int, fallback_center: np.ndarray, fallback_spacing: tuple[float, float, float]) -> tuple[float, float, float, float, float, float]:
    try:
        cell = mesh.get_cell(int(cell_index))
        pts = np.asarray(cell.points, dtype=float)
        if pts.ndim == 2 and pts.shape[0] > 0 and pts.shape[1] >= 3:
            return (
                float(np.nanmin(pts[:, 0])), float(np.nanmax(pts[:, 0])),
                float(np.nanmin(pts[:, 1])), float(np.nanmax(pts[:, 1])),
                float(np.nanmin(pts[:, 2])), float(np.nanmax(pts[:, 2])),
            )
    except Exception:
        pass
    cx, cy, cz = [float(v) for v in fallback_center]
    dx, dy, dz = [float(v) for v in fallback_spacing]
    return (cx - dx / 2, cx + dx / 2, cy - dy / 2, cy + dy / 2, cz - dz / 2, cz + dz / 2)


def _target_indices_covered_by_bounds(
    bounds: Sequence[float],
    spacing: tuple[float, float, float],
    origin: tuple[float, float, float],
    fallback_center: np.ndarray,
) -> List[tuple[int, int, int]]:
    """Return target-grid indices whose centers fall inside a source voxel/cell bounds.

    If the source cell is smaller than the target voxel and contains no target
    center, fall back to the target voxel containing the source cell center.
    """
    dx, dy, dz = [float(v) for v in spacing]
    x0, y0, z0 = [float(v) for v in origin]
    b = [float(v) for v in bounds]
    eps = 1e-9

    ranges = []
    for axis, (lo, hi, step, org) in enumerate([(b[0], b[1], dx, x0), (b[2], b[3], dy, y0), (b[4], b[5], dz, z0)]):
        if step <= 0:
            ranges.append([])
            continue
        # Centers are at org + (idx + 0.5) * step.
        imin = int(np.ceil((lo - org) / step - 0.5 - eps))
        imax = int(np.floor((hi - org) / step - 0.5 + eps))
        if imax < imin:
            idx = int(np.floor((float(fallback_center[axis]) - org) / step))
            ranges.append([idx])
        else:
            ranges.append(list(range(imin, imax + 1)))

    out = []
    for i in ranges[0]:
        for j in ranges[1]:
            for k in ranges[2]:
                out.append((int(i), int(j), int(k)))
    if not out:
        out = [(
            int(np.floor((float(fallback_center[0]) - x0) / dx)),
            int(np.floor((float(fallback_center[1]) - y0) / dy)),
            int(np.floor((float(fallback_center[2]) - z0) / dz)),
        )]
    return out


def _voxel_grid_entries(
    mesh: Any,
    spacing: tuple[float, float, float],
    origin: tuple[float, float, float],
    scalar_name: str,
    default_value: int,
    source_index: int,
    *,
    source_spacing: Optional[tuple[float, float, float]] = None,
    resample_to_target_grid: bool = True,
    max_entries: int = 2000000,
) -> tuple[List[tuple[tuple[int, int, int], Any, str, Dict[str, Any]]], Dict[str, Any]]:
    centers = np.asarray(mesh.cell_centers().points, dtype=float)
    if centers.size == 0:
        return [], {"input_cells": 0, "entries": 0, "expanded_cells": 0, "max_targets_per_cell": 0}
    if source_spacing is None:
        source_spacing = _infer_voxel_spacing_from_grid(mesh) or spacing
    direct_aligned = bool(
        centers_are_aligned(centers, {"origin": origin, "spacing": spacing})
        and voxel_cells_are_aligned(mesh, {"origin": origin, "spacing": spacing})
    )

    if scalar_name in getattr(mesh, "cell_data", {}):
        vals = np.asarray(mesh.cell_data[scalar_name])
    elif scalar_name in getattr(mesh, "point_data", {}):
        vals = _point_scalar_to_cell_scalar(mesh, scalar_name, missing_value=default_value)
    else:
        vals = np.full(mesh.n_cells, default_value, dtype=np.int32)

    dx, dy, dz = spacing
    x0, y0, z0 = origin
    entries: List[tuple[tuple[int, int, int], Any, str, Dict[str, Any]]] = []
    expanded_cells = 0
    max_targets_per_cell = 0

    for idx, center in enumerate(centers):
        if direct_aligned:
            target_keys = [tuple(int(v) for v in np.rint((center - np.asarray(origin)) / np.asarray(spacing) - 0.5))]
        elif resample_to_target_grid:
            bounds = _cell_bounds_safe(mesh, idx, center, source_spacing)
            target_keys = _target_indices_covered_by_bounds(bounds, spacing, origin, center)
        else:
            key = (
                int(np.floor((center[0] - x0) / dx)),
                int(np.floor((center[1] - y0) / dy)),
                int(np.floor((center[2] - z0) / dz)),
            )
            target_keys = [key]

        if len(target_keys) > 1:
            expanded_cells += 1
        max_targets_per_cell = max(max_targets_per_cell, len(target_keys))

        raw_value = vals[idx]
        value_key = _scalar_value_key(raw_value)
        value_label = _scalar_value_label(raw_value)
        for key in target_keys:
            entries.append((key, value_key, value_label, {
                "source_index": int(source_index),
                "source_cell": int(idx),
                "scalar_name": str(scalar_name),
            }))
            if len(entries) > int(max_entries):
                raise NodeExecutionError(
                    f"Merge Voxel Models produced more than {int(max_entries):,} target voxel entries for input {source_index}. "
                    "Use a larger target voxel size, choose Target voxel size mode = first_input/largest_input, or increase Max merged voxels."
                )

    report = {
        "input_cells": int(mesh.n_cells),
        "entries": int(len(entries)),
        "expanded_cells": int(expanded_cells),
        "max_targets_per_cell": int(max_targets_per_cell),
        "source_spacing": [float(v) for v in source_spacing],
        "direct_aligned": direct_aligned,
    }
    return entries, report



def _voxel_key_array_from_centers(centers: np.ndarray, spacing: tuple[float, float, float], origin: tuple[float, float, float]) -> np.ndarray:
    centers = np.asarray(centers, dtype=float).reshape((-1, 3))
    dx, dy, dz = [float(v) for v in spacing]
    x0, y0, z0 = [float(v) for v in origin]
    return np.column_stack([
        np.floor((centers[:, 0] - x0) / dx).astype(int),
        np.floor((centers[:, 1] - y0) / dy).astype(int),
        np.floor((centers[:, 2] - z0) / dz).astype(int),
    ])


def _expanded_xy_keys(i: int, j: int, radius: int) -> List[tuple[int, int]]:
    r = max(int(radius or 0), 0)
    out = []
    for di in range(-r, r + 1):
        for dj in range(-r, r + 1):
            out.append((int(i + di), int(j + dj)))
    return out


def _copy_cell_arrays_for_indices(mesh: Any, indices: np.ndarray) -> Dict[str, Any]:
    arrays: Dict[str, Any] = {}
    indices = np.asarray(indices, dtype=np.int64)
    for name, arr in getattr(mesh, "cell_data", {}).items():
        try:
            a = np.asarray(arr)
            if a.shape[0] == int(mesh.n_cells):
                arrays[str(name)] = a[indices]
        except Exception:
            pass
    return arrays


class ClipVoxelModelByMaskNode(BaseNode):
    type_name = "ClipVoxelModelByMask"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        base_rv = _single(inputs, "base_voxel_model")
        mask_rv = _single(inputs, "mask_voxel_model")
        if base_rv.kind != "mesh" or mask_rv.kind != "mesh":
            raise NodeExecutionError("Clip Voxel Model by Mask expects mesh/voxel-grid inputs.")

        base = base_rv.value
        mask = mask_rv.value

        base_centers = np.asarray(base.cell_centers().points, dtype=float)
        mask_centers = np.asarray(mask.cell_centers().points, dtype=float)
        if base_centers.size == 0:
            raise NodeExecutionError("Base voxel model has no cells.")
        if mask_centers.size == 0:
            raise NodeExecutionError("Mask voxel model has no cells.")

        dx = _as_optional_float(params.get("voxel_size"))
        dy = _as_optional_float(params.get("voxel_size_y"))
        dz = _as_optional_float(params.get("voxel_size_z"))
        if dx is not None and dx > 0:
            if dy is None or dy <= 0:
                dy = dx
            if dz is None or dz <= 0:
                dz = dx
            spacing = (float(dx), float(dy), float(dz))
            spacing_source = "manual"
        else:
            spacing = _infer_voxel_spacing_from_grid(base)
            if spacing is None:
                spacing = _infer_voxel_spacing_from_grid(mask)
            if spacing is None:
                raise NodeExecutionError("Could not infer voxel spacing. Set manual voxel size X/Y/Z.")
            spacing_source = "base_input" if _infer_voxel_spacing_from_grid(base) is not None else "mask_input"

        all_bounds = np.asarray([base.bounds, mask.bounds], dtype=float)
        union_bounds = [
            float(np.min(all_bounds[:, 0])), float(np.max(all_bounds[:, 1])),
            float(np.min(all_bounds[:, 2])), float(np.max(all_bounds[:, 3])),
            float(np.min(all_bounds[:, 4])), float(np.max(all_bounds[:, 5])),
        ]
        origin = _aligned_origin_from_union(base.bounds, union_bounds, spacing)

        base_keys = _voxel_key_array_from_centers(base_centers, spacing, origin)
        mask_keys = _voxel_key_array_from_centers(mask_centers, spacing, origin)

        clip_mode = str(params.get("clip_mode") or "same_xy_column_all_z").strip().lower()
        xy_expand = _as_int(params.get("xy_expand_cells"), 0) or 0
        z_margin_cells = _as_int(params.get("z_margin_cells"), 0) or 0
        resample_mask = _as_bool(params.get("resample_mask_to_base_grid"), True)
        keep_removed = _as_bool(params.get("output_removed_voxels"), False)
        clean_output = _as_bool(params.get("clean_output"), True)
        show_edges = _as_bool(params.get("show_edges"), True)

        mask_key_set = set()
        column_stats: Dict[tuple[int, int], Dict[str, int]] = {}

        # Build mask occupancy on the target/base grid. If mask voxels are larger
        # than base voxels, expand them to all covered target voxels.
        if resample_mask:
            mask_spacing = _infer_voxel_spacing_from_grid(mask) or spacing
            for ci, center in enumerate(mask_centers):
                bounds = _cell_bounds_safe(mask, ci, center, mask_spacing)
                target_keys = _target_indices_covered_by_bounds(bounds, spacing, origin, center)
                for key in target_keys:
                    i, j, k = [int(v) for v in key]
                    mask_key_set.add((i, j, k))
                    for xy in _expanded_xy_keys(i, j, xy_expand):
                        st = column_stats.setdefault(xy, {"min_k": int(k), "max_k": int(k)})
                        st["min_k"] = min(st["min_k"], int(k))
                        st["max_k"] = max(st["max_k"], int(k))
        else:
            for key_arr in mask_keys:
                i, j, k = [int(v) for v in key_arr]
                mask_key_set.add((i, j, k))
                for xy in _expanded_xy_keys(i, j, xy_expand):
                    st = column_stats.setdefault(xy, {"min_k": int(k), "max_k": int(k)})
                    st["min_k"] = min(st["min_k"], int(k))
                    st["max_k"] = max(st["max_k"], int(k))

        remove = np.zeros(int(base.n_cells), dtype=bool)
        for ci, key_arr in enumerate(base_keys):
            i, j, k = [int(v) for v in key_arr]
            if clip_mode in {"exact_overlap", "same_xyz"}:
                # Also honor XY expansion for exact overlap by checking nearby columns
                # at the same z level.
                if xy_expand > 0:
                    remove[ci] = any((xy[0], xy[1], k) in mask_key_set for xy in _expanded_xy_keys(i, j, xy_expand))
                else:
                    remove[ci] = (i, j, k) in mask_key_set
            else:
                st = column_stats.get((i, j))
                if st is None:
                    continue
                if clip_mode in {"same_xy_column_all_z", "same_xy_all_z", "column_all_z", "all_z"}:
                    remove[ci] = True
                elif clip_mode in {"same_xy_column_above_mask", "above_mask", "above"}:
                    # k increases with z because origin is the lower z bound.
                    remove[ci] = int(k) >= int(st["min_k"]) - int(z_margin_cells)
                elif clip_mode in {"same_xy_column_below_mask", "below_mask", "below"}:
                    remove[ci] = int(k) <= int(st["max_k"]) + int(z_margin_cells)
                elif clip_mode in {"same_xy_column_inside_mask_z_range", "inside_mask_z_range", "z_range"}:
                    remove[ci] = int(st["min_k"]) - int(z_margin_cells) <= int(k) <= int(st["max_k"]) + int(z_margin_cells)
                else:
                    raise NodeExecutionError(f"Unknown clip_mode: {clip_mode}")

        keep_indices = np.where(~remove)[0]
        removed_indices = np.where(remove)[0]
        if keep_indices.size == 0:
            raise NodeExecutionError("All base voxels were removed. Use a less aggressive clip mode or reduce XY expand cells.")

        kept_centers = base_centers[keep_indices]
        cell_arrays = _copy_cell_arrays_for_indices(base, keep_indices)
        cell_arrays["clip_mask_removed"] = np.zeros(kept_centers.shape[0], dtype=np.int32)

        clipped = _make_voxel_unstructured_grid(kept_centers, spacing, cell_arrays)
        if clean_output:
            try:
                clipped = clipped.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
            except TypeError:
                clipped = clipped.clean()
            except Exception:
                pass

        scalar = str(params.get("preview_scalar") or "auto").strip() or "auto"
        preview_scalar = _choose_mesh_scalar(clipped, scalar)

        file_name = str(params.get("file_name") or "clipped_voxel_model.vtu").strip() or "clipped_voxel_model.vtu"
        if not file_name.lower().endswith(".vtu"):
            file_name = f"{Path(file_name).stem}.vtu"
        path = make_runtime_path(Path(file_name).stem, ".vtu")
        clipped.save(path)
        record = register_output_file(path, display_name=file_name)

        report = {
            "operation": "clip_voxel_model_by_mask",
            "base_name": base_rv.name,
            "mask_name": mask_rv.name,
            "clip_mode": clip_mode,
            "xy_expand_cells": int(xy_expand),
            "z_margin_cells": int(z_margin_cells),
            "resample_mask_to_base_grid": bool(resample_mask),
            "voxel_size": [float(v) for v in spacing],
            "voxel_size_source": spacing_source,
            "origin": [float(v) for v in origin],
            "base_cells": int(base.n_cells),
            "mask_cells": int(mask.n_cells),
            "removed_cells": int(removed_indices.size),
            "kept_cells": int(keep_indices.size),
            "removed_fraction": float(removed_indices.size / max(int(base.n_cells), 1)),
            "mask_columns": int(len(column_stats)),
            "output_scalar": preview_scalar,
            "cell_data": list(clipped.cell_data.keys()),
            "n_cells": int(clipped.n_cells),
            "n_points": int(clipped.n_points),
            "bounds": [float(v) for v in clipped.bounds],
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": file_name,
            "pyvista_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(show_edges).lower()}&scalars={quote(preview_scalar or '')}",
            "pyvista_button_label": "Open Clipped Voxel Model 3D Popup",
            "web_scalar": preview_scalar or "",
            "web_show_edges": show_edges,
            "web_viewer_label": "Clipped voxel model",
            "pyvista_note": "This node removes voxels from the base model where the mask model occupies or occludes the target grid, so the mask can become visible after merging.",
        }
        _attach_web_surface_preview(report, clipped, file_name, preferred_scalar=preview_scalar or "", show_edges=show_edges)

        outputs = {
            "voxel_model": RuntimeValue("mesh", clipped, name=file_name, preview=report, metadata={"file_id": record["file_id"], **report}),
            "voxel_grid": RuntimeValue("mesh", clipped, name=file_name, preview=report, metadata={"file_id": record["file_id"], **report}),
            "file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=report, metadata=report),
            "report": RuntimeValue("report", report, name="clip_voxel_model_by_mask_report", preview=report, metadata=report),
        }

        if keep_removed and removed_indices.size > 0:
            removed_centers = base_centers[removed_indices]
            removed_arrays = _copy_cell_arrays_for_indices(base, removed_indices)
            removed_arrays["clip_mask_removed"] = np.ones(removed_centers.shape[0], dtype=np.int32)
            removed_grid = _make_voxel_unstructured_grid(removed_centers, spacing, removed_arrays)
            removed_name = f"{Path(file_name).stem}_removed.vtu"
            removed_path = make_runtime_path(Path(removed_name).stem, ".vtu")
            removed_grid.save(removed_path)
            removed_record = register_output_file(removed_path, display_name=removed_name)
            removed_preview = {
                **report,
                "file_name": removed_name,
                "removed_output": True,
                "n_cells": int(removed_grid.n_cells),
                "download_url": f"/api/download/{removed_record['file_id']}",
                "pyvista_preview_url": f"/api/pyvista/mesh/{removed_record['file_id']}?show_edges={str(show_edges).lower()}&scalars={quote(preview_scalar or '')}",
                "web_viewer_label": "Removed voxels",
            }
            _attach_web_surface_preview(removed_preview, removed_grid, removed_name, preferred_scalar=preview_scalar or "", show_edges=show_edges)
            outputs["removed_voxels"] = RuntimeValue("mesh", removed_grid, name=removed_name, preview=removed_preview, metadata={"file_id": removed_record["file_id"], **removed_preview})

        return outputs



class MergeVoxelModelsNode(BaseNode):
    type_name = "MergeVoxelModels"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        voxel_values: List[RuntimeValue] = []
        for port in ["voxel_models", "voxel_model", "voxel_grid", "mesh"]:
            voxel_values.extend(_many(inputs, port))

        if not voxel_values:
            raise NodeExecutionError("Merge Voxel Models needs at least one connected voxel model/grid input.")

        meshes = []
        sources = []
        for i, rv in enumerate(voxel_values):
            if rv.kind != "mesh":
                raise NodeExecutionError(f"Merge Voxel Models expects mesh/voxel-grid inputs, got {rv.kind} from {rv.name}")
            meshes.append(rv.value)
            sources.append(rv.name or f"voxel_{i}")

        first_wins = _as_bool(params.get("first_input_wins"), True)
        fallback_scalar = str(params.get("cell_data_name") or "auto").strip() or "auto"
        output_scalar = str(params.get("output_scalar_name") or "MaterialIDs").strip() or "MaterialIDs"
        # Important: keep source_and_value as the default. The user wants input-source
        # separation when building the output IDs.
        reindex_scope = str(params.get("reindex_scope") or "source_and_value").strip().lower()
        reindex_start_id = _as_int(params.get("reindex_start_id"), 1) or 1
        default_value = _as_int(params.get("default_value"), 1) or 1
        clean_output = _as_bool(params.get("clean_output"), True)
        show_edges = _as_bool(params.get("show_edges"), True)
        scalar_map = _parse_voxel_scalar_map(params.get("input_scalar_map_json"))
        max_merged_voxels = _as_int(params.get("max_merged_voxels"), 2000000) or 2000000
        resample_to_target_grid = _as_bool(params.get("resample_to_target_grid"), True)

        dx = _as_optional_float(params.get("voxel_size"))
        dy = _as_optional_float(params.get("voxel_size_y"))
        dz = _as_optional_float(params.get("voxel_size_z"))
        manual_spacing = None
        if dx is not None and dx > 0:
            if dy is None or dy <= 0:
                dy = dx
            if dz is None or dz <= 0:
                dz = dx
            manual_spacing = (float(dx), float(dy), float(dz))

        target_mode = str(params.get("target_voxel_size_mode") or "smallest_input").strip() or "smallest_input"
        spacing, spacing_source, spacing_report = _choose_merge_target_spacing(
            meshes,
            mode=target_mode,
            manual_spacing=manual_spacing,
        )
        shared_grid = _shared_grid_input(inputs)
        if shared_grid is not None:
            spacing = tuple(shared_grid["spacing"])
            spacing_source = "shared_grid"

        all_bounds = np.asarray([m.bounds for m in meshes], dtype=float)
        union_bounds = [
            float(np.min(all_bounds[:, 0])), float(np.max(all_bounds[:, 1])),
            float(np.min(all_bounds[:, 2])), float(np.max(all_bounds[:, 3])),
            float(np.min(all_bounds[:, 4])), float(np.max(all_bounds[:, 5])),
        ]
        origin = tuple(shared_grid["origin"]) if shared_grid is not None else _aligned_origin_from_union(meshes[0].bounds, union_bounds, spacing)

        source_entries: List[List[tuple[tuple[int, int, int], Any, str, Dict[str, Any]]]] = []
        scalar_selection_report = []
        value_order: List[Any] = []
        seen_value_keys = set()

        for source_index, mesh in enumerate(meshes):
            chosen = _select_voxel_scalar_for_source(mesh, source_index, sources[source_index], scalar_map, fallback_scalar)
            source_spacing = tuple(float(v) for v in spacing_report[source_index]["inferred_spacing"])
            entries, entry_report = _voxel_grid_entries(
                mesh,
                spacing,
                origin,
                chosen,
                default_value,
                source_index,
                source_spacing=source_spacing,
                resample_to_target_grid=resample_to_target_grid,
                max_entries=max_merged_voxels,
            )
            source_entries.append(entries)
            available = list(getattr(mesh, "cell_data", {}).keys())
            scalar_selection_report.append({
                "source_index": int(source_index),
                "source_name": sources[source_index],
                "selected_scalar": chosen,
                "available_cell_data": available,
                **entry_report,
            })
            for _voxel_key, scalar_value_key, scalar_value_label, _meta in entries:
                if reindex_scope == "global_value":
                    map_key = ("global", scalar_value_key)
                else:
                    map_key = (int(source_index), chosen, scalar_value_key)
                if map_key not in seen_value_keys:
                    seen_value_keys.add(map_key)
                    value_order.append((map_key, {
                        "source_index": int(source_index),
                        "source_name": sources[source_index],
                        "scalar": chosen,
                        "original_value": scalar_value_label,
                    }))

        occupied: Dict[tuple[int, int, int], Dict[str, Any]] = {}
        order = list(range(len(meshes)))
        write_order = list(reversed(order)) if first_wins else order
        write_report = []
        total_written_entries = 0

        for source_index in write_order:
            entries = source_entries[source_index]
            overwritten = 0
            source_internal_collisions = 0
            seen_this_source = set()
            for key, scalar_value_key, scalar_value_label, meta in entries:
                if key in seen_this_source:
                    source_internal_collisions += 1
                seen_this_source.add(key)

                if reindex_scope == "global_value":
                    map_key = ("global", scalar_value_key)
                else:
                    map_key = (int(source_index), meta["scalar_name"], scalar_value_key)
                if key in occupied:
                    overwritten += 1
                occupied[key] = {
                    "map_key": map_key,
                    "original_value": scalar_value_label,
                    "source_index": meta["source_index"],
                    "source_cell": meta["source_cell"],
                    "scalar_name": meta["scalar_name"],
                }
                total_written_entries += 1
                if len(occupied) > int(max_merged_voxels):
                    raise NodeExecutionError(
                        f"Merged voxel model exceeded Max merged voxels = {int(max_merged_voxels):,}. "
                        "Use a larger target voxel size or increase the limit."
                    )

            write_report.append({
                "source_index": int(source_index),
                "source_name": sources[source_index],
                "scalar_used": scalar_selection_report[source_index]["selected_scalar"],
                "input_cells": int(meshes[source_index].n_cells),
                "written_entries": int(len(entries)),
                "unique_target_cells_from_source": int(len(seen_this_source)),
                "source_internal_collisions": int(source_internal_collisions),
                "overwritten_existing_cells": int(overwritten),
            })

        if not occupied:
            raise NodeExecutionError("No occupied voxels found in the input voxel models.")

        final_map_keys = {entry["map_key"] for entry in occupied.values()}
        reindex_mapping: Dict[Any, int] = {}
        mapping_rows = []
        next_id = int(reindex_start_id)

        for map_key, row in value_order:
            if map_key not in final_map_keys or map_key in reindex_mapping:
                continue
            reindex_mapping[map_key] = next_id
            mapping_rows.append({**row, "new_id": int(next_id)})
            next_id += 1

        for map_key in final_map_keys:
            if map_key not in reindex_mapping:
                reindex_mapping[map_key] = next_id
                mapping_rows.append({
                    "source_index": map_key[0] if isinstance(map_key, tuple) and isinstance(map_key[0], int) else None,
                    "source_name": "",
                    "scalar": "",
                    "original_value": str(map_key[-1] if isinstance(map_key, tuple) else map_key),
                    "new_id": int(next_id),
                })
                next_id += 1

        keys = list(occupied.keys())
        centers = np.array([
            [origin[0] + (i + 0.5) * spacing[0], origin[1] + (j + 0.5) * spacing[1], origin[2] + (k + 0.5) * spacing[2]]
            for i, j, k in keys
        ], dtype=float)
        values = np.asarray([reindex_mapping[occupied[k]["map_key"]] for k in keys], dtype=np.int32)
        source_ids = np.asarray([occupied[k]["source_index"] for k in keys], dtype=np.int32)
        source_cells = np.asarray([occupied[k]["source_cell"] for k in keys], dtype=np.int32)

        cell_arrays = {
            output_scalar: values,
            "merge_source_index": source_ids,
            "merge_source_cell": source_cells,
        }
        merged = _make_voxel_unstructured_grid(centers, spacing, cell_arrays)

        if clean_output:
            try:
                merged = merged.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
            except TypeError:
                merged = merged.clean()
            except Exception:
                pass

        try:
            merged.set_active_scalars(output_scalar, preference="cell")
        except Exception:
            pass

        file_name = str(params.get("file_name") or "merged_voxel_model.vtu").strip() or "merged_voxel_model.vtu"
        if not file_name.lower().endswith(".vtu"):
            file_name = f"{Path(file_name).stem}.vtu"
        path = make_runtime_path(Path(file_name).stem, ".vtu")
        merged.save(path)
        record = register_output_file(path, display_name=file_name)

        preview = {
            "operation": "merge_voxel_models",
            "input_count": int(len(meshes)),
            "priority_rule": "first_input_wins" if first_wins else "last_input_wins",
            "input_order": sources,
            "scalar_selection": scalar_selection_report,
            "reindex_scope": reindex_scope,
            "reindex_start_id": int(reindex_start_id),
            "reindex_mapping": mapping_rows,
            "n_reindexed_values": int(len(mapping_rows)),
            "write_order": write_report,
            "target_voxel_size_mode": target_mode,
            "resample_to_target_grid": bool(resample_to_target_grid),
            "max_merged_voxels": int(max_merged_voxels),
            "total_written_entries": int(total_written_entries),
            "voxel_size": [float(v) for v in spacing],
            "voxel_size_source": spacing_source,
            "input_spacing_report": spacing_report,
            "origin": [float(v) for v in origin],
            "shared_grid": shared_grid,
            "fallback_input_scalar": fallback_scalar,
            "output_scalar": output_scalar,
            "output_scalar_note": "Selected scalar values are reindexed to consecutive integers. Default source_and_value keeps source-based ID separation; grid-size mismatch is handled by resampling input cells to the target grid.",
            "n_cells": int(merged.n_cells),
            "n_points": int(merged.n_points),
            "bounds": [float(v) for v in merged.bounds],
            "cell_data": list(merged.cell_data.keys()),
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": file_name,
            "pyvista_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(show_edges).lower()}&scalars={quote(output_scalar or '')}",
            "pyvista_button_label": "Open Merged Voxel Model 3D Popup",
            "web_scalar": output_scalar,
            "web_show_edges": show_edges,
            "web_viewer_label": "Merged voxel model",
            "pyvista_note": "Overlapping voxels are resolved by input order. Inputs with different grid sizes are resampled onto a common target voxel grid before merging.",
        }
        _attach_web_surface_preview(preview, merged, file_name, preferred_scalar=output_scalar, show_edges=show_edges)

        return {
            "voxel_model": RuntimeValue("mesh", merged, name=file_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "voxel_grid": RuntimeValue("mesh", merged, name=file_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview),
            "report": RuntimeValue("report", preview, name="merge_voxel_models_report", preview=preview, metadata=preview),
        }




class HexMeshToVoxelGridNode(BaseNode):
    type_name = "HexMeshToVoxelGrid"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import pyvista as pv
        except Exception as exc:
            raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

        mesh_rv = inputs.get("mesh")
        if isinstance(mesh_rv, list):
            mesh_rv = mesh_rv[0] if mesh_rv else None

        mesh_file_id = params.get("mesh_file_id") or ""
        if mesh_rv is not None:
            if mesh_rv.kind != "mesh":
                raise NodeExecutionError(f"HexMeshToVoxelGrid expects mesh input, got {mesh_rv.kind}")
            mesh = mesh_rv.value
            source_name = mesh_rv.name or "mesh"
        elif mesh_file_id:
            mesh_path = get_file_path(mesh_file_id)
            mesh = pv.read(str(mesh_path))
            source_name = mesh_path.name
        else:
            raise NodeExecutionError("Connect a mesh input or choose a mesh file. A mesh file can be .vtu/.vtk/.vti/.vtp etc.")

        skip_non_hex = _as_bool(params.get("skip_non_hex"), False)
        clean_after = _as_bool(params.get("clean_after"), True)
        tolerance = _as_float(params.get("tolerance"), 1e-8)
        voxel_grid, conversion_report = self._hexmesh_to_voxelgrid(mesh, skip_non_hex=skip_non_hex, tol=float(tolerance or 1e-8))

        material_scalar = str(params.get("material_scalar") or "auto").strip()
        output_material_name = str(params.get("output_material_name") or "MaterialIDs").strip() or "MaterialIDs"
        chosen_scalar = self._choose_material_scalar(voxel_grid, material_scalar)
        if chosen_scalar:
            try:
                voxel_grid.cell_data[output_material_name] = np.asarray(voxel_grid.cell_data[chosen_scalar])
                voxel_grid.set_active_scalars(output_material_name, preference="cell")
                preview_scalar = output_material_name
            except Exception:
                preview_scalar = chosen_scalar
        else:
            preview_scalar = ""

        before_points = int(voxel_grid.n_points)
        if clean_after:
            try:
                voxel_grid = voxel_grid.clean(tolerance=0.0, remove_unused_points=True, average_point_data=False)
            except TypeError:
                voxel_grid = voxel_grid.clean()
            except Exception:
                pass
        after_points = int(voxel_grid.n_points)

        file_name = str(params.get("file_name") or "gempy_volume_with_topo_voxel_grid.vtu")
        if not file_name.lower().endswith(".vtu"):
            file_name = f"{Path(file_name).stem}.vtu"
        path = make_runtime_path(Path(file_name).stem, ".vtu")
        voxel_grid.save(path)
        record = register_output_file(path, display_name=file_name)

        celltypes = np.unique(voxel_grid.celltypes).tolist() if hasattr(voxel_grid, "celltypes") else []
        # If cleaning changed cell_data ordering, keep the material scalar active and present.
        if preview_scalar and preview_scalar in voxel_grid.cell_data:
            try:
                voxel_grid.set_active_scalars(preview_scalar, preference="cell")
            except Exception:
                pass
        preview = {
            "conversion": "hexmesh_to_voxelgrid",
            "source": source_name,
            "material_scalar_used": preview_scalar or None,
            "n_cells": int(voxel_grid.n_cells),
            "n_points_before_clean": before_points,
            "n_points_after_clean": after_points,
            "unique_cell_types": [int(x) for x in celltypes],
            "cell_data": list(voxel_grid.cell_data.keys()),
            "bounds": [float(v) for v in voxel_grid.bounds],
            "skipped_non_hex_cells": int(conversion_report.get("skipped_non_hex_cells", 0)),
            "converted_cells": int(conversion_report.get("converted_cells", 0)),
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": file_name,
            "pyvista_preview_url": f"/api/pyvista/mesh/{record['file_id']}?show_edges={str(_as_bool(params.get('show_edges'), True)).lower()}&scalars={quote(preview_scalar or '')}",
            "pyvista_button_label": "Open Voxel Grid 3D Popup",
            "web_scalar": preview_scalar or "",
            "web_show_edges": _as_bool(params.get("show_edges"), True),
            "web_viewer_label": "Voxel grid mesh",
            "pyvista_note": "The preview is colored by the material/layer scalar, not by internal cell_ids. Default auto priority is MaterialIDs → id → lithology/lith_block.",
        }
        _attach_web_surface_preview(
            preview,
            voxel_grid,
            file_name,
            preferred_scalar=preview_scalar or "",
            show_edges=_as_bool(params.get("show_edges"), True),
        )
        preview["web_viewer_label"] = "Voxel grid mesh"

        return {
            "voxel_grid": RuntimeValue("mesh", voxel_grid, name=file_name, preview=preview, metadata={"file_id": record["file_id"], **preview}),
            "file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview),
        }

    @staticmethod
    def _choose_material_scalar(mesh: Any, requested: str) -> str:
        cell_keys = [str(k) for k in mesh.cell_data.keys()]
        if not cell_keys:
            return ""
        if requested and requested.lower() not in {"auto", "none", ""}:
            for key in cell_keys:
                if key == requested:
                    return key
            for key in cell_keys:
                if key.lower() == requested.lower():
                    return key
            return ""

        # Important: GemPy/PyVista grids often contain an internal 'cell_ids' array
        # with a unique value for every cell. Coloring by that array produces a
        # smooth rainbow like an index map. For geology/material visualization we
        # want the categorical layer id instead.
        priority_exact = ["MaterialIDs", "MaterialID", "material_ids", "material_id", "id", "ids", "lith_block", "lithology", "layer", "layer_id"]
        for name in priority_exact:
            for key in cell_keys:
                if key == name:
                    return key
        for needle in ["material", "lith", "formation", "layer"]:
            for key in cell_keys:
                if needle in key.lower() and "cell_id" not in key.lower():
                    return key
        for key in cell_keys:
            if key.lower() not in {"cell_ids", "cell_id", "vtkoriginalcellids", "vtkoriginalpointids"}:
                return key
        return cell_keys[0]

    def _hexmesh_to_voxelgrid(self, mesh: Any, *, skip_non_hex: bool = False, tol: float = 1e-8):
        import pyvista as pv

        voxel_cell_type = pv.CellType.VOXEL
        all_points = []
        all_cells = []
        cell_types = []
        cell_data_dict = {name: [] for name in mesh.cell_data.keys()}
        point_offset = 0
        skipped = 0
        converted = 0

        for i in range(mesh.n_cells):
            cell = mesh.get_cell(i)
            pts = np.asarray(cell.points, dtype=float)
            if pts.shape != (8, 3):
                if skip_non_hex:
                    skipped += 1
                    continue
                raise NodeExecutionError(f"Cell {i} does not have 8 points. Got shape {pts.shape}. Enable 'Skip non-hex cells' to ignore these cells.")

            mins = pts.min(axis=0)
            maxs = pts.max(axis=0)
            if np.any((maxs - mins) <= tol):
                if skip_non_hex:
                    skipped += 1
                    continue

            voxel_pts = np.array([
                [mins[0], mins[1], mins[2]],
                [maxs[0], mins[1], mins[2]],
                [mins[0], maxs[1], mins[2]],
                [maxs[0], maxs[1], mins[2]],
                [mins[0], mins[1], maxs[2]],
                [maxs[0], mins[1], maxs[2]],
                [mins[0], maxs[1], maxs[2]],
                [maxs[0], maxs[1], maxs[2]],
            ], dtype=float)
            all_points.append(voxel_pts)
            all_cells.append(np.hstack([[8], np.arange(point_offset, point_offset + 8)]))
            point_offset += 8
            cell_types.append(voxel_cell_type)
            for name in mesh.cell_data.keys():
                cell_data_dict[name].append(mesh.cell_data[name][i])
            converted += 1

        if not all_points:
            raise NodeExecutionError("No 8-point hexahedral cells were available for voxel conversion.")

        all_points_arr = np.vstack(all_points)
        all_cells_arr = np.hstack(all_cells)
        cell_types_arr = np.array(cell_types, dtype=np.uint8)
        voxel_grid = pv.UnstructuredGrid(all_cells_arr, cell_types_arr, all_points_arr)
        for name, values in cell_data_dict.items():
            voxel_grid.cell_data[name] = np.array(values)
        return voxel_grid, {"skipped_non_hex_cells": skipped, "converted_cells": converted}


class ExtractVoxelBoundariesNode(BaseNode):
    type_name = "ExtractVoxelBoundaries"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            import pyvista as pv
        except Exception as exc:
            raise NodeExecutionError(f"PyVista is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

        mesh_rv = inputs.get("mesh") or inputs.get("voxel_grid")
        if isinstance(mesh_rv, list):
            mesh_rv = mesh_rv[0] if mesh_rv else None

        mesh_file_id = params.get("mesh_file_id") or ""
        if mesh_rv is not None:
            if mesh_rv.kind != "mesh":
                raise NodeExecutionError(f"ExtractVoxelBoundaries expects a mesh/voxel_grid input, got {mesh_rv.kind}")
            mesh = mesh_rv.value
            source_name = mesh_rv.name or "voxel_grid"
        elif mesh_file_id:
            mesh_path = get_file_path(mesh_file_id)
            mesh = pv.read(str(mesh_path))
            source_name = mesh_path.name
        else:
            raise NodeExecutionError("Connect Hex Mesh to Voxel Grid.voxel_grid or choose a mesh file.")

        cell_data_name = str(params.get("cell_data_name") or "MaterialIDs").strip() or "MaterialIDs"
        if cell_data_name not in mesh.cell_data:
            available = list(mesh.cell_data.keys())
            raise NodeExecutionError(f"Cell data '{cell_data_name}' not found. Available cell_data: {available}")

        decimals = _as_int(params.get("decimals"), 8) or 8
        add_top_risers = _as_bool(params.get("add_top_risers"), True)
        triangulate_shells = _as_bool(params.get("triangulate_shells"), True)
        clean_tolerance = _as_float(params.get("clean_tolerance"), 1e-9)
        output_prefix = str(params.get("output_prefix") or "voxel").strip() or "voxel"
        show_edges = _as_bool(params.get("show_edges"), True)

        boundaries = self.extract_boundaries_from_voxel_grid(
            mesh,
            cell_data_name=cell_data_name,
            decimals=int(decimals),
            add_top_risers=add_top_risers,
        )

        side_mesh = boundaries["west"].merge(boundaries["east"]).merge(boundaries["north"]).merge(boundaries["south"])
        if triangulate_shells:
            side_mesh = side_mesh.triangulate()
        try:
            side_mesh = side_mesh.clean(tolerance=float(clean_tolerance if clean_tolerance is not None else 1e-9))
        except TypeError:
            side_mesh = side_mesh.clean()

        full_shell = boundaries["top"].merge(boundaries["bottom"]).merge(side_mesh)
        if triangulate_shells:
            full_shell = full_shell.triangulate()
        try:
            full_shell = full_shell.clean(tolerance=float(clean_tolerance if clean_tolerance is not None else 1e-9))
        except TypeError:
            full_shell = full_shell.clean()

        meshes_to_save = {
            "top": boundaries["top"],
            "bottom": boundaries["bottom"],
            "north": boundaries["north"],
            "south": boundaries["south"],
            "east": boundaries["east"],
            "west": boundaries["west"],
            "side_mesh": side_mesh,
            "full_shell": full_shell,
        }

        # Re-transfer point data after all merge/triangulate/clean operations.
        # This guarantees that combined side_mesh/full_shell outputs also inherit
        # all point arrays from the input voxel mesh.
        grid_info = self.build_voxel_index_map(mesh, cell_data_name=cell_data_name, decimals=int(decimals))
        point_transfer_reports: Dict[str, Dict[str, Any]] = {}
        for boundary_name, boundary_mesh in meshes_to_save.items():
            point_transfer_reports[boundary_name] = self.transfer_input_point_data(mesh, boundary_mesh, grid_info)

        colors = self._boundary_colors(params)
        selected_for_preview = [
            name for name in ["top", "bottom", "north", "south", "east", "west"]
            if _as_bool(params.get(f"show_{name}"), True if name in {"top", "bottom"} else False)
        ]
        if not selected_for_preview:
            selected_for_preview = ["top"]

        output_records: Dict[str, Dict[str, Any]] = {}
        outputs: Dict[str, RuntimeValue] = {}
        boundary_stats = {}
        manifest_items = []

        for name, bnd in meshes_to_save.items():
            saved = self.save_polydata_as_vtp_and_vtu(
                bnd,
                stem=f"{output_prefix}_{name}",
                display_stem=f"{output_prefix}_{name}",
            )
            rec_vtp = saved["vtp"]["record"]
            rec_vtu = saved["vtu"]["record"]
            display_name_vtp = saved["vtp"]["file_name"]
            display_name_vtu = saved["vtu"]["file_name"]

            output_records[name] = {"vtp": rec_vtp, "vtu": rec_vtu}
            cell_scalar_names = list(getattr(bnd, "cell_data", {}).keys())
            point_scalar_names = list(getattr(bnd, "point_data", {}).keys())
            boundary_stats[name] = {
                "n_cells": int(bnd.n_cells),
                "n_points": int(bnd.n_points),
                "bounds": [float(v) for v in bnd.bounds] if bnd.n_points else None,
                "cell_data": cell_scalar_names,
                "point_data": point_scalar_names,
                "input_cell_scalars_copied": [s for s in cell_scalar_names if s != "BoundaryID"],
                "input_point_scalars_copied": point_scalar_names,
                # Backward-compatible field name from v93.
                "input_scalars_copied": [s for s in cell_scalar_names if s != "BoundaryID"],
                "point_data_transfer": point_transfer_reports.get(name, {}),
                # Backward-compatible default download points to VTP.
                "download_url": f"/api/download/{rec_vtp['file_id']}",
                "download_url_vtp": f"/api/download/{rec_vtp['file_id']}",
                "download_url_vtu": f"/api/download/{rec_vtu['file_id']}",
                "file_name": display_name_vtp,
                "file_name_vtp": display_name_vtp,
                "file_name_vtu": display_name_vtu,
            }
            outputs[name] = RuntimeValue(
                "file",
                Path(rec_vtp["path"]),
                name=display_name_vtp,
                preview=boundary_stats[name],
                metadata={"file_id": rec_vtp["file_id"], "format": "vtp", **boundary_stats[name]},
            )
            outputs[f"{name}_vtu"] = RuntimeValue(
                "file",
                Path(rec_vtu["path"]),
                name=display_name_vtu,
                preview=boundary_stats[name],
                metadata={"file_id": rec_vtu["file_id"], "format": "vtu", **boundary_stats[name]},
            )
            if name in selected_for_preview:
                manifest_items.append({
                    "name": name,
                    "path": rec_vtp["path"],
                    "color": colors.get(name, "white"),
                    "n_cells": int(bnd.n_cells),
                })

        manifest = {
            "title": f"Voxel boundaries | {source_name}",
            "show_edges": show_edges,
            "items": manifest_items,
        }
        manifest_path = make_runtime_path(f"{output_prefix}_selected_boundaries_preview", ".json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_rec = register_output_file(manifest_path, display_name=f"{output_prefix}_selected_boundaries_preview.json")

        preview = {
            "operation": "extract_voxel_boundaries",
            "source": source_name,
            "cell_data_name": cell_data_name,
            "decimals": int(decimals),
            "grid_indexing": grid_info.get("grid_indexing"),
            "grid_shape": [int(grid_info["nx"]), int(grid_info["ny"]), int(grid_info["nz"])],
            "voxel_spacing": [float(grid_info["dx"]), float(grid_info["dy"]), float(grid_info["dz"])],
            "duplicate_quantized_centers": int(grid_info.get("duplicate_quantized_centers", 0)),
            "occupied_voxels": int(grid_info.get("occupied_voxels", 0)),
            "input_cells": int(grid_info.get("input_cells", mesh.n_cells)),
            "selected_for_preview": selected_for_preview,
            "boundary_stats": boundary_stats,
            "downloads": {name: info["download_url"] for name, info in boundary_stats.items()},
            "downloads_vtp": {name: info["download_url_vtp"] for name, info in boundary_stats.items()},
            "downloads_vtu": {name: info["download_url_vtu"] for name, info in boundary_stats.items()},
            "all_input_cell_scalars_copied": list(self.collect_input_cell_scalars(mesh).keys()),
            "all_input_point_scalars_copied": list(self.collect_input_point_scalars(mesh).keys()),
            "point_data_transfer": point_transfer_reports,
            "formats": ["vtp", "vtu"],
            "pyvista_preview_url": f"/api/pyvista/boundaries/{manifest_rec['file_id']}",
            "pyvista_button_label": "Open Selected Boundaries 3D Popup",
            "pyvista_note": "The popup shows only the boundaries selected by the node checkboxes. Each boundary is added as a separate PyVista mesh with its configured color.",
        }
        outputs["report"] = RuntimeValue("report", preview, name="voxel_boundaries", preview=preview, metadata=preview)
        outputs["selected_preview"] = RuntimeValue("file", Path(manifest_rec["path"]), name="selected_boundaries_preview.json", preview=preview, metadata={"file_id": manifest_rec["file_id"], **preview})
        outputs["side_mesh_obj"] = RuntimeValue("mesh", side_mesh, name=f"{output_prefix}_side_mesh", preview=boundary_stats["side_mesh"], metadata=boundary_stats["side_mesh"])
        outputs["full_shell_obj"] = RuntimeValue("mesh", full_shell, name=f"{output_prefix}_full_shell", preview=boundary_stats["full_shell"], metadata=boundary_stats["full_shell"])
        return outputs

    @staticmethod
    def _boundary_colors(params: Dict[str, Any]) -> Dict[str, str]:
        defaults = {
            "top": "red",
            "bottom": "blue",
            "north": "green",
            "south": "yellow",
            "east": "orange",
            "west": "purple",
        }
        out = {}
        for name, default in defaults.items():
            out[name] = str(params.get(f"color_{name}") or default).strip() or default
        return out

    @staticmethod
    def infer_regular_spacing_from_centers(values: Any, decimals: int = 8, min_diff: float = 1e-5) -> float:
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
    def build_voxel_index_map(mesh: Any, cell_data_name: str = "MaterialIDs", decimals: int = 8) -> Dict[str, Any]:
        centers = np.asarray(mesh.cell_centers().points, dtype=float)
        if centers.size == 0:
            raise NodeExecutionError("Cannot extract voxel boundaries from an empty mesh.")

        # Robust spacing inference: determine the real voxel size first, then
        # quantize every center back to integer grid indices. This prevents tiny
        # floating-point deviations from becoming fake grid columns/rows.
        dx = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 0], decimals)
        dy = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 1], decimals)
        dz = ExtractVoxelBoundariesNode.infer_regular_spacing_from_centers(centers[:, 2], decimals)

        xmin_c = float(np.round(np.nanmin(centers[:, 0]), int(decimals)))
        ymin_c = float(np.round(np.nanmin(centers[:, 1]), int(decimals)))
        zmin_c = float(np.round(np.nanmin(centers[:, 2]), int(decimals)))

        ii = np.rint((centers[:, 0] - xmin_c) / dx).astype(int)
        jj = np.rint((centers[:, 1] - ymin_c) / dy).astype(int)
        kk = np.rint((centers[:, 2] - zmin_c) / dz).astype(int)

        # Guard against negative indices caused by sub-decimal noise below min.
        ii -= int(ii.min()) if ii.size and ii.min() < 0 else 0
        jj -= int(jj.min()) if jj.size and jj.min() < 0 else 0
        kk -= int(kk.min()) if kk.size and kk.min() < 0 else 0

        nx, ny, nz = int(ii.max()) + 1, int(jj.max()) + 1, int(kk.max()) + 1
        occ = np.zeros((nx, ny, nz), dtype=bool)
        cell_ids = -np.ones((nx, ny, nz), dtype=int)

        duplicate_centers = 0
        for cid, (i, j, k) in enumerate(zip(ii, jj, kk)):
            i, j, k = int(i), int(j), int(k)
            if occ[i, j, k]:
                duplicate_centers += 1
                # Keep the first cell for this quantized voxel index. Duplicates
                # are expected only when numerical noise collapses to the same
                # true voxel center.
                continue
            occ[i, j, k] = True
            cell_ids[i, j, k] = int(cid)

        origin = (
            xmin_c - dx / 2.0,
            ymin_c - dy / 2.0,
            zmin_c - dz / 2.0,
        )

        xs = origin[0] + (np.arange(nx, dtype=float) + 0.5) * dx
        ys = origin[1] + (np.arange(ny, dtype=float) + 0.5) * dy
        zs = origin[2] + (np.arange(nz, dtype=float) + 0.5) * dz

        return {
            "xs": xs, "ys": ys, "zs": zs,
            "nx": nx, "ny": ny, "nz": nz,
            "dx": float(dx), "dy": float(dy), "dz": float(dz),
            "origin": origin,
            "occ": occ,
            "cell_ids": cell_ids,
            "cell_data_name": cell_data_name,
            "grid_indexing": "quantized_from_median_spacing",
            "duplicate_quantized_centers": int(duplicate_centers),
            "occupied_voxels": int(occ.sum()),
            "input_cells": int(mesh.n_cells),
        }

    @staticmethod
    def collect_input_cell_scalars(mesh: Any) -> Dict[str, np.ndarray]:
        """Collect all input cell-data scalar arrays that can be transferred to boundary faces.

        Boundary faces are generated from source voxel cells, so cell-data arrays
        can be copied unambiguously via the source cell id. 1D and multi-column
        cell arrays are both supported as long as their first dimension matches
        mesh.n_cells.
        """
        arrays: Dict[str, np.ndarray] = {}
        n_cells = int(getattr(mesh, "n_cells", 0))
        for name, values in getattr(mesh, "cell_data", {}).items():
            try:
                arr = np.asarray(values)
                if arr.shape[0] == n_cells:
                    arrays[str(name)] = arr
            except Exception:
                pass
        return arrays

    @staticmethod
    def boundary_arrays_from_source_cells(mesh: Any, source_cell_ids: List[int], preferred_first: Optional[str] = None) -> Dict[str, Any]:
        """Create boundary cell arrays by copying all input cell-data scalars.

        Each generated boundary face stores the scalar values of the voxel cell
        from which the face was generated.
        """
        scalars = ExtractVoxelBoundariesNode.collect_input_cell_scalars(mesh)
        if preferred_first and preferred_first in scalars:
            # Keep the preferred scalar first in JSON/VTK metadata where possible.
            scalars = {preferred_first: scalars[preferred_first], **{k: v for k, v in scalars.items() if k != preferred_first}}

        ids = np.asarray(source_cell_ids, dtype=np.int64)
        out: Dict[str, Any] = {}
        if ids.size == 0:
            for name, arr in scalars.items():
                out[name] = np.asarray(arr[:0])
            return out

        valid = (ids >= 0) & (ids < int(getattr(mesh, "n_cells", 0)))
        for name, arr in scalars.items():
            try:
                # Allocate with the correct trailing shape and dtype.
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
        n_points = int(getattr(mesh, "n_points", 0))
        for name, values in getattr(mesh, "point_data", {}).items():
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
                return ""
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
        input_points = np.asarray(getattr(mesh, "points", np.empty((0, 3))), dtype=float)
        boundary_points = np.asarray(getattr(boundary, "points", np.empty((0, 3))), dtype=float)
        n_boundary = int(boundary_points.shape[0])
        if n_boundary == 0:
            return np.empty((0,), dtype=np.int64), {
                "boundary_points": 0,
                "matched_points": 0,
                "unmatched_points": 0,
                "duplicate_input_grid_vertices": 0,
                "mapping": "quantized_grid_vertex",
            }
        if input_points.shape[0] == 0:
            return np.full(n_boundary, -1, dtype=np.int64), {
                "boundary_points": n_boundary,
                "matched_points": 0,
                "unmatched_points": n_boundary,
                "duplicate_input_grid_vertices": 0,
                "mapping": "quantized_grid_vertex",
            }

        dx, dy, dz = float(info["dx"]), float(info["dy"]), float(info["dz"])
        x0, y0, z0 = [float(v) for v in info["origin"]]
        nx, ny, nz = int(info["nx"]), int(info["ny"]), int(info["nz"])
        spacing = np.asarray([dx, dy, dz], dtype=float)
        origin = np.asarray([x0, y0, z0], dtype=float)

        input_ijk = np.rint((input_points - origin[None, :]) / spacing[None, :]).astype(np.int64)
        boundary_ijk = np.rint((boundary_points - origin[None, :]) / spacing[None, :]).astype(np.int64)

        sx = int(nx + 1)
        sy = int(ny + 1)
        sz = int(nz + 1)

        input_valid = (
            (input_ijk[:, 0] >= 0) & (input_ijk[:, 0] < sx)
            & (input_ijk[:, 1] >= 0) & (input_ijk[:, 1] < sy)
            & (input_ijk[:, 2] >= 0) & (input_ijk[:, 2] < sz)
        )
        boundary_valid = (
            (boundary_ijk[:, 0] >= 0) & (boundary_ijk[:, 0] < sx)
            & (boundary_ijk[:, 1] >= 0) & (boundary_ijk[:, 1] < sy)
            & (boundary_ijk[:, 2] >= 0) & (boundary_ijk[:, 2] < sz)
        )

        valid_input_ids = np.where(input_valid)[0].astype(np.int64)
        valid_input_ijk = input_ijk[input_valid]
        input_keys = (
            valid_input_ijk[:, 0]
            + sx * (valid_input_ijk[:, 1] + sy * valid_input_ijk[:, 2])
        ).astype(np.int64)

        # Sort once, then retain the first original point id for every regular
        # grid vertex. This is much lighter than a Python dictionary for large
        # voxel models.
        order = np.argsort(input_keys, kind="mergesort")
        sorted_keys = input_keys[order]
        sorted_ids = valid_input_ids[order]
        unique_keys, unique_first = np.unique(sorted_keys, return_index=True)
        unique_ids = sorted_ids[unique_first]
        duplicate_count = int(sorted_keys.size - unique_keys.size)

        mapped = np.full(n_boundary, -1, dtype=np.int64)
        valid_boundary_ids = np.where(boundary_valid)[0].astype(np.int64)
        valid_boundary_ijk = boundary_ijk[boundary_valid]
        boundary_keys = (
            valid_boundary_ijk[:, 0]
            + sx * (valid_boundary_ijk[:, 1] + sy * valid_boundary_ijk[:, 2])
        ).astype(np.int64)

        pos = np.searchsorted(unique_keys, boundary_keys)
        found = (pos < unique_keys.size)
        if found.any():
            found_indices = np.where(found)[0]
            found[found_indices] = unique_keys[pos[found_indices]] == boundary_keys[found_indices]
        mapped_valid = np.full(valid_boundary_ids.size, -1, dtype=np.int64)
        mapped_valid[found] = unique_ids[pos[found]]
        mapped[valid_boundary_ids] = mapped_valid

        # Rare fallback: when a generated point does not quantize to an existing
        # input vertex, query the nearest original point. The tolerance is tied
        # to the inferred voxel size, so a genuinely unrelated point is not used.
        missing = np.where(mapped < 0)[0]
        fallback_matches = 0
        if missing.size:
            try:
                from scipy.spatial import cKDTree
                tree = cKDTree(input_points)
                distances, nearest = tree.query(boundary_points[missing], k=1)
                tolerance = max(min(dx, dy, dz) * 1e-5, 1e-8)
                accept = np.isfinite(distances) & (distances <= tolerance)
                mapped[missing[accept]] = np.asarray(nearest, dtype=np.int64)[accept]
                fallback_matches = int(np.count_nonzero(accept))
            except Exception:
                pass

        matched = int(np.count_nonzero(mapped >= 0))
        return mapped, {
            "boundary_points": n_boundary,
            "matched_points": matched,
            "unmatched_points": int(n_boundary - matched),
            "fallback_nearest_matches": fallback_matches,
            "duplicate_input_grid_vertices": duplicate_count,
            "mapping": "quantized_grid_vertex",
        }

    @classmethod
    def transfer_input_point_data(cls, mesh: Any, boundary: Any, info: Dict[str, Any]) -> Dict[str, Any]:
        """Copy every compatible input point-data array to a boundary mesh."""
        point_scalars = cls.collect_input_point_scalars(mesh)
        source_point_ids, mapping_report = cls.map_boundary_points_to_input_points(mesh, boundary, info)

        copied_names: List[str] = []
        if int(getattr(boundary, "n_points", 0)) == 0:
            return {
                **mapping_report,
                "copied_point_data": copied_names,
            }

        valid = source_point_ids >= 0
        for name, values in point_scalars.items():
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
                # Some object/string arrays may not be supported by a particular
                # VTK writer. Keep the remaining arrays rather than failing the
                # complete boundary extraction.
                pass

        return {
            **mapping_report,
            "copied_point_data": copied_names,
        }

    @staticmethod
    def save_polydata_as_vtp_and_vtu(mesh: Any, stem: str, display_stem: str) -> Dict[str, Dict[str, Any]]:
        """Save a boundary mesh in both VTP and VTU formats and register both files."""
        vtp_path = make_runtime_path(stem, ".vtp")
        mesh.save(vtp_path)
        vtp_name = f"{display_stem}.vtp"
        vtp_rec = register_output_file(vtp_path, display_name=vtp_name)

        vtu_path = make_runtime_path(stem, ".vtu")
        try:
            ug = mesh.cast_to_unstructured_grid()
            ug.save(vtu_path)
        except Exception:
            # Fallback: PyVista usually supports this conversion for PolyData.
            # If not, try extract_surface -> cast one more time to keep the node usable.
            ug = mesh.extract_surface().cast_to_unstructured_grid()
            ug.save(vtu_path)
        vtu_name = f"{display_stem}.vtu"
        vtu_rec = register_output_file(vtu_path, display_name=vtu_name)

        return {
            "vtp": {"record": vtp_rec, "file_name": vtp_name, "path": vtp_path},
            "vtu": {"record": vtu_rec, "file_name": vtu_name, "path": vtu_path},
        }

    @staticmethod
    def build_polydata_from_quads(quads: List[np.ndarray], cell_arrays: Optional[Dict[str, Any]] = None):
        import pyvista as pv
        if len(quads) == 0:
            poly = pv.PolyData()
            if cell_arrays is not None:
                for name, vals in cell_arrays.items():
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
            for name, vals in cell_arrays.items():
                poly.cell_data[name] = np.asarray(vals)
        try:
            poly = poly.clean(tolerance=0.0)
        except TypeError:
            poly = poly.clean()
        return poly

    @classmethod
    def extract_top_bottom(cls, info: Dict[str, Any], mesh: Any, cell_data_name: str = "MaterialIDs", add_top_risers: bool = True):
        occ = info["occ"]
        cell_ids = info["cell_ids"]
        nx, ny, nz = info["nx"], info["ny"], info["nz"]
        dx, dy, dz = info["dx"], info["dy"], info["dz"]
        x0, y0, z0 = info["origin"]

        top_quads, bot_quads = [], []
        top_source_cells: List[int] = []
        bot_source_cells: List[int] = []

        def voxel_bounds(i, j, k):
            xmin = x0 + i * dx
            xmax = xmin + dx
            ymin = y0 + j * dy
            ymax = ymin + dy
            zmin = z0 + k * dz
            zmax = zmin + dz
            return xmin, xmax, ymin, ymax, zmin, zmax

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

                xmin, xmax, ymin, ymax, zmin, zmax = voxel_bounds(i, j, k_top)
                top_quads.append(np.array([[xmin, ymin, zmax], [xmax, ymin, zmax], [xmax, ymax, zmax], [xmin, ymax, zmax]]))
                top_source_cells.append(int(cell_ids[i, j, k_top]))

                xmin, xmax, ymin, ymax, zmin, zmax = voxel_bounds(i, j, k_bot)
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
                            lo, hi = z0 + k * dz, z0 + (k + 1) * dz
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
                            lo, hi = z0 + k * dz, z0 + (k + 1) * dz
                            quad = np.array([[xmin, y, lo], [xmax, y, lo], [xmax, y, hi], [xmin, y, hi]])
                            top_quads.append(quad[::-1] if k1 > k2 else quad)
                            top_source_cells.append(owner)

        top_arrays = cls.boundary_arrays_from_source_cells(mesh, top_source_cells, preferred_first=cell_data_name)
        bot_arrays = cls.boundary_arrays_from_source_cells(mesh, bot_source_cells, preferred_first=cell_data_name)
        top = cls.build_polydata_from_quads(top_quads, top_arrays)
        bottom = cls.build_polydata_from_quads(bot_quads, bot_arrays)
        return top, bottom


    @staticmethod
    def build_footprint_boundary_edges(info: Dict[str, Any]):
        occ = info["occ"]
        nx, ny, nz = info["nx"], info["ny"], info["nz"]
        footprint = occ.any(axis=2)
        edges = []
        for i in range(nx):
            for j in range(ny):
                if not footprint[i, j]:
                    continue
                if j == 0 or not footprint[i, j - 1]:
                    edges.append({"start": (i, j), "end": (i + 1, j), "column": (i, j), "local_face": "south"})
                if i == nx - 1 or not footprint[i + 1, j]:
                    edges.append({"start": (i + 1, j), "end": (i + 1, j + 1), "column": (i, j), "local_face": "east"})
                if j == ny - 1 or not footprint[i, j + 1]:
                    edges.append({"start": (i + 1, j + 1), "end": (i, j + 1), "column": (i, j), "local_face": "north"})
                if i == 0 or not footprint[i - 1, j]:
                    edges.append({"start": (i, j + 1), "end": (i, j), "column": (i, j), "local_face": "west"})
        return footprint, edges

    @staticmethod
    def polygon_area_xy(points: np.ndarray) -> float:
        x = points[:, 0]
        y = points[:, 1]
        return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)

    @staticmethod
    def trace_cycles_from_oriented_edges(edges: List[Dict[str, Any]]):
        start_to_edge = {}
        for eid, e in enumerate(edges):
            start_to_edge.setdefault(e["start"], []).append(eid)
        used = np.zeros(len(edges), dtype=bool)
        cycles = []
        for eid0 in range(len(edges)):
            if used[eid0]:
                continue
            cycle_edge_ids, cycle_vertices = [], []
            eid = eid0
            v_start = edges[eid]["start"]
            while True:
                if used[eid]:
                    break
                used[eid] = True
                e = edges[eid]
                cycle_edge_ids.append(eid)
                if len(cycle_vertices) == 0:
                    cycle_vertices.append(e["start"])
                cycle_vertices.append(e["end"])
                v = e["end"]
                if v == v_start:
                    break
                candidates = start_to_edge.get(v, [])
                next_unused = [cid for cid in candidates if not used[cid]]
                if len(next_unused) == 0:
                    raise NodeExecutionError("Boundary tracing failed: open contour encountered.")
                if len(next_unused) > 1:
                    raise NodeExecutionError("Boundary tracing failed: ambiguous next edge encountered.")
                eid = next_unused[0]
            cycles.append({"edge_ids": cycle_edge_ids, "vertices": cycle_vertices})
        return cycles

    @staticmethod
    def rotate_to_min_gap(angles: np.ndarray) -> int:
        diffs = np.abs(np.diff(np.r_[angles, angles[0] + 2 * np.pi]))
        return int(np.argmax(diffs) + 1) % len(angles)

    @staticmethod
    def angle_in_ccw_interval(a: float, start: float, end: float) -> bool:
        if start <= end:
            return start <= a < end
        return (a >= start) or (a < end)

    @classmethod
    def classify_cycle_edges_into_four_sides(
        cls,
        cycle: Dict[str, Any],
        info: Dict[str, Any],
        angle_offset_deg: float = 0.0,
        south_east_boundary_deg: float = -49.0,
        east_north_boundary_deg: float = 45.0,
        north_west_boundary_deg: float = 135.0,
        west_south_boundary_deg: float = 226.0,
    ) -> Dict[str, List[int]]:
        x0, y0, _ = info["origin"]
        dx, dy = info["dx"], info["dy"]
        edge_ids = cycle["edge_ids"]
        verts = cycle["vertices"][:-1]
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
        side_groups = {"north": [], "south": [], "east": [], "west": []}
        for eid, ang in zip(edge_ids_rot, angles_rot):
            a = ang % (2 * np.pi)
            if cls.angle_in_ccw_interval(a, b_se, b_en):
                side_groups["east"].append(eid)
            elif cls.angle_in_ccw_interval(a, b_en, b_nw):
                side_groups["north"].append(eid)
            elif cls.angle_in_ccw_interval(a, b_nw, b_ws):
                side_groups["west"].append(eid)
            else:
                side_groups["south"].append(eid)
        return side_groups

    @classmethod
    def extract_side_meshes_from_outer_contours(cls, info: Dict[str, Any], mesh: Any, cell_data_name: str = "MaterialIDs") -> Dict[str, Any]:
        footprint, edges = cls.build_footprint_boundary_edges(info)
        cycles = cls.trace_cycles_from_oriented_edges(edges)
        outer_cycles = []
        x0, y0, _ = info["origin"]
        dx, dy = info["dx"], info["dy"]
        for cyc in cycles:
            verts = cyc["vertices"][:-1]
            verts_phys = np.array([[x0 + u * dx, y0 + v * dy] for (u, v) in verts])
            area = cls.polygon_area_xy(verts_phys)
            if area > 0:
                outer_cycles.append(cyc)

        occ = info["occ"]
        cell_ids = info["cell_ids"]
        dz = info["dz"]
        z0 = info["origin"][2]
        side_quads = {"north": [], "south": [], "east": [], "west": []}
        side_source_cells = {"north": [], "south": [], "east": [], "west": []}

        def vertex_phys(v):
            u, vv = v
            return np.array([x0 + u * dx, y0 + vv * dy])

        for cyc in outer_cycles:
            side_edge_groups = cls.classify_cycle_edges_into_four_sides(cyc, info)
            for side_name, edge_ids in side_edge_groups.items():
                for eid in edge_ids:
                    e = edges[eid]
                    i, j = e["column"]
                    p0_xy = vertex_phys(e["start"])
                    p1_xy = vertex_phys(e["end"])
                    ks = np.where(occ[i, j, :])[0]
                    if len(ks) == 0:
                        continue
                    for k in ks:
                        zmin = z0 + k * dz
                        zmax = zmin + dz
                        q = np.array([
                            [p0_xy[0], p0_xy[1], zmin],
                            [p1_xy[0], p1_xy[1], zmin],
                            [p1_xy[0], p1_xy[1], zmax],
                            [p0_xy[0], p0_xy[1], zmax],
                        ])
                        side_quads[side_name].append(q)
                        side_source_cells[side_name].append(int(cell_ids[i, j, k]))
        return {
            side_name: cls.build_polydata_from_quads(
                side_quads[side_name],
                cls.boundary_arrays_from_source_cells(mesh, side_source_cells[side_name], preferred_first=cell_data_name),
            )
            for side_name in ["north", "south", "east", "west"]
        }


    @classmethod
    def extract_boundaries_from_voxel_grid(cls, mesh: Any, cell_data_name: str = "MaterialIDs", decimals: int = 8, add_top_risers: bool = True) -> Dict[str, Any]:
        info = cls.build_voxel_index_map(mesh, cell_data_name=cell_data_name, decimals=decimals)
        top, bottom = cls.extract_top_bottom(info, mesh, cell_data_name=cell_data_name, add_top_risers=add_top_risers)
        side_meshes = cls.extract_side_meshes_from_outer_contours(info, mesh, cell_data_name=cell_data_name)
        boundaries = {
            "top": top,
            "bottom": bottom,
            "north": side_meshes["north"],
            "south": side_meshes["south"],
            "east": side_meshes["east"],
            "west": side_meshes["west"],
        }
        boundary_id_map = {"top": 1, "bottom": 2, "north": 3, "south": 4, "east": 5, "west": 6}
        for name, bnd in boundaries.items():
            bnd.cell_data["BoundaryID"] = np.full(bnd.n_cells, boundary_id_map[name], dtype=np.int32)
            point_report = cls.transfer_input_point_data(mesh, bnd, info)
            try:
                bnd._node_editor_point_data_transfer_report = point_report
            except Exception:
                pass
        return boundaries


class ExtractGemPyArrayNode(BaseNode):
    type_name = "ExtractGemPyArray"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "solution")
        if rv.kind != "gempy_solution":
            raise NodeExecutionError(f"ExtractGemPyArray expects gempy_solution input, got {rv.kind}")
        solution = rv.value
        output_name = params.get("output_name") or "lith_block"
        arr = extract_array_from_solution(solution, output_name)
        reshape = _parse_json(params.get("reshape"), None)
        if reshape:
            arr = np.asarray(arr).reshape(tuple(reshape))
        arr = np.asarray(arr)
        preview = {
            "output_name": output_name,
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "min": float(np.nanmin(arr)) if arr.size else None,
            "max": float(np.nanmax(arr)) if arr.size else None,
            "unique_sample": np.unique(arr[: min(arr.size, 5000)]).tolist()[:50] if arr.size else [],
        }
        return {"array": RuntimeValue("array", arr, name=output_name, preview=preview)}



class LoadGemPyModelJsonNode(BaseNode):
    type_name = "LoadGemPyModelJson"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        connected = inputs.get("file")
        path: Optional[Path] = None
        file_id = str(params.get("file_id") or "").strip()
        connected_file_id = ""

        if connected is not None:
            rv = connected[0] if isinstance(connected, list) else connected
            if rv.kind != "file":
                raise NodeExecutionError(f"Load GemPy Model expects a file input, got {rv.kind}")
            path = Path(rv.value)
            connected_file_id = str((rv.metadata or {}).get("file_id") or "")
        elif file_id:
            path = get_file_path(file_id)
        else:
            raise NodeExecutionError("Provide a connected .gempy or legacy .json file, or select an uploaded file.")

        if not path.exists():
            raise NodeExecutionError(f"GemPy model file does not exist: {path}")
        suffix = path.suffix.lower()
        if suffix not in {".gempy", ".json"}:
            raise NodeExecutionError(f"Expected a .gempy or legacy .json GemPy model file, got: {path.name}")

        try:
            if suffix == ".gempy":
                import gempy as gp
                geo_model = gp.load_model(str(path))
            else:
                from gempy.modules.json_io.json_operations import JsonIO
                _patch_gempy_stringarray_compat()
                geo_model = JsonIO.load_model_from_json(str(path))
        except Exception as exc:
            raise NodeExecutionError(f"GemPy failed to load {path.name}: {exc}") from exc

        inferred_resolution = _infer_geo_model_resolution(geo_model)
        try:
            geo_model._node_editor_loaded_from_json = suffix == ".json"
            geo_model._node_editor_resolution_was_explicit = inferred_resolution is not None
            geo_model._node_editor_resolution = inferred_resolution
        except Exception:
            pass

        preview = _geo_model_basic_preview(geo_model, name=path.stem, source="GemPy .gempy" if suffix == ".gempy" else "GemPy JsonIO")
        preview.update({
            "file_name": path.name,
            "loaded_from_file_id": file_id or connected_file_id,
            "regular_grid_resolution_inferred": inferred_resolution is not None,
        })
        if inferred_resolution is None and suffix == ".json":
            preview["warning"] = "No regular-grid resolution could be inferred from this legacy JSON model. gp.compute_model may fail."

        return {"geo_model": RuntimeValue("geo_model", geo_model, name=path.stem, preview=preview, metadata=preview)}


class SaveGemPyModelJsonNode(BaseNode):
    type_name = "SaveGemPyModelJson"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "geo_model")
        if rv.kind != "geo_model":
            raise NodeExecutionError(f"Save GemPy Model expects geo_model input, got {rv.kind}")
        geo_model = rv.value

        requested_name = Path(str(params.get("file_name") or "").strip() or "gempy_model.gempy").name
        file_name = f"{Path(requested_name).stem}.gempy" if Path(requested_name).suffix else f"{requested_name}.gempy"
        path = make_runtime_path(Path(file_name).stem, ".gempy")
        try:
            import gempy as gp
            saved_path = Path(gp.save_model(geo_model, path=str(path)))
        except Exception as exc:
            raise NodeExecutionError(f"GemPy failed to save model to .gempy: {exc}") from exc
        if not saved_path.exists():
            raise NodeExecutionError(f"GemPy reported a saved model, but the file does not exist: {saved_path}")

        record = register_output_file(saved_path, display_name=file_name)
        preview = {
            "file_id": record["file_id"],
            "download_url": f"/api/download/{record['file_id']}",
            "file_name": file_name,
            "format": "GemPy model (.gempy)",
            "message": "Model definition saved with gp.save_model. Computed solutions are not included; load and recompute the model when needed.",
        }
        return {
            "file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview),
            "report": RuntimeValue("report", preview, name="gempy_save_report", preview=preview, metadata=preview),
        }



class SaveArrayNode(BaseNode):
    type_name = "SaveArray"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        rv = _single(inputs, "array")
        if rv.kind != "array":
            raise NodeExecutionError(f"SaveArray expects array input, got {rv.kind}")
        fmt = params.get("format") or "npy"
        file_name = params.get("file_name") or f"{rv.name}.{fmt}"
        arr = np.asarray(rv.value)
        if fmt == "npy":
            if not file_name.lower().endswith(".npy"):
                file_name += ".npy"
            path = make_runtime_path(Path(file_name).stem, ".npy")
            np.save(path, arr)
        elif fmt == "csv":
            if arr.ndim > 2:
                raise NodeExecutionError("CSV export supports only 1D or 2D arrays. Use npy for 3D voxel/lith block arrays.")
            if not file_name.lower().endswith(".csv"):
                file_name += ".csv"
            path = make_runtime_path(Path(file_name).stem, ".csv")
            np.savetxt(path, arr, delimiter=",")
        else:
            raise NodeExecutionError(f"Unsupported array export format: {fmt}")
        record = register_output_file(path, display_name=file_name)
        preview = {"file_id": record["file_id"], "download_url": f"/api/download/{record['file_id']}", "file_name": file_name}
        return {"file": RuntimeValue("file", Path(record["path"]), name=file_name, preview=preview, metadata=preview)}



def _kadi_create_geodt_structure_record(
    manager: Any,
    *,
    now: datetime,
    title: str,
    description: str,
    subject: str,
    collection_id: int,
    version: str,
    tag: str = "geolab",
    add_group_roles: bool = True,
) -> Any:
    """Create a Kadi dataset record following createRecord(2).ipynb."""
    timestamp = now.strftime("%Y%m%d%H%M%S")
    version_timestamp = f"{version}_{timestamp}"
    full_title = f"{title} V{version_timestamp}"

    new_record = manager.record(
        create=True,
        identifier=full_title,
        title=full_title,
        description=description,
    )

    reference_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
    try:
        creator = manager.pat_user.meta["displayname"]
    except Exception:
        creator = ""

    metadata_paper = [
        {
            "key": "Subject",
            "term": "http://purl.org/dc/elements/1.1/subject",
            "type": "str",
            "validation": {"required": True},
            "value": subject,
        },
        {
            "key": "Dataset Category",
            "term": "http://purl.org/dc/elements/1.1/type",
            "type": "str",
            "validation": {
                "options": [
                    "GIS Project", "Raster Data", "Vector Data", "Logging Data",
                    "Time Series", "Mesh/CAD Data", "Sample", "Text",
                    "Presentation", "Visualisation", "Other",
                ],
                "required": True,
            },
            "value": "Mesh/CAD Data",
        },
        {
            "key": "Coverage",
            "term": "http://purl.org/dc/elements/1.1/coverage",
            "type": "str",
            "validation": {"required": True},
            "value": "GeoDT",
        },
        {
            "key": "Reference System",
            "type": "str",
            "validation": {
                "options": [
                    "EPSG:25832 (UTM Zone 32N)",
                    "EPSG:31463 (DHDN / 3-degree Gauss zone 3)",
                    "Other",
                    "None",
                ],
                "required": True,
            },
            "value": "EPSG:25832 (UTM Zone 32N)",
        },
        {
            "key": "Reference Date",
            "term": "http://purl.org/dc/elements/1.1/date",
            "type": "date",
            "validation": {"required": True},
            "value": reference_date,
        },
        {
            "key": "Language",
            "term": "http://purl.org/dc/elements/1.1/language",
            "type": "str",
            "validation": {
                "options": ["German", "English", "French", "Other", "None"],
                "required": True,
            },
            "value": "None",
        },
        {
            "key": "Format",
            "term": "http://purl.org/dc/elements/1.1/format",
            "type": "str",
            "validation": {
                "options": [
                    "ArcGIS project (gdb)",
                    "CSV (csv)",
                    "GIS Shape (shp)",
                    "GIS Raster (asc, ovr, tif, ...)",
                    "GOCAD (ts, pl, mx, ...)",
                    "Image (jpg, png, bmp, ...)",
                    "Microsoft Office (doc, xls, ppt, ...)",
                    "PDF (pdf)",
                    "QGIS project (qgz)",
                    "Text (txt, md)",
                    "Video (mp4, mkv, mov, ...)",
                    "Visualization Toolkit (vtp, vtu, vti, ...)",
                    "Multiple File Types",
                    "Other",
                ],
                "required": True,
            },
            "value": "Visualization Toolkit (vtp, vtu, vti, ...)",
        },
        {
            "key": "Responsible Party",
            "type": "str",
            "validation": {"required": True},
            "value": creator,
        },
        {
            "key": "Creator",
            "term": "http://purl.org/dc/elements/1.1/creator",
            "type": "str",
            "value": creator,
        },
        {
            "key": "Publisher",
            "term": "http://purl.org/dc/elements/1.1/publisher",
            "type": "str",
            "value": "",
        },
        {
            "key": "Contributers",
            "term": "http://purl.org/dc/elements/1.1/contributor",
            "type": "str",
            "value": "",
        },
        {
            "key": "Source",
            "term": "http://purl.org/dc/elements/1.1/source",
            "type": "str",
            "value": "",
        },
    ]

    new_record.edit(type="dataset", force=True)
    if tag:
        new_record.add_tag(tag)
    new_record.add_metadata(metadata_new=metadata_paper, force=True)

    if add_group_roles:
        new_record.add_group_role(143, "Admin")
        new_record.add_group_role(302, "Editor")
        new_record.add_group_role(122, "Member")

    model_collection = manager.collection(id=collection_id)
    try:
        model_collection.add_record_link(new_record.id)
    except Exception as exc:
        if "409" not in str(exc):
            raise

    return new_record


def _runtime_file_from_input(rv: RuntimeValue, input_name: str) -> Path:
    if rv.kind != "file":
        raise NodeExecutionError(f"Input port {input_name} expects a file output, got {rv.kind}. Connect the .file output from the upstream node.")
    path = Path(rv.value)
    if not path.exists():
        # Some file RuntimeValues carry only a file_id in metadata. Resolve it if possible.
        meta_file_id = str((rv.metadata or {}).get("file_id") or "")
        if meta_file_id:
            path = get_file_path(meta_file_id)
    if not path.exists():
        raise NodeExecutionError(f"Connected file for {input_name} does not exist: {path}")
    return path


def _prepare_kadi_named_vtk_file(source_path: Path, target_path: Path, *, convert_to_vtu: bool = True) -> Dict[str, Any]:
    """Copy/convert one VTK-family file to the exact upload target name.

    Boundary meshes are often saved as .vtp PolyData by Extract Voxel Boundaries,
    but the GeoDT upload convention requires .vtu names. When the target suffix
    is .vtu, this helper converts PolyData to UnstructuredGrid before saving.
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    source_suffix = source_path.suffix.lower()
    target_suffix = target_path.suffix.lower()
    converted = False

    if convert_to_vtu and target_suffix == ".vtu":
        try:
            import pyvista as pv
            mesh = pv.read(str(source_path))
            if not isinstance(mesh, pv.UnstructuredGrid):
                mesh = mesh.cast_to_unstructured_grid()
            mesh.save(str(target_path))
            converted = True
        except Exception as exc:
            # If the source is already a VTU or another valid file, fall back to
            # a plain copy only when suffixes match. Otherwise report a useful error.
            if source_suffix == target_suffix:
                shutil.copyfile(source_path, target_path)
            else:
                raise NodeExecutionError(f"Could not convert {source_path.name} to {target_path.name}: {exc}") from exc
    else:
        shutil.copyfile(source_path, target_path)

    return {
        "source": str(source_path),
        "target": str(target_path),
        "target_name": target_path.name,
        "source_suffix": source_suffix,
        "target_suffix": target_suffix,
        "converted_to_vtu": converted,
        "size_bytes": target_path.stat().st_size if target_path.exists() else None,
    }


class UploadGeoDTStructureVersionToKadiNode(BaseNode):
    type_name = "UploadGeoDTStructureVersionToKadi"

    REQUIRED_UPLOAD_NAMES = {
        "volume": "gempy_volume_with_topo.vtu",
        "south": "south.vtu",
        "north": "north.vtu",
        "bottom": "bottom.vtu",
        "top": "top.vtu",
        "west": "west.vtu",
        "east": "east.vtu",
        "side_mesh": "side_mesh.vtu",
        "full_shell": "full_shell.vtu",
    }

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            from kadi_apy import KadiManager
        except Exception as exc:
            raise NodeExecutionError("kadi_apy is not installed or not importable. Install requirements-gempy.txt and configure Kadi authentication.") from exc

        dry_run = _as_bool(params.get("dry_run"), False)
        force_upload = _as_bool(params.get("force_upload"), True)
        update_description_record = _as_bool(params.get("update_description_record"), True)
        add_group_roles = _as_bool(params.get("add_group_roles"), True)

        title = str(params.get("title") or "GeoDT Input Structure Model")
        description = str(params.get("description") or "Data from the structural model workflow, including the structural model, the total surfaces and all sub-surfaces.")
        subject = str(params.get("subject") or "Structure model in voxel grid from GemPy")
        collection_id = _as_int(params.get("collection_id"), 7238)
        description_record_id = _as_int(params.get("description_record_id"), 80295)
        version = str(params.get("version") or "0.1")
        tag = str(params.get("tag") or "geolab")

        if collection_id is None:
            raise NodeExecutionError("collection_id is required.")
        if update_description_record and description_record_id is None:
            raise NodeExecutionError("description_record_id is required when update_description_record is enabled.")

        upload_dir = make_runtime_path("geodt_kadi_structure_upload", ".dir")
        upload_dir.mkdir(parents=True, exist_ok=True)

        prepared_files: List[Dict[str, Any]] = []
        for input_name, target_name in self.REQUIRED_UPLOAD_NAMES.items():
            rv = _single(inputs, input_name)
            source_path = _runtime_file_from_input(rv, input_name)
            target_path = upload_dir / target_name
            prepared_files.append(_prepare_kadi_named_vtk_file(source_path, target_path, convert_to_vtu=True))

        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "prepared_directory": str(upload_dir),
            "required_naming": self.REQUIRED_UPLOAD_NAMES,
            "prepared_files": prepared_files,
            "record": None,
            "description_record_update": None,
            "uploaded_files": [],
            "notebook_settings": {
                "title": title,
                "description": description,
                "subject": subject,
                "collection_id": collection_id,
                "description_record_id": description_record_id,
                "version": version,
                "tag": tag,
                "dataset_category": "Mesh/CAD Data",
                "coverage": "GeoDT",
                "reference_system": "EPSG:25832 (UTM Zone 32N)",
                "format": "Visualization Toolkit (vtp, vtu, vti, ...)",
                "group_roles": {"143": "Admin", "302": "Editor", "122": "Member"} if add_group_roles else {},
            },
        }

        if dry_run:
            report["message"] = "Dry run only: files were prepared with the exact GeoDT/Kadi names, but no Kadi record was created and no files were uploaded."
            return {"report": RuntimeValue("report", report, name="geodt_kadi_upload_dry_run", preview=report, metadata=report)}

        with KadiManager() as manager:
            now = datetime.now()
            record = _kadi_create_geodt_structure_record(
                manager,
                now=now,
                title=title,
                description=description,
                subject=subject,
                collection_id=int(collection_id),
                version=version,
                tag=tag,
                add_group_roles=add_group_roles,
            )

            report["record"] = {
                "id": getattr(record, "id", None),
                "title": getattr(record, "title", None),
                "identifier": getattr(record, "identifier", None),
            }

            if update_description_record:
                description_rec = manager.record(id=int(description_record_id))
                description_rec.add_metadatum(
                    {
                        "key": "Record ID of the current version",
                        "type": "int",
                        "value": record.id,
                    },
                    force=True,
                )
                report["description_record_update"] = {
                    "description_record_id": int(description_record_id),
                    "metadata_key": "Record ID of the current version",
                    "value": record.id,
                }

            for item in prepared_files:
                path = Path(item["target"])
                response = record.upload_file(str(path), file_name=item["target_name"], force=force_upload)
                upload_info = dict(item)
                upload_info["response"] = str(response)
                report["uploaded_files"].append(upload_info)

        report["message"] = "GeoDT structure model version record created and all named VTU files uploaded to Kadi."
        return {"report": RuntimeValue("report", report, name="geodt_kadi_structure_version_upload", preview=report, metadata=report)}




class UploadFileToKadiNode(BaseNode):
    type_name = "UploadFileToKadi"

    def run(self, inputs: Dict[str, Any], params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, RuntimeValue]:
        try:
            from kadi_apy import KadiManager
        except Exception as exc:
            raise NodeExecutionError("kadi_apy is not installed or not importable. Install requirements-gempy.txt and configure Kadi authentication.") from exc
        rv = _single(inputs, "file")
        if rv.kind != "file":
            raise NodeExecutionError(f"UploadFileToKadi expects file input, got {rv.kind}")
        record_id = params.get("record_id")
        if not record_id:
            raise NodeExecutionError("record_id is required for Kadi upload.")
        path = Path(rv.value)
        file_name = params.get("file_name") or path.name
        force = _as_bool(params.get("force"), True)
        with KadiManager() as manager:
            record = manager.record(id=int(record_id))
            response = record.upload_file(str(path), file_name=str(file_name), force=force)
        report = {"uploaded": True, "record_id": int(record_id), "file_name": str(file_name), "response": str(response)}
        return {"report": RuntimeValue("report", report, name="kadi_upload_report", preview=report)}


def stratified_kmeans_sample(
    df: pd.DataFrame,
    group_col: str,
    feature_cols: Optional[List[str]] = None,
    n: int = 100,
    random_state: Optional[int] = None,
    allocation: str = "proportional",
    na_strategy: str = "median",
    scale: str = "none",
) -> pd.DataFrame:
    """Notebook-compatible stratified KMeans sampler.

    This intentionally follows the user's original notebook implementation, so
    the same data, n, allocation, random_state, na_strategy and scale produce the
    same selected rows as the notebook.
    """
    if n <= 0 or df.empty:
        return df.iloc[0:0].copy()
    if n >= len(df):
        return df.reset_index(drop=True)

    if allocation == "min_distance":
        if feature_cols is None:
            numeric_cols = df.select_dtypes(include=np.number).columns.tolist()
            coord_cols = [c for c in numeric_cols if c != group_col]
        else:
            coord_cols = [c for c in feature_cols if c in df.columns]
        if len(coord_cols) == 0:
            return df.sample(n=n, random_state=random_state).reset_index(drop=True)

        rng = np.random.RandomState(random_state)

        def _grid_key(pt: np.ndarray, inv_cell: np.ndarray) -> tuple:
            return tuple(np.floor(pt * inv_cell).astype(int).tolist())

        def _select_with_min_distance(X: np.ndarray, idx: np.ndarray, r: float) -> List[int]:
            if len(X) == 0:
                return []
            d = X.shape[1]
            cell = max(r / np.sqrt(d), 1e-12)
            inv_cell = np.full(d, 1.0 / cell)
            order = np.arange(len(X)); rng.shuffle(order)
            selected = []
            buckets = {}
            r2 = r * r
            offsets = np.array(np.meshgrid(*([[-1, 0, 1]] * d), indexing="ij")).reshape(d, -1).T
            for k in order:
                p = X[k]; key = _grid_key(p, inv_cell)
                ok = True
                for off in offsets:
                    nb_key = tuple((np.array(key) + off).tolist())
                    if nb_key not in buckets:
                        continue
                    for pos in buckets[nb_key]:
                        q = X[selected[pos]]
                        if np.dot(p - q, p - q) < r2:
                            ok = False; break
                    if not ok:
                        break
                if ok:
                    pos = len(selected)
                    selected.append(k)
                    buckets.setdefault(key, []).append(pos)
            return idx[selected].tolist()

        def _one_pass(min_dist: float) -> List[int]:
            chosen = []
            for _, g in df.groupby(group_col, sort=False):
                G = g[coord_cols].apply(pd.to_numeric, errors="coerce")
                if na_strategy == "drop":
                    valid = ~G.isna().any(axis=1)
                    g2 = g.loc[valid]; X = G.loc[valid].to_numpy(dtype=np.float64)
                elif na_strategy == "median":
                    G2 = G.fillna(G.median(numeric_only=True))
                    g2 = g.loc[G2.index]
                    X = G2.to_numpy(dtype=np.float64)
                else:
                    raise ValueError("na_strategy must be 'drop' or 'median'")
                if len(X) == 0:
                    continue
                idx = g2.index.to_numpy()
                chosen.extend(_select_with_min_distance(X, idx, min_dist))
            return chosen

        G_all = df[coord_cols].apply(pd.to_numeric, errors="coerce").dropna()
        if len(G_all) == 0:
            return df.iloc[0:0].copy()
        mins = G_all.min(axis=0).to_numpy(dtype=float)
        maxs = G_all.max(axis=0).to_numpy(dtype=float)
        diag = float(np.linalg.norm(maxs - mins))
        low, high = 0.0, max(diag, 1.0)

        mode = "closest"
        best_idx, best_diff = None, float("inf")

        for r0 in (low, high):
            idxs = _one_pass(r0); cnt = len(idxs)
            diff = abs(cnt - n)
            if mode == "at_most" and cnt > n:
                diff += 0.5
            if diff < best_diff:
                best_idx, best_diff = idxs, diff

        for _ in range(32):
            mid = (low + high) / 2.0
            idxs = _one_pass(mid); cnt = len(idxs)
            diff = abs(cnt - n)
            if mode == "at_most" and cnt > n:
                diff += 0.5
            if diff < best_diff or (diff == best_diff and mode == "at_most" and cnt <= n):
                best_idx, best_diff = idxs, diff

            if cnt > n:
                low = mid
            elif cnt < n:
                high = mid
            else:
                best_idx = idxs
                break
            if abs(high - low) < 1e-9:
                break

        if not best_idx:
            return df.iloc[0:0].copy()
        return df.loc[sorted(best_idx)].reset_index(drop=True)

    level_sizes = df[group_col].value_counts()
    levels = level_sizes.index.tolist()
    S = len(levels)
    if S == 0:
        return df.iloc[0:0].copy()

    def proportional_take():
        props = level_sizes / level_sizes.sum()
        raw = props * n
        floors = np.floor(raw).astype(int)
        remainder = n - floors.sum()
        frac = (raw - floors).sort_values(ascending=False)
        alloc = floors.copy()
        for lvl in frac.index[:remainder]:
            alloc[lvl] += 1
        take = alloc.clip(upper=level_sizes)
        leftover = n - int(take.sum())
        if leftover > 0:
            capacity = (level_sizes - take).to_dict()
            while leftover > 0:
                progressed = False
                for lvl in sorted(capacity, key=lambda x: capacity[x], reverse=True):
                    if capacity[lvl] > 0:
                        take[lvl] += 1
                        capacity[lvl] -= 1
                        leftover -= 1
                        progressed = True
                        if leftover == 0:
                            break
                if not progressed:
                    break
        return take.to_dict()

    def equal_take():
        base = n // S
        rem = n % S
        levels_by_size = level_sizes.sort_values(ascending=False).index.tolist()
        alloc = pd.Series({lvl: base for lvl in levels})
        for lvl in levels_by_size[:rem]:
            alloc[lvl] += 1
        take = alloc.clip(upper=level_sizes)
        leftover = n - int(take.sum())
        if leftover > 0:
            capacity = (level_sizes - take).to_dict()
            while leftover > 0:
                progressed = False
                for lvl in sorted(capacity, key=lambda x: capacity[x], reverse=True):
                    if capacity[lvl] > 0:
                        take[lvl] += 1
                        capacity[lvl] -= 1
                        leftover -= 1
                        progressed = True
                        if leftover == 0:
                            break
                if not progressed:
                    break
        return take.to_dict()

    take = equal_take() if allocation == "equal" else proportional_take()

    if feature_cols is None:
        numeric_cols = df.select_dtypes(include=np.number).columns.tolist()
        feats_global = [c for c in numeric_cols if c != group_col]
    else:
        feats_global = [c for c in feature_cols if c in df.columns]
    if len(feats_global) == 0:
        parts = []
        for lvl in levels:
            k_i = int(take.get(lvl, 0))
            if k_i <= 0:
                continue
            group_df = df[df[group_col] == lvl]
            parts.append(group_df if k_i >= len(group_df) else group_df.sample(n=k_i, random_state=random_state))
        return pd.concat(parts, ignore_index=True)

    rng = np.random.RandomState(random_state)
    parts = []

    # Match the original notebook exactly: KMeans is imported before the
    # n_init="auto" capability check.  In v10-v12 this import was missing at
    # this point, so the check always fell back to n_init=10, which changes the
    # selected representative points even with the same seed.
    from sklearn.cluster import KMeans

    try:
        _kmeans = KMeans(n_clusters=2, n_init="auto")
        _ = _kmeans.set_params(n_init="auto")
        n_init_param = "auto"
    except Exception:
        n_init_param = 10

    for lvl in levels:
        k_i = int(take.get(lvl, 0))
        if k_i <= 0:
            continue

        group_df = df[df[group_col] == lvl]
        Ni = len(group_df)
        if Ni == 0:
            continue
        if k_i >= Ni:
            parts.append(group_df)
            continue

        feats = [c for c in feats_global if c in group_df.columns]
        if len(feats) == 0:
            parts.append(group_df.sample(n=k_i, random_state=random_state))
            continue

        num = group_df[feats].apply(pd.to_numeric, errors="coerce")
        if na_strategy == "drop":
            valid_mask = ~num.isna().any(axis=1)
            group_df = group_df.loc[valid_mask]
            num = num.loc[valid_mask]
        elif na_strategy == "median":
            num = num.fillna(num.median(numeric_only=True))
        else:
            raise ValueError("na_strategy must be 'drop' or 'median'")

        if len(num) == 0:
            parts.append(group_df.sample(n=min(k_i, len(group_df)), random_state=random_state))
            continue

        X = num.to_numpy(dtype=np.float64)
        if scale == "standard":
            mu = X.mean(axis=0, keepdims=True)
            sigma = X.std(axis=0, ddof=0, keepdims=True)
            sigma[sigma == 0] = 1.0
            X_scaled = (X - mu) / sigma
        else:
            X_scaled = X

        k_eff = min(k_i, len(X_scaled))
        if k_eff == 1:
            km = KMeans(n_clusters=1, random_state=random_state, n_init=n_init_param).fit(X_scaled)
            center = km.cluster_centers_[0]
            d = np.linalg.norm(X_scaled - center, axis=1)
            chosen = group_df.index.to_numpy()[np.argmin(d)]
            parts.append(group_df.loc[[chosen]])
            continue

        km = KMeans(n_clusters=k_eff, random_state=random_state, n_init=n_init_param)
        labels = km.fit_predict(X_scaled)
        centers_local = km.cluster_centers_

        chosen_indices = []
        grp_idx = group_df.index.to_numpy()
        for j in range(k_eff):
            mask = (labels == j)
            if not np.any(mask):
                continue
            Xj = X_scaled[mask]
            dists = np.linalg.norm(Xj - centers_local[j], axis=1)
            chosen_idx = grp_idx[mask][np.argmin(dists)]
            chosen_indices.append(chosen_idx)

        if len(chosen_indices) < k_i:
            remaining = k_i - len(chosen_indices)
            pool = group_df.drop(index=chosen_indices, errors="ignore")
            add = pool.sample(n=min(remaining, len(pool)), random_state=random_state).index.tolist()
            chosen_indices.extend(add)

        parts.append(df.loc[sorted(set(chosen_indices))])

    return pd.concat(parts, ignore_index=True)


def stratified_sample(
    df: pd.DataFrame,
    group_col: str,
    feature_cols: Sequence[str],
    n: int,
    random_state: Optional[int],
    allocation: str,
) -> pd.DataFrame:
    return stratified_kmeans_sample(df, group_col, list(feature_cols), n, random_state, allocation)

def summarize_gempy_solution(solution: Any) -> Dict[str, Any]:
    report: Dict[str, Any] = {"type": type(solution).__name__}
    raw = getattr(solution, "raw_arrays", None)
    if raw is not None:
        raw_report = {}
        for name in dir(raw):
            if name.startswith("_"):
                continue
            try:
                value = getattr(raw, name)
            except Exception:
                continue
            if isinstance(value, np.ndarray):
                raw_report[name] = {"shape": list(value.shape), "dtype": str(value.dtype)}
        report["raw_arrays"] = raw_report
    return report


def extract_array_from_solution(solution: Any, output_name: str) -> np.ndarray:
    raw = getattr(solution, "raw_arrays", None)
    if raw is None:
        raise NodeExecutionError("Solution has no raw_arrays attribute.")
    if not hasattr(raw, output_name):
        available = [name for name in dir(raw) if not name.startswith("_")]
        raise NodeExecutionError(f"raw_arrays has no '{output_name}'. Available attributes include: {available[:30]}")
    arr = getattr(raw, output_name)
    if not isinstance(arr, np.ndarray):
        arr = np.asarray(arr)
    return arr
