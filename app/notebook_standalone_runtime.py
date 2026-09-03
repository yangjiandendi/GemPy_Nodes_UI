"""Standalone helper functions embedded into exported Jupyter notebooks.

This module deliberately has no dependency on the GemPy Node Editor runtime.
The exporter inserts this source code directly into the generated notebook.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


def parse_json(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return default


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return default


def infer_file_type(path: Path, requested: str = "auto") -> str:
    requested = str(requested or "auto").lower()
    if requested != "auto":
        return requested
    suffix = path.suffix.lower()
    mapping = {
        ".csv": "csv", ".xlsx": "excel", ".xls": "excel",
        ".vtk": "mesh", ".vtp": "mesh", ".vtu": "mesh",
        ".vti": "mesh", ".stl": "mesh", ".ply": "mesh",
        ".obj": "mesh", ".npy": "npy", ".npz": "npz",
        ".json": "json", ".txt": "text",
    }
    return mapping.get(suffix, "file")


def load_input_file(path: Path, file_type: str = "auto", sheet_name: str = "") -> Dict[str, Any]:
    """Load a normal input file and return its available Python representations."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    kind = infer_file_type(path, file_type)
    result: Dict[str, Any] = {"file": path, "file_type": kind}

    if kind == "csv":
        result["table"] = pd.read_csv(path)
    elif kind in {"xlsx", "excel"}:
        result["table"] = pd.read_excel(path, sheet_name=sheet_name or 0)
    elif kind in {"mesh", "vtk", "vtp", "vtu", "vti", "stl", "ply", "obj"}:
        import pyvista as pv
        result["mesh"] = pv.read(path)
    elif kind == "npy":
        result["array"] = np.load(path, allow_pickle=False)
    elif kind == "npz":
        result["array"] = np.load(path, allow_pickle=False)
    elif kind == "json":
        result["json"] = json.loads(path.read_text(encoding="utf-8"))
    elif kind == "text":
        result["text"] = path.read_text(encoding="utf-8")
    return result


def preview_loaded_data(loaded: Dict[str, Any], rows: int = 10) -> None:
    from IPython.display import display
    if "table" in loaded:
        table = loaded["table"]
        display(table.head(rows))
        print(f"Table shape: {table.shape}")
    elif "mesh" in loaded:
        mesh = loaded["mesh"]
        print(mesh)
        try:
            mesh.plot(jupyter_backend="static", show_edges=True)
        except Exception:
            pass
    elif "array" in loaded:
        array = np.asarray(loaded["array"])
        print(f"Array shape={array.shape}, dtype={array.dtype}")
        display(array if array.size <= 100 else array.reshape(-1)[:100])
    else:
        print(loaded)


def merge_tables(
    tables: Sequence[pd.DataFrame],
    axis: int = 0,
    ignore_index: bool = True,
    add_source_column: bool = False,
    source_column: str = "_merge_source",
    source_names: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    prepared = []
    for index, table in enumerate(tables):
        frame = table.copy()
        if add_source_column:
            name = source_names[index] if source_names and index < len(source_names) else f"input_{index}"
            frame[source_column] = name
        prepared.append(frame)
    return pd.concat(prepared, axis=int(axis), ignore_index=bool(ignore_index))


def validate_geological_table(table: pd.DataFrame, table_kind: str = "auto") -> Dict[str, Any]:
    columns_lower = {str(column).strip().lower() for column in table.columns}
    if table_kind == "auto":
        has_gradient = {"g_x", "g_y", "g_z"}.issubset(columns_lower) or {"gx", "gy", "gz"}.issubset(columns_lower)
        has_angles = {"azimuth", "dip"}.issubset(columns_lower)
        table_kind = "orientations" if has_gradient or has_angles else "surface_points"

    required = {"x", "y", "z", "formation"}
    missing = sorted(required - columns_lower)
    report = {
        "table_kind": table_kind,
        "rows": int(len(table)),
        "columns": [str(column) for column in table.columns],
        "missing_required_columns": missing,
        "null_counts": {str(column): int(table[column].isna().sum()) for column in table.columns},
        "valid": not missing,
    }
    if table_kind == "orientations":
        report["has_gradient_vectors"] = {"g_x", "g_y", "g_z"}.issubset(columns_lower) or {"gx", "gy", "gz"}.issubset(columns_lower)
        report["has_dip_azimuth"] = {"azimuth", "dip"}.issubset(columns_lower)
        report["valid"] = report["valid"] and (report["has_gradient_vectors"] or report["has_dip_azimuth"])
    if not report["valid"]:
        raise ValueError(f"Invalid geological table: {report}")
    return report


def _find_column(table: pd.DataFrame, requested: str, candidates: Sequence[str]) -> Optional[str]:
    if requested and requested != "auto" and requested in table.columns:
        return requested
    lookup = {str(column).strip().lower(): str(column) for column in table.columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def convert_orientation_angles(
    table: pd.DataFrame,
    azimuth_col: str = "auto",
    dip_col: str = "auto",
    polarity_col: str = "auto",
    azimuth_is_dip_direction: bool = True,
    normalize_vectors: bool = True,
    overwrite_existing_gradients: bool = False,
    drop_angle_columns: bool = False,
) -> pd.DataFrame:
    frame = table.copy()
    azimuth_name = _find_column(frame, azimuth_col, ["azimuth", "azi", "dip_direction"])
    dip_name = _find_column(frame, dip_col, ["dip", "inclination"])
    polarity_name = _find_column(frame, polarity_col, ["polarity", "orientation_polarity"])
    if not azimuth_name or not dip_name:
        raise ValueError("Azimuth and dip columns are required.")

    azimuth = np.deg2rad(pd.to_numeric(frame[azimuth_name], errors="coerce").to_numpy(float))
    dip = np.deg2rad(pd.to_numeric(frame[dip_name], errors="coerce").to_numpy(float))
    if not azimuth_is_dip_direction:
        azimuth = azimuth + np.pi / 2.0
    polarity = np.ones(len(frame), dtype=float)
    if polarity_name:
        polarity = pd.to_numeric(frame[polarity_name], errors="coerce").fillna(1.0).to_numpy(float)

    vectors = np.column_stack((
        np.sin(dip) * np.sin(azimuth),
        np.sin(dip) * np.cos(azimuth),
        np.cos(dip),
    )) * polarity[:, None]
    if normalize_vectors:
        norms = np.linalg.norm(vectors, axis=1)
        valid = norms > 0
        vectors[valid] /= norms[valid, None]

    for index, name in enumerate(["G_x", "G_y", "G_z"]):
        if overwrite_existing_gradients or name not in frame.columns:
            frame[name] = vectors[:, index]
        else:
            existing = pd.to_numeric(frame[name], errors="coerce")
            frame[name] = existing.where(existing.notna(), vectors[:, index])

    if drop_angle_columns:
        frame = frame.drop(columns=[name for name in [azimuth_name, dip_name, polarity_name] if name], errors="ignore")
    return frame


def clean_geological_table(
    table: pd.DataFrame,
    coord_cols: str = "X,Y,Z",
    grad_cols: str = "G_x,G_y,G_z",
    formation_col: str = "formation",
    formation_to_str: bool = True,
    numeric_xyz: bool = True,
    numeric_gradients: bool = True,
    drop_missing_xyz: bool = True,
    remove_zero_gradients: bool = True,
) -> pd.DataFrame:
    frame = table.copy()
    coordinates = [value.strip() for value in str(coord_cols).split(",") if value.strip()]
    gradients = [value.strip() for value in str(grad_cols).split(",") if value.strip()]
    if formation_to_str and formation_col in frame.columns:
        frame[formation_col] = frame[formation_col].where(frame[formation_col].isna(), frame[formation_col].astype(str))
    if numeric_xyz:
        for column in coordinates:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if numeric_gradients:
        for column in gradients:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if drop_missing_xyz:
        frame = frame.dropna(subset=[column for column in coordinates if column in frame.columns])
    if remove_zero_gradients and all(column in frame.columns for column in gradients):
        norm = np.sqrt(sum(np.square(frame[column].fillna(0.0)) for column in gradients))
        frame = frame.loc[norm > 0].copy()
    return frame.reset_index(drop=True)


def sample_geological_table(
    table: pd.DataFrame,
    n: int = 100,
    group_col: str = "formation",
    feature_cols: str = "X,Y,Z",
    allocation: str = "equal",
    method: str = "fast",
    random_state: int = 42,
) -> pd.DataFrame:
    frame = table.copy()
    n = min(max(int(n), 1), len(frame))
    if n >= len(frame):
        return frame.reset_index(drop=True)
    rng = np.random.default_rng(int(random_state))
    if group_col not in frame.columns:
        return frame.iloc[np.sort(rng.choice(len(frame), n, replace=False))].reset_index(drop=True)

    groups = list(frame.groupby(group_col, dropna=False))
    if allocation == "proportional":
        allocations = [max(1, round(n * len(group) / len(frame))) for _, group in groups]
    else:
        base = max(1, n // max(len(groups), 1))
        allocations = [base for _ in groups]
    while sum(allocations) > n:
        allocations[int(np.argmax(allocations))] -= 1
    while sum(allocations) < n:
        allocations[int(np.argmax([len(group) - allocations[index] for index, (_, group) in enumerate(groups)]))] += 1

    selected = []
    for allocation_count, (_, group) in zip(allocations, groups):
        count = min(max(allocation_count, 0), len(group))
        if count:
            selected.append(group.sample(n=count, random_state=int(random_state)))
    return pd.concat(selected, ignore_index=True) if selected else frame.iloc[:0].copy()


def _normalize_gempy_table(table: pd.DataFrame, kind: str) -> pd.DataFrame:
    frame = table.copy()
    rename = {}
    for column in frame.columns:
        lower = str(column).strip().lower()
        if lower == "x": rename[column] = "X"
        elif lower == "y": rename[column] = "Y"
        elif lower == "z": rename[column] = "Z"
        elif lower in {"formation", "surface", "element"}: rename[column] = "formation"
        elif lower in {"gx", "g_x"}: rename[column] = "G_x"
        elif lower in {"gy", "g_y"}: rename[column] = "G_y"
        elif lower in {"gz", "g_z"}: rename[column] = "G_z"
    frame = frame.rename(columns=rename)
    for column in ["X", "Y", "Z"]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "formation" in frame.columns:
        frame["formation"] = frame["formation"].where(frame["formation"].isna(), frame["formation"].astype(str))
    required = ["X", "Y", "Z", "formation"]
    frame = frame.dropna(subset=[column for column in required if column in frame.columns])
    return frame


def auto_extent_from_tables(surface_points: pd.DataFrame, orientations: pd.DataFrame, padding_percent: float = 5.0) -> List[float]:
    coords = pd.concat([
        surface_points[["X", "Y", "Z"]],
        orientations[["X", "Y", "Z"]],
    ], ignore_index=True)
    mins = coords.min().to_numpy(float)
    maxs = coords.max().to_numpy(float)
    spans = np.maximum(maxs - mins, 1.0)
    padding = spans * float(padding_percent) / 100.0
    return [
        float(mins[0] - padding[0]), float(maxs[0] + padding[0]),
        float(mins[1] - padding[1]), float(maxs[1] + padding[1]),
        float(mins[2] - padding[2]), float(maxs[2] + padding[2]),
    ]


def create_gempy_model(
    surface_points: pd.DataFrame,
    orientations: pd.DataFrame,
    project_name: str = "GemPy Model",
    extent: Any = None,
    refinement: int = 3,
    resolution: Any = None,
    auto_extent_from_data: bool = True,
    extent_user_overridden: bool = False,
    extent_padding_percent: float = 5.0,
    apply_surface_point_nugget: bool = True,
    surface_point_nugget: float = 0.01,
    working_dir: Path = Path("outputs"),
):
    import gempy as gp
    surface = _normalize_gempy_table(surface_points, "surface")
    orientation = _normalize_gempy_table(orientations, "orientation")
    validate_geological_table(surface, "surface_points")
    validate_geological_table(orientation, "orientations")

    if auto_extent_from_data and not extent_user_overridden:
        final_extent = auto_extent_from_tables(surface, orientation, extent_padding_percent)
    else:
        final_extent = parse_json(extent, extent)
        if not isinstance(final_extent, (list, tuple)) or len(final_extent) != 6:
            raise ValueError("extent must contain six values.")
        final_extent = [float(value) for value in final_extent]

    working_dir = Path(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    surface_path = working_dir / "surface_points_for_gempy.csv"
    orientation_path = working_dir / "orientations_for_gempy.csv"
    surface.to_csv(surface_path, index=False)
    orientation.to_csv(orientation_path, index=False)

    kwargs = {
        "project_name": project_name,
        "extent": final_extent,
        "importer_helper": gp.data.ImporterHelper(
            path_to_orientations=str(orientation_path),
            path_to_surface_points=str(surface_path),
        ),
    }
    parsed_resolution = parse_json(resolution, resolution)
    if parsed_resolution:
        kwargs["resolution"] = [int(value) for value in parsed_resolution]
    else:
        kwargs["refinement"] = int(refinement)
    geo_model = gp.create_geomodel(**kwargs)
    if apply_surface_point_nugget:
        gp.modify_surface_points(geo_model=geo_model, nugget=float(surface_point_nugget))
    return geo_model


def structural_group_summary(geo_model: Any) -> List[Dict[str, Any]]:
    result = []
    for index, group in enumerate(geo_model.structural_frame.structural_groups):
        relation = getattr(group, "structural_relation", None)
        result.append({
            "index": index,
            "name": str(getattr(group, "name", f"group_{index}")),
            "relation": str(getattr(relation, "name", relation)),
            "elements": [str(getattr(element, "name", element)) for element in getattr(group, "elements", [])],
        })
    return result


def configure_structural_frame(
    geo_model: Any,
    mapping_json: Any = None,
    groups_json: Any = None,
    anisotropy: str = "NONE",
    remove_default_formation: bool = True,
    fault_relations_json: Any = None,
):
    import gempy as gp
    if str(anisotropy).upper() == "NONE":
        geo_model.input_transform.apply_anisotropy(gp.data.GlobalAnisotropy.NONE)
    mapping = parse_json(mapping_json, {}) or {}
    groups = parse_json(groups_json, []) or []
    relation_type = gp.data.StackRelationType
    relation_by_name = {
        str(group.get("name")): str(group.get("relation", "ERODE")).upper()
        for group in groups if isinstance(group, dict) and group.get("name")
    }

    if mapping:
        gp.map_stack_to_surfaces(gempy_model=geo_model, mapping_object=mapping)
        mapped_names = set(map(str, mapping.keys()))
        for group in geo_model.structural_frame.structural_groups:
            name = str(getattr(group, "name", ""))
            if name in mapped_names:
                relation_name = relation_by_name.get(name, "ERODE")
                group.structural_relation = getattr(relation_type, relation_name)
        extra_index = len(mapping)
        for group_cfg in groups:
            name = str(group_cfg.get("name", "")).strip()
            if not name or name in mapped_names:
                continue
            elements = [geo_model.structural_frame.get_element_by_name(str(value)) for value in group_cfg.get("elements", [])]
            if elements:
                relation_name = str(group_cfg.get("relation", "ERODE")).upper()
                gp.add_structural_group(
                    model=geo_model,
                    group_index=extra_index,
                    structural_group_name=name,
                    elements=elements,
                    structural_relation=getattr(relation_type, relation_name),
                )
                extra_index += 1
    else:
        for index, group_cfg in enumerate(groups):
            name = str(group_cfg.get("name", "")).strip()
            element_names = group_cfg.get("elements", [])
            if isinstance(element_names, str):
                element_names = [value.strip() for value in element_names.split(",") if value.strip()]
            if not name or not element_names:
                continue
            elements = [geo_model.structural_frame.get_element_by_name(str(value)) for value in element_names]
            relation_name = str(group_cfg.get("relation", "ERODE")).upper()
            gp.add_structural_group(
                model=geo_model,
                group_index=int(group_cfg.get("index", index)),
                structural_group_name=name,
                elements=elements,
                structural_relation=getattr(relation_type, relation_name),
            )
    if remove_default_formation:
        try:
            gp.remove_structural_group_by_name(model=geo_model, group_name="default_formation")
        except Exception:
            pass

    # Apply the full fault-relation matrix after the final group order is known.
    # Assigning individual entries would first invoke GemPy's property getter,
    # which can reject a FAULT group before an explicit matrix has been set.
    cfg = parse_json(fault_relations_json, {}) or {}
    if as_bool(cfg.get("enabled"), False):
        groups_now = list(geo_model.structural_frame.structural_groups)
        group_names = [str(getattr(group, "name", index)) for index, group in enumerate(groups_now)]
        name_to_index = {name: index for index, name in enumerate(group_names)}
        group_count = len(group_names)
        matrix = np.zeros((group_count, group_count), dtype=int)

        # Preserve compatibility with projects that store a complete raw matrix.
        raw_matrix = cfg.get("matrix")
        if isinstance(raw_matrix, list) and len(raw_matrix) == group_count:
            candidate = np.asarray(raw_matrix, dtype=int)
            if candidate.shape != (group_count, group_count):
                raise ValueError(
                    f"Fault relation matrix must be {group_count} x {group_count}, got {candidate.shape}."
                )
            matrix = (candidate != 0).astype(int)
        else:
            relations = cfg.get("relations") or []
            if not isinstance(relations, list):
                raise ValueError("Fault relations must be a list.")
            for relation in relations:
                if not isinstance(relation, dict):
                    continue
                active = as_bool(relation.get("active", relation.get("value", True)), True)
                if not active:
                    continue
                source = str(relation.get("from") or relation.get("source") or "").strip()
                target = str(relation.get("to") or relation.get("target") or "").strip()
                if source not in name_to_index or target not in name_to_index or source == target:
                    continue
                matrix[name_to_index[source], name_to_index[target]] = 1

        try:
            geo_model.structural_frame.fault_relations = matrix
        except Exception:
            # Some GemPy releases require a boolean matrix.
            geo_model.structural_frame.fault_relations = matrix.astype(bool)
    return geo_model


def compute_gempy_model(
    geo_model: Any,
    backend: str = "PYTORCH",
    dtype: str = "float64",
    mesh_extraction: bool = False,
    retry_without_mesh_extraction: bool = True,
):
    import gempy as gp
    backend_value = None
    for candidate in [backend, str(backend).upper(), str(backend).lower()]:
        if hasattr(gp.data.AvailableBackends, candidate):
            backend_value = getattr(gp.data.AvailableBackends, candidate)
            break
    if backend_value is None:
        raise ValueError(f"Unknown GemPy backend: {backend}")
    options = getattr(geo_model, "interpolation_options", None)
    if options is not None and hasattr(options, "mesh_extraction"):
        options.mesh_extraction = bool(mesh_extraction)
    config = gp.data.GemPyEngineConfig(backend=backend_value, dtype=dtype)
    try:
        return gp.compute_model(geo_model, engine_config=config)
    except Exception:
        if not (mesh_extraction and retry_without_mesh_extraction and options is not None and hasattr(options, "mesh_extraction")):
            raise
        options.mesh_extraction = False
        return gp.compute_model(geo_model, engine_config=config)


def summarize_gempy_solution(solution: Any) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"type": type(solution).__name__}
    for name in ["lith_block", "block_matrix", "scalar_field_matrix", "values_matrix"]:
        try:
            value = getattr(solution, name)
            if value is not None:
                array = np.asarray(value)
                summary[name] = {"shape": list(array.shape), "dtype": str(array.dtype)}
        except Exception:
            pass
    return summary



def _vtk_faces_from_gempy_faces(faces: Any) -> Optional[np.ndarray]:
    """Convert common GemPy triangle/face arrays to a VTK PolyData cell array."""
    if faces is None:
        return None

    try:
        array = np.asarray(faces)
    except Exception:
        return None

    if array.size == 0:
        return None

    if array.ndim == 2:
        if array.shape[1] == 3:
            return np.hstack(
                [
                    np.full((array.shape[0], 1), 3, dtype=np.int64),
                    array.astype(np.int64),
                ]
            ).ravel()

        if array.shape[1] == 4:
            if np.all(array[:, 0] == 3):
                return array.astype(np.int64).ravel()

            return np.hstack(
                [
                    np.full((array.shape[0], 1), 4, dtype=np.int64),
                    array.astype(np.int64),
                ]
            ).ravel()

    vtk_cells: List[int] = []
    try:
        for face in faces:
            point_ids = [int(value) for value in face]
            if len(point_ids) >= 3:
                vtk_cells.extend([len(point_ids), *point_ids])
    except Exception:
        return None

    if not vtk_cells:
        return None

    return np.asarray(vtk_cells, dtype=np.int64)


def gempy_element_to_polydata(
    element: Any,
    element_id: int = 0,
) -> Optional[Any]:
    """Convert one computed GemPy structural element into PyVista PolyData."""
    import pyvista as pv

    vertices = getattr(element, "vertices", None)
    if vertices is None:
        return None

    try:
        vertices = np.asarray(vertices, dtype=float)
    except Exception:
        return None

    if (
        vertices.ndim != 2
        or vertices.shape[0] == 0
        or vertices.shape[1] != 3
    ):
        return None

    vtk_faces = None
    for attribute in ["edges", "simplices", "faces", "triangles"]:
        vtk_faces = _vtk_faces_from_gempy_faces(
            getattr(element, attribute, None)
        )
        if vtk_faces is not None:
            break

    if vtk_faces is not None:
        mesh = pv.PolyData(vertices, vtk_faces)
    else:
        # Some GemPy versions expose vertices without a common triangle array.
        # Keep those data visible as a point cloud instead of dropping them.
        mesh = pv.PolyData(vertices)
        try:
            mesh.verts = np.hstack(
                [
                    np.ones((vertices.shape[0], 1), dtype=np.int64),
                    np.arange(
                        vertices.shape[0],
                        dtype=np.int64,
                    ).reshape(-1, 1),
                ]
            )
        except Exception:
            pass

    element_name = str(
        getattr(element, "name", f"element_{element_id}")
    )

    if mesh.n_cells:
        mesh.cell_data["element_id"] = np.full(
            mesh.n_cells,
            int(element_id),
            dtype=np.int32,
        )

    if mesh.n_points:
        mesh.point_data["element_id"] = np.full(
            mesh.n_points,
            int(element_id),
            dtype=np.int32,
        )

    try:
        mesh.field_data["element_name"] = np.asarray(
            [element_name]
        )
    except Exception:
        pass

    return mesh


def extract_gempy_surface_mesh(
    geo_model: Any,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Extract and combine computed GemPy structural surfaces.

    The returned mesh is a normal PyVista object and no longer depends on
    GemPy Viewer or the node editor.
    """
    import pyvista as pv

    report: Dict[str, Any] = {
        "source": "geo_model.structural_frame.structural_elements",
        "elements": [],
        "mesh_count": 0,
        "combined_cells": 0,
        "combined_points": 0,
        "message": "",
    }

    try:
        elements = list(
            getattr(
                geo_model.structural_frame,
                "structural_elements",
                [],
            )
            or []
        )
    except Exception:
        elements = []

    meshes = []

    for element_id, element in enumerate(elements):
        element_name = str(
            getattr(element, "name", f"element_{element_id}")
        )
        vertices = getattr(element, "vertices", None)

        try:
            number_of_vertices = (
                int(len(vertices))
                if vertices is not None
                else 0
            )
        except Exception:
            number_of_vertices = 0

        mesh = gempy_element_to_polydata(
            element,
            element_id=element_id,
        )

        element_report = {
            "id": int(element_id),
            "name": element_name,
            "vertices": number_of_vertices,
            "exported": bool(
                mesh is not None
                and getattr(mesh, "n_points", 0) > 0
            ),
        }

        if mesh is not None and mesh.n_points:
            element_report["cells"] = int(mesh.n_cells)
            element_report["points"] = int(mesh.n_points)
            meshes.append(mesh)

        report["elements"].append(element_report)

    if not meshes:
        report["message"] = (
            "No computed GemPy surface mesh was found. Run Compute GemPy Model "
            "with surface mesh extraction enabled before Plot GemPy 3D."
        )
        return None, report

    combined = meshes[0]

    for mesh in meshes[1:]:
        try:
            combined = combined.merge(
                mesh,
                merge_points=False,
            )
        except Exception:
            combined = pv.MultiBlock(
                [combined, mesh]
            ).combine()

    try:
        combined = combined.clean(tolerance=0.0)
    except Exception:
        pass

    report["mesh_count"] = int(len(meshes))
    report["combined_cells"] = int(combined.n_cells)
    report["combined_points"] = int(combined.n_points)
    report["message"] = (
        "Computed GemPy structural surfaces were extracted as a standalone "
        "PyVista mesh."
    )

    return combined, report


def export_gempy_surface_mesh(
    geo_model: Any,
    output_path: Path,
) -> Tuple[Optional[Any], Optional[Path], Dict[str, Any]]:
    """Extract GemPy surfaces and save them as VTP, VTU, or legacy VTK."""
    mesh, report = extract_gempy_surface_mesh(geo_model)

    if mesh is None:
        return None, None, report

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    suffix = output_path.suffix.lower()
    if suffix == ".vtu":
        mesh_to_save = mesh.cast_to_unstructured_grid()
    elif suffix == ".vtp":
        mesh_to_save = mesh.extract_surface()
    elif suffix == ".vtk":
        mesh_to_save = mesh
    else:
        output_path = output_path.with_suffix(".vtp")
        mesh_to_save = mesh.extract_surface()

    mesh_to_save.save(output_path)

    report.update(
        {
            "file": str(output_path),
            "format": output_path.suffix.lower(),
            "saved": True,
        }
    )

    return mesh, output_path, report

def plot_gempy_3d(
    geo_model: Any,
    show_data: bool = True,
    show_lith: bool = True,
    show_surfaces: bool = True,
    show_topography: bool = True,
    show_boundaries: bool = True,
):
    import gempy_viewer as gpv
    kwargs = {
        "show_data": bool(show_data),
        "show_lith": bool(show_lith),
        "show_surfaces": bool(show_surfaces),
        "show_topography": bool(show_topography),
        "show_boundaries": bool(show_boundaries),
    }
    try:
        viewer = gpv.plot_3d(geo_model, show=True, **kwargs)
    except TypeError:
        kwargs.pop("show_boundaries", None)
        viewer = gpv.plot_3d(geo_model, show=True, **kwargs)
    return viewer


def plot_gempy_2d(
    geo_model: Any,
    direction: str = "z",
    cell_number: Optional[int] = None,
    show_data: bool = True,
    show_lith: bool = True,
    show_boundaries: bool = True,
    show_scalar: bool = False,
    dpi: int = 160,
    output_path: Optional[Path] = None,
):
    import matplotlib.pyplot as plt
    import gempy_viewer as gpv
    kwargs = {
        "direction": direction,
        "show_data": bool(show_data),
        "show_lith": bool(show_lith),
        "show_boundaries": bool(show_boundaries),
        "show_scalar": bool(show_scalar),
    }
    # Empty node-editor number fields may arrive as empty strings.
    normalized_cell_number = cell_number
    if isinstance(normalized_cell_number, str):
        normalized_cell_number = normalized_cell_number.strip()
        if normalized_cell_number.lower() in {"", "none", "null", "nan"}:
            normalized_cell_number = None

    if normalized_cell_number is not None:
        try:
            kwargs["cell_number"] = int(float(normalized_cell_number))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cell_number must be empty/None or a valid integer, "
                f"got {cell_number!r}."
            ) from exc
    try:
        result = gpv.plot_2d(geo_model, **kwargs)
    except TypeError:
        kwargs.pop("show_scalar", None)
        result = gpv.plot_2d(geo_model, **kwargs)
    figure = getattr(result, "fig", None) or plt.gcf()
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output_path, dpi=int(dpi), bbox_inches="tight")
    return result, figure


def extract_gempy_array(solution: Any, output_name: str = "lith_block", reshape: Any = None) -> np.ndarray:
    candidates = [solution, getattr(solution, "raw_arrays", None), getattr(solution, "solutions", None)]
    value = None
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, dict) and output_name in candidate:
            value = candidate[output_name]
            break
        if hasattr(candidate, output_name):
            value = getattr(candidate, output_name)
            break
    if value is None:
        raise AttributeError(f"GemPy solution has no output named {output_name!r}")
    array = np.asarray(value)
    parsed_reshape = parse_json(reshape, reshape)
    if parsed_reshape:
        array = array.reshape(tuple(int(value) for value in parsed_reshape))
    return array


def _visualization_scalar_name(mesh: Any, requested: str = "auto") -> str:
    cell_keys = [str(key) for key in getattr(mesh, "cell_data", {}).keys()]
    point_keys = [str(key) for key in getattr(mesh, "point_data", {}).keys()]
    keys = cell_keys + point_keys
    requested_text = str(requested or "auto").strip()
    if requested_text.lower() not in {"", "auto", "none"}:
        for key in keys:
            if key == requested_text or key.lower() == requested_text.lower():
                return key
        return ""
    for preferred in ["MaterialIDs", "MaterialID", "material_ids", "material_id", "id", "lith_block", "lithology", "layer", "layer_id", "BoundaryID"]:
        if preferred in keys:
            return preferred
    return keys[0] if keys else ""


def _filter_visualization_mesh(mesh: Any, scalar_name: str, raw_values: Any) -> tuple[Any, dict]:
    if isinstance(raw_values, (list, tuple, np.ndarray)):
        tokens = list(np.asarray(raw_values).ravel())
    else:
        tokens = [token.strip() for token in str(raw_values or "").replace(";", ",").split(",") if token.strip()]
    if not tokens:
        raise ValueError("Scalar-value filtering is enabled, but no Scalar values to show were provided.")
    try:
        selected_values = [float(token) for token in tokens]
    except (TypeError, ValueError) as exc:
        raise ValueError("Scalar values to show must be numeric and comma-separated, for example 2 or 2,5,7.") from exc
    if not scalar_name:
        available = list(getattr(mesh, "cell_data", {}).keys()) + list(getattr(mesh, "point_data", {}).keys())
        raise ValueError(f"The selected visualization scalar was not found. Available scalars: {available}")

    if scalar_name in getattr(mesh, "cell_data", {}):
        association = "cell"
        source_values = np.asarray(mesh.cell_data[scalar_name])
    else:
        association = "point"
        source_values = np.asarray(mesh.point_data[scalar_name])
    if source_values.ndim != 1:
        raise ValueError(f"Scalar-value filtering needs a one-component array; {scalar_name!r} has shape {source_values.shape}.")
    numeric_values = source_values.astype(float)
    mask = np.zeros(numeric_values.shape, dtype=bool)
    for selected in selected_values:
        mask |= np.isclose(numeric_values, selected, rtol=1e-9, atol=1e-9)
    if not np.any(mask):
        raise ValueError(
            f"No {association}s have {scalar_name} in {selected_values}. "
            f"Available values (first 30): {np.unique(numeric_values)[:30].tolist()}"
        )
    if association == "cell":
        filtered = mesh.extract_cells(np.flatnonzero(mask))
    else:
        filtered = mesh.extract_points(np.flatnonzero(mask), adjacent_cells=False, include_cells=False)
    return filtered, {
        "scalar": scalar_name,
        "values": selected_values,
        "association": association,
        "input_cells": int(getattr(mesh, "n_cells", 0)),
        "display_cells": int(getattr(filtered, "n_cells", 0)),
        "display_points": int(getattr(filtered, "n_points", 0)),
    }


def visualize(
    value: Any,
    scalars: str = "auto",
    show_edges: bool = True,
    rows: int = 15,
    filter_by_scalar_values: bool = False,
    scalar_filter_values: Any = "",
) -> Any:
    from IPython.display import display
    if isinstance(value, pd.DataFrame):
        display(value.head(int(rows)))
        print(f"Shape: {value.shape}")
        return value
    if isinstance(value, np.ndarray):
        print(f"Array shape={value.shape}, dtype={value.dtype}")
        display(value if value.size <= 100 else value.reshape(-1)[:100])
        return value
    if hasattr(value, "plot") and hasattr(value, "n_points"):
        scalar_name = _visualization_scalar_name(value, scalars)
        if bool(filter_by_scalar_values):
            value, filter_report = _filter_visualization_mesh(value, scalar_name, scalar_filter_values)
            print(
                f"Display-only filter: {filter_report['scalar']} = {filter_report['values']}; "
                f"showing {filter_report['display_cells']} cells and {filter_report['display_points']} points."
            )
        try:
            return value.plot(scalars=scalar_name or None, show_edges=bool(show_edges), jupyter_backend="static")
        except Exception:
            return value.plot(scalars=scalar_name or None, show_edges=bool(show_edges))
    display(value)
    return value


def combine_meshes(meshes: Sequence[Any], merge_points: bool = False, clean_output: bool = True):
    import pyvista as pv
    if not meshes:
        raise ValueError("At least one mesh is required.")
    result = meshes[0].copy(deep=True)
    for mesh in meshes[1:]:
        result = result.merge(mesh, merge_points=bool(merge_points))
    if clean_output:
        try:
            result = result.clean()
        except Exception:
            pass
    return result


def thicken_mesh(mesh: Any, distance: float, mode: str = "symmetric", close_sides: bool = True):
    """Create a simple normal-offset shell from a surface mesh."""
    import pyvista as pv
    surface = mesh.extract_surface().triangulate().compute_normals(
        point_normals=True,
        cell_normals=False,
        consistent_normals=True,
        auto_orient_normals=True,
        inplace=False,
    )
    normals = np.asarray(surface.point_data["Normals"])
    if mode == "outward":
        inner_points = np.asarray(surface.points)
        outer_points = inner_points + normals * float(distance)
    elif mode == "inward":
        outer_points = np.asarray(surface.points)
        inner_points = outer_points - normals * float(distance)
    else:
        inner_points = np.asarray(surface.points) - normals * float(distance) / 2.0
        outer_points = np.asarray(surface.points) + normals * float(distance) / 2.0
    inner = surface.copy(deep=True); inner.points = inner_points
    outer = surface.copy(deep=True); outer.points = outer_points
    shell = inner.merge(outer, merge_points=False)
    if close_sides:
        edges = surface.extract_feature_edges(boundary_edges=True, feature_edges=False, manifold_edges=False, non_manifold_edges=False)
        if edges.n_cells:
            # PyVista's extrusion gives a robust side wall for the boundary edges.
            walls = edges.extrude([0, 0, 0], capping=False)
            shell = shell.merge(walls, merge_points=False)
    return shell.clean()



def offset_topography_vertical(
    topography_mesh: Any,
    thickness: float,
    triangulate: bool = True,
    clean: bool = False,
):
    """Create Z_cutoff(X,Y) = Z_DEM(X,Y) - thickness."""
    d = float(thickness)
    if not np.isfinite(d) or d <= 0.0:
        raise ValueError("Top-layer thickness must be a finite number greater than zero.")
    try:
        surface = topography_mesh.extract_surface()
    except Exception:
        surface = topography_mesh.copy(deep=True)
    if triangulate:
        try:
            surface = surface.triangulate()
        except Exception:
            pass
    lowered = surface.copy(deep=True)
    original_points = np.asarray(surface.points, dtype=float)
    if original_points.ndim != 2 or original_points.shape[1] < 3:
        raise ValueError("DEM/topography mesh does not contain XYZ points.")
    lowered_points = original_points.copy()
    lowered_points[:, 2] -= d
    lowered.points = lowered_points
    updated_arrays = []
    for key in list(lowered.point_data.keys()):
        try:
            values = np.asarray(lowered.point_data[key])
        except Exception:
            continue
        if (
            values.ndim == 1
            and values.shape[0] == lowered_points.shape[0]
            and np.issubdtype(values.dtype, np.number)
            and np.allclose(values.astype(float), original_points[:, 2], rtol=0.0, atol=1e-7)
        ):
            lowered.point_data[key] = values.astype(float) - d
            updated_arrays.append(str(key))
    lowered.point_data["local_cutoff_z"] = lowered_points[:, 2].copy()
    lowered.point_data["removed_top_thickness"] = np.full(
        lowered_points.shape[0], d, dtype=float
    )
    if clean:
        try:
            lowered = lowered.clean(tolerance=0.0)
        except Exception:
            try:
                lowered = lowered.clean()
            except Exception:
                pass
    report = {
        "operation": "offset_dem_vertically_for_local_top_removal",
        "top_layer_thickness": d,
        "input_z_min": float(np.min(original_points[:, 2])),
        "input_z_max": float(np.max(original_points[:, 2])),
        "cutoff_z_min": float(np.min(lowered_points[:, 2])),
        "cutoff_z_max": float(np.max(lowered_points[:, 2])),
        "updated_z_like_point_arrays": updated_arrays,
        "formula": "local_cutoff_z(x, y) = dem_z(x, y) - top_layer_thickness",
    }
    return lowered, report


def _build_dem_z_sampler(topography_mesh: Any, method: str = "auto"):
    from scipy.interpolate import RegularGridInterpolator
    from scipy.spatial import cKDTree

    points = np.asarray(topography_mesh.points, dtype=float)
    finite = np.isfinite(points[:, 0]) & np.isfinite(points[:, 1]) & np.isfinite(points[:, 2])
    points = points[finite, :3]
    if points.shape[0] == 0:
        raise ValueError("DEM/topography mesh has no finite XYZ points.")
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    bounds = [float(x.min()), float(x.max()), float(y.min()), float(y.max())]
    requested = str(method or "auto").strip().lower()
    if requested not in {"auto", "regular_grid", "nearest"}:
        raise ValueError("DEM sampling method must be auto, regular_grid, or nearest.")

    regular_error = ""
    if requested in {"auto", "regular_grid"}:
        try:
            unique_x, inverse_x = np.unique(x, return_inverse=True)
            unique_y, inverse_y = np.unique(y, return_inverse=True)
            if int(unique_x.size) * int(unique_y.size) != int(points.shape[0]):
                raise ValueError("DEM is not a complete regular XY lattice")
            z_grid = np.full((unique_y.size, unique_x.size), np.nan, dtype=float)
            z_grid[inverse_y, inverse_x] = z
            if not np.isfinite(z_grid).all():
                raise ValueError("DEM grid has duplicates or missing positions")
            interpolator = RegularGridInterpolator(
                (unique_y, unique_x), z_grid, method="linear",
                bounds_error=False, fill_value=np.nan,
            )

            def regular_sample(xy: np.ndarray, chunk_size: int = 500000) -> np.ndarray:
                query = np.asarray(xy, dtype=float)
                result = np.full(query.shape[0], np.nan, dtype=float)
                for start in range(0, query.shape[0], int(chunk_size)):
                    stop = min(start + int(chunk_size), query.shape[0])
                    block = query[start:stop]
                    result[start:stop] = interpolator(
                        np.column_stack([block[:, 1], block[:, 0]])
                    )
                return result

            return regular_sample, {
                "sampling_method": "regular_grid_linear",
                "dem_grid_shape": [int(unique_y.size), int(unique_x.size)],
                "dem_xy_bounds": bounds,
            }
        except Exception as exc:
            regular_error = str(exc)
            if requested == "regular_grid":
                raise

    tree = cKDTree(np.column_stack([x, y]))

    def nearest_sample(xy: np.ndarray, chunk_size: int = 500000) -> np.ndarray:
        query = np.asarray(xy, dtype=float)
        result = np.full(query.shape[0], np.nan, dtype=float)
        inside = (
            (query[:, 0] >= bounds[0]) & (query[:, 0] <= bounds[1])
            & (query[:, 1] >= bounds[2]) & (query[:, 1] <= bounds[3])
        )
        ids = np.flatnonzero(inside)
        for start in range(0, ids.size, int(chunk_size)):
            block_ids = ids[start:start + int(chunk_size)]
            _dist, nearest = tree.query(query[block_ids], k=1, workers=-1)
            result[block_ids] = z[np.asarray(nearest, dtype=np.int64)]
        return result

    return nearest_sample, {
        "sampling_method": "nearest_dem_point",
        "dem_grid_shape": None,
        "dem_xy_bounds": bounds,
        "regular_grid_fallback_reason": regular_error or None,
    }


def _threshold_upper(mesh: Any, value: float, scalars: str, preference: str, all_scalars: bool):
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
        return mesh.threshold(value=[float(value), np.inf], **kwargs)


def remove_local_top_layer(
    base_mesh: Any,
    topography_mesh: Any,
    thickness: float = 20.0,
    sampling_method: str = "auto",
    selection_mode: str = "cell_center",
    crop_to_dem_xy: bool = True,
    clean_output: bool = True,
):
    """Keep cells whose local depth below the DEM is at least thickness."""
    d = float(thickness)
    lowered_dem, offset_report = offset_topography_vertical(
        topography_mesh, d, triangulate=True, clean=False
    )
    sampler, sampler_report = _build_dem_z_sampler(topography_mesh, sampling_method)
    mesh = base_mesh.copy(deep=True)
    input_cells = int(mesh.n_cells)
    input_points = int(mesh.n_points)
    mode = str(selection_mode or "cell_center").strip().lower()
    if mode not in {"cell_center", "all_points", "any_point"}:
        raise ValueError("selection_mode must be cell_center, all_points, or any_point")

    if mode == "cell_center":
        centers = np.asarray(mesh.cell_centers().points, dtype=float)
        dem_z = sampler(centers[:, :2])
        depth = dem_z - centers[:, 2]
        depth[~np.isfinite(depth)] = -np.inf
        mesh.cell_data["__local_depth_below_dem_cell__"] = depth
        result = _threshold_upper(
            mesh, d, "__local_depth_below_dem_cell__", "cell", False
        )
        criterion = "cell center depth below local DEM"
    else:
        points = np.asarray(mesh.points, dtype=float)
        dem_z = sampler(points[:, :2])
        depth = dem_z - points[:, 2]
        depth[~np.isfinite(depth)] = -np.inf
        mesh.point_data["__local_depth_below_dem_point__"] = depth
        result = _threshold_upper(
            mesh, d, "__local_depth_below_dem_point__", "point", mode == "all_points"
        )
        criterion = "all cell points" if mode == "all_points" else "any cell point"

    for association in [result.point_data, result.cell_data]:
        for key in ["__local_depth_below_dem_point__", "__local_depth_below_dem_cell__"]:
            if key in association:
                del association[key]

    if crop_to_dem_xy and result.n_cells:
        bounds = sampler_report["dem_xy_bounds"]
        centers = np.asarray(result.cell_centers().points, dtype=float)
        inside = (
            (centers[:, 0] >= bounds[0]) & (centers[:, 0] <= bounds[1])
            & (centers[:, 1] >= bounds[2]) & (centers[:, 1] <= bounds[3])
        )
        result = result.extract_cells(np.flatnonzero(inside))

    if int(result.n_cells) <= 0:
        raise ValueError(
            "Local top-layer threshold removed all cells. Check coordinates, units, and thickness."
        )
    if clean_output:
        try:
            result = result.clean(
                tolerance=0.0, remove_unused_points=True, average_point_data=False
            )
        except Exception:
            try:
                result = result.clean()
            except Exception:
                pass

    report = {
        "operation": "remove_local_top_layer_by_dem",
        "top_layer_thickness": d,
        "selection_mode": mode,
        "selection_criterion": criterion,
        "input_cells": input_cells,
        "output_cells": int(result.n_cells),
        "removed_cells": input_cells - int(result.n_cells),
        "input_points": input_points,
        "output_points": int(result.n_points),
        "output_bounds": [float(v) for v in result.bounds],
        "local_cutoff_surface": offset_report,
        "dem_sampling": sampler_report,
        "formula": "keep cells where DEM_Z(X,Y) - mesh_Z >= top_layer_thickness",
        "note": "Every XY position uses its own DEM elevation; no global cutoff Z is used.",
    }
    return result, lowered_dem, report


def _patch_pyvista_add_mesh_positional_color() -> None:
    """Compatibility patch for GemPy Viewer/PyVista version combinations."""
    try:
        import pyvista as pv

        base_plotter = pv.plotting.plotter.BasePlotter
        original = getattr(
            base_plotter,
            "_gempy_notebook_original_add_mesh",
            None,
        )
        if original is None:
            original = base_plotter.add_mesh
            setattr(
                base_plotter,
                "_gempy_notebook_original_add_mesh",
                original,
            )

        def add_mesh_compat(self, *args, **kwargs):
            if len(args) >= 2 and "color" not in kwargs:
                possible_color = args[1]
                if isinstance(
                    possible_color,
                    (str, tuple, list),
                ):
                    kwargs["color"] = possible_color
                    args = (args[0],) + tuple(args[2:])
            return original(self, *args, **kwargs)

        base_plotter.add_mesh = add_mesh_compat
    except Exception:
        pass


def gempy_regular_grid_mesh(geo_model: Any) -> Any:
    """Return the computed GemPy regular-grid volume as a PyVista dataset.

    This is the same full lithology/layer volume displayed by GemPy Viewer,
    not merely the extracted geological interface surfaces.
    """
    import pyvista as pv
    import gempy_viewer as gpv

    _patch_pyvista_add_mesh_positional_color()

    viewer = None
    errors: List[str] = []
    candidate_kwargs = [
        {
            "show": False,
            "show_data": False,
            "show_lith": True,
            "show_surfaces": False,
            "show_topography": False,
        },
        {
            "show_data": False,
            "show_lith": True,
            "show_surfaces": False,
            "show_topography": False,
        },
        {},
    ]

    for kwargs in candidate_kwargs:
        try:
            viewer = gpv.plot_3d(
                geo_model,
                **kwargs,
            )
            break
        except Exception as exc:
            errors.append(str(exc))

    if viewer is None:
        raise RuntimeError(
            "Could not create the GemPy regular-grid volume. "
            + " | ".join(errors[-3:])
        )

    actor = getattr(
        viewer,
        "regular_grid_actor",
        None,
    )
    if actor is None and hasattr(viewer, "plotter"):
        actor = getattr(
            viewer.plotter,
            "regular_grid_actor",
            None,
        )

    if actor is None:
        raise RuntimeError(
            "GemPy Viewer did not expose regular_grid_actor. "
            "Run Compute GemPy Model before clipping and use a compatible "
            "GemPy/gempy-viewer version."
        )

    mapper = actor.GetMapper()
    if mapper is None:
        raise RuntimeError(
            "The GemPy regular-grid actor has no VTK mapper."
        )

    vtk_input = mapper.GetInput()
    if vtk_input is None:
        raise RuntimeError(
            "The GemPy regular-grid actor has no mesh input."
        )

    return pv.wrap(vtk_input).copy(deep=True)


def crop_mesh_to_xy_bounds(
    mesh: Any,
    bounds: Sequence[float],
) -> Any:
    """Crop cells by their center coordinates to the supplied XY bounds."""
    centers = np.asarray(
        mesh.cell_centers().points,
        dtype=float,
    )
    keep = np.where(
        (centers[:, 0] >= float(bounds[0]))
        & (centers[:, 0] <= float(bounds[1]))
        & (centers[:, 1] >= float(bounds[2]))
        & (centers[:, 1] <= float(bounds[3]))
    )[0]

    if keep.size == 0:
        return mesh.extract_cells([])

    return mesh.extract_cells(keep)


def clean_layer_styles(
    layer_styles: Any,
) -> List[Dict[str, Any]]:
    """Normalize layer-style JSON/list data used by the clipping node."""
    rows = parse_json(
        layer_styles,
        layer_styles,
    )
    if rows in (None, ""):
        return []
    if not isinstance(rows, list):
        raise ValueError(
            "layer_styles must be a list or a JSON list."
        )

    cleaned: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            layer_id = int(row.get("id"))
        except Exception:
            continue

        cleaned.append(
            {
                "id": layer_id,
                "label": str(
                    row.get("label")
                    or f"layer_{layer_id}"
                ),
                "color": str(
                    row.get("color")
                    or ""
                ),
                "opacity": float(
                    row.get("opacity", 0.5)
                ),
            }
        )
    return cleaned


def clip_pyvista_dataset(
    base_mesh: Any,
    clip_mesh: Any = None,
    topography_mesh: Any = None,
    invert: bool = False,
    crinkle: bool = True,
    topography_clip_enabled: bool = True,
    topography_invert: bool = False,
    crop_to_topography_xy: bool = True,
    clean_output_mesh: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Clip a normal PyVista dataset with shell and/or topography surfaces."""
    mesh = base_mesh.copy(deep=True)
    operations: List[str] = ["input_mesh"]

    if clip_mesh is not None:
        shell = clip_mesh.extract_surface().triangulate()
        try:
            mesh = mesh.clip_surface(
                shell,
                invert=bool(invert),
                crinkle=bool(crinkle),
            )
        except TypeError:
            mesh = mesh.clip_surface(
                shell,
                invert=bool(invert),
            )
        operations.append(
            f"clip_surface(invert={bool(invert)})"
        )

    if (
        topography_mesh is not None
        and bool(topography_clip_enabled)
    ):
        topography = (
            topography_mesh
            .extract_surface()
            .triangulate()
        )

        if crop_to_topography_xy:
            mesh = crop_mesh_to_xy_bounds(
                mesh,
                topography.bounds,
            )
            operations.append(
                "crop_to_topography_xy_bounds"
            )

        try:
            mesh = mesh.clip_surface(
                topography,
                invert=bool(topography_invert),
                crinkle=bool(crinkle),
            )
        except TypeError:
            mesh = mesh.clip_surface(
                topography,
                invert=bool(topography_invert),
            )
        operations.append(
            "clip_surface_topography"
            f"(invert={bool(topography_invert)})"
        )

    if clean_output_mesh:
        try:
            mesh = mesh.clean(
                tolerance=0.0,
                remove_unused_points=True,
                average_point_data=False,
            )
        except TypeError:
            try:
                mesh = mesh.clean()
            except Exception:
                pass
        except Exception:
            pass

    report = {
        "input_mode": "mesh",
        "operations": operations,
        "n_cells": int(mesh.n_cells),
        "n_points": int(mesh.n_points),
        "bounds": [
            float(value)
            for value in mesh.bounds
        ],
        "cell_data": list(
            mesh.cell_data.keys()
        ),
        "point_data": list(
            mesh.point_data.keys()
        ),
    }
    return mesh, report


def build_clipped_gempy_layer_mesh(
    geo_model: Any,
    clip_mesh: Any = None,
    topography_mesh: Any = None,
    cell_data_name: str = "id",
    layer_styles: Any = None,
    invert: bool = False,
    crinkle: bool = True,
    topography_clip_enabled: bool = True,
    topography_invert: bool = False,
    crop_to_topography_xy: bool = True,
    clean_output_mesh: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Convert a computed GeoModel to its regular-grid volume and clip it.

    This reproduces the GeoModel mode of the visual Clipping Tool in a normal,
    independently executable notebook function.
    """
    volume = gempy_regular_grid_mesh(
        geo_model
    )
    operations: List[str] = [
        "gempy_regular_grid_actor"
    ]

    if clip_mesh is not None:
        shell = clip_mesh.extract_surface().triangulate()
        try:
            volume = volume.clip_surface(
                shell,
                invert=bool(invert),
                crinkle=bool(crinkle),
            )
        except TypeError:
            volume = volume.clip_surface(
                shell,
                invert=bool(invert),
            )
        operations.append(
            f"clip_surface(invert={bool(invert)})"
        )

    if (
        topography_mesh is not None
        and bool(topography_clip_enabled)
    ):
        topography = (
            topography_mesh
            .extract_surface()
            .triangulate()
        )

        if crop_to_topography_xy:
            volume = crop_mesh_to_xy_bounds(
                volume,
                topography.bounds,
            )
            operations.append(
                "crop_to_topography_xy_bounds"
            )

        try:
            volume = volume.clip_surface(
                topography,
                invert=bool(topography_invert),
                crinkle=bool(crinkle),
            )
        except TypeError:
            volume = volume.clip_surface(
                topography,
                invert=bool(topography_invert),
            )
        operations.append(
            "clip_surface_topography"
            f"(invert={bool(topography_invert)})"
        )

    if cell_data_name not in volume.cell_data:
        available = list(
            volume.cell_data.keys()
        )
        raise KeyError(
            f"Clipped GemPy volume has no cell_data "
            f"{cell_data_name!r}. Available arrays: {available}"
        )

    styles = clean_layer_styles(
        layer_styles
    )

    if not styles:
        unique_layer_ids = np.unique(
            np.asarray(
                volume.cell_data[
                    cell_data_name
                ]
            )
        )
        styles = [
            {
                "id": int(layer_id),
                "label": f"layer_{int(layer_id)}",
                "color": "",
                "opacity": 0.5,
            }
            for layer_id in unique_layer_ids
        ]

    parts = []
    extracted_counts: Dict[str, int] = {}

    values = np.asarray(
        volume.cell_data[
            cell_data_name
        ]
    )

    for style in styles:
        layer_id = int(style["id"])
        cell_ids = np.where(
            values == layer_id
        )[0]
        extracted_counts[str(layer_id)] = int(
            cell_ids.size
        )

        if cell_ids.size == 0:
            continue

        layer = volume.extract_cells(
            cell_ids
        )

        try:
            layer.cell_data[
                cell_data_name
            ] = np.full(
                layer.n_cells,
                layer_id,
                dtype=values.dtype,
            )
        except Exception:
            pass

        parts.append(
            layer.cast_to_unstructured_grid()
        )

    if parts:
        combined = parts[0].copy(
            deep=True
        )
        for part in parts[1:]:
            try:
                combined = combined.merge(
                    part,
                    merge_points=False,
                )
            except TypeError:
                combined = combined.merge(
                    part,
                )
    else:
        combined = volume.cast_to_unstructured_grid()

    if clean_output_mesh:
        try:
            combined = combined.clean(
                tolerance=0.0,
                remove_unused_points=True,
                average_point_data=False,
            )
        except TypeError:
            try:
                combined = combined.clean()
            except Exception:
                pass
        except Exception:
            pass

    report = {
        "input_mode": "geo_model",
        "operations": operations,
        "cell_data_name": cell_data_name,
        "layer_styles": styles,
        "extracted_layer_cell_counts": extracted_counts,
        "n_cells": int(combined.n_cells),
        "n_points": int(combined.n_points),
        "bounds": [
            float(value)
            for value in combined.bounds
        ],
        "cell_data": list(
            combined.cell_data.keys()
        ),
        "point_data": list(
            combined.point_data.keys()
        ),
    }

    return combined, report


def save_mesh_compatible(
    mesh: Any,
    path: Path,
) -> Path:
    """Save a PyVista mesh while adapting VTP/VTU to the dataset type."""
    import pyvista as pv

    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    suffix = path.suffix.lower()
    is_polydata = isinstance(
        mesh,
        pv.PolyData,
    )

    if is_polydata and suffix == ".vtu":
        path = path.with_suffix(".vtp")
        mesh_to_save = mesh
    elif (
        not is_polydata
        and suffix == ".vtp"
    ):
        mesh_to_save = mesh.extract_surface()
    elif suffix == ".vtu":
        mesh_to_save = mesh.cast_to_unstructured_grid()
    else:
        mesh_to_save = mesh

    if path.suffix.lower() not in {
        ".vtk",
        ".vtp",
        ".vtu",
        ".vti",
        ".ply",
        ".stl",
        ".obj",
    }:
        path = path.with_suffix(
            ".vtu"
            if not is_polydata
            else ".vtp"
        )

    mesh_to_save.save(path)
    return path


def clip_mesh(mesh: Any, clip_mesh: Any = None, invert: bool = False, crinkle: bool = False):
    if clip_mesh is None:
        return mesh.copy(deep=True)
    surface = clip_mesh.extract_surface().triangulate()
    try:
        return mesh.clip_surface(surface, invert=bool(invert), crinkle=bool(crinkle))
    except TypeError:
        return mesh.clip_surface(surface, invert=bool(invert))


def save_mesh(mesh: Any, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(path)
    return path


def save_table_csv(table: pd.DataFrame, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False)
    return path


def save_array(array: np.ndarray, path: Path, fmt: str = "npy") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        np.savetxt(path, np.asarray(array), delimiter=",")
    else:
        np.save(path, np.asarray(array))
    return path


def load_gempy_model_json(path: Path):
    from gempy.modules.json_io.json_operations import JsonIO
    return JsonIO.load_model_from_json(str(path))


def save_gempy_model_json(geo_model: Any, path: Path) -> Path:
    from gempy.modules.json_io.json_operations import JsonIO
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    JsonIO.save_model_to_json(geo_model, str(path))
    return path


def find_identify_subdomains(executable_path: str = "", ogs_bin_dir: str = "") -> Path:
    if executable_path:
        candidate = Path(executable_path).expanduser().resolve()
        if candidate.exists():
            return candidate
        raise FileNotFoundError(candidate)
    names = ["identifySubdomains.exe", "identifySubdomains"] if os.name == "nt" else ["identifySubdomains", "identifySubdomains.exe"]
    if ogs_bin_dir:
        for name in names:
            candidate = Path(ogs_bin_dir).expanduser().resolve() / name
            if candidate.exists():
                return candidate
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise FileNotFoundError("identifySubdomains was not found.")


def ogs_identify_full_mesh(
    mesh: Any,
    output_path: Path,
    executable_path: str = "",
    ogs_bin_dir: str = "",
    search_length: float = 1e-6,
):
    import pyvista as pv
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    executable = find_identify_subdomains(executable_path, ogs_bin_dir)
    with tempfile.TemporaryDirectory(prefix="gempy_ogs_identify_") as temporary_directory:
        temporary_directory_path = Path(temporary_directory)
        input_path = temporary_directory_path / (output_path.stem + "_input.vtu")
        mesh.cast_to_unstructured_grid().save(input_path, binary=True)
        prefix = temporary_directory_path / (output_path.stem + "_tmp_")
        command = [str(executable), "-f", "-m", str(input_path), "-s", f"{float(search_length):.16g}", "-o", str(prefix), "--", str(input_path)]
        subprocess.run(command, check=True)
        candidates = _newest_existing_paths(temporary_directory_path.glob(prefix.name + "*.vtu"))
        if not candidates:
            raise FileNotFoundError("identifySubdomains did not create an output VTU.")
        if output_path.exists():
            output_path.unlink()
        shutil.copy2(candidates[0], output_path)
    return pv.read(output_path)


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


def _kadi_record_by_id(manager: Any, record_id: Any) -> Any:
    """Retrieve a KADI record using the keyword-only kadi-apy API."""
    try:
        normalized_id = int(str(record_id).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"KADI record ID must be an integer, got {record_id!r}."
        ) from exc

    return manager.record(id=normalized_id)


def load_from_kadi(
    record_id: str,
    file_name: str,
    output_dir: Path = Path("inputs"),
) -> Path:
    """Download one named file from a KADI record.

    kadi-apy requires the record ID as a keyword argument. The download API
    expects the KADI file UUID, so the visible filename is resolved first.
    """
    from kadi_apy import KadiManager

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manager = KadiManager()
    record = _kadi_record_by_id(manager, record_id)

    target = output_dir / Path(file_name).name
    file_id = record.get_file_id(str(file_name))
    response = record.download_file(
        file_id,
        file_path=str(target),
    )

    status_code = getattr(response, "status_code", None)
    response_ok = getattr(response, "ok", None)
    if response_ok is False or (
        status_code is not None and int(status_code) >= 400
    ):
        raise RuntimeError(
            f"KADI download failed for record {record_id}, "
            f"file {file_name!r}, status={status_code}."
        )

    if not target.exists():
        raise FileNotFoundError(
            "KADI reported a successful request, but the downloaded file "
            f"was not created at {target}."
        )

    return target


def upload_file_to_kadi(
    path: Path,
    record_id: str,
    force: bool = False,
) -> Any:
    """Upload one file using the keyword-only KADI record factory."""
    from kadi_apy import KadiManager

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    manager = KadiManager()
    record = _kadi_record_by_id(manager, record_id)
    return record.upload_file(
        str(path),
        force=bool(force),
    )


# ---------------------------------------------------------------------------
# Additional direct GemPy workflow operations
# ---------------------------------------------------------------------------


def _number_list(value: Any) -> List[float]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, np.ndarray)):
        return [float(item) for item in value]
    return [float(item.strip()) for item in str(value).replace(";", ",").split(",") if item.strip()]


def add_surface_points(geo_model: Any, points_table: Optional[pd.DataFrame] = None, points_json: Any = None):
    import gempy as gp

    def add_group(element: str, x: Sequence[float], y: Sequence[float], z: Sequence[float]) -> None:
        try:
            gp.add_surface_points(geo_model=geo_model, x=list(x), y=list(y), z=list(z), elements_names=element)
        except TypeError:
            gp.add_surface_points(geo_model, list(x), list(y), list(z), element)

    if points_table is not None:
        frame = points_table.copy()
        for column in ["X", "Y", "Z"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["X", "Y", "Z", "formation"])
        for formation, group in frame.groupby("formation", dropna=True):
            add_group(str(formation), group["X"].tolist(), group["Y"].tolist(), group["Z"].tolist())

    rows = parse_json(points_json, []) or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        element = str(row.get("element") or "").strip()
        if not element:
            continue
        if str(row.get("mode") or "single").lower() == "grid":
            xs = _number_list(row.get("xs")); ys = _number_list(row.get("ys")); z = float(row.get("z"))
            x_values, y_values, z_values = [], [], []
            for y in ys:
                for x in xs:
                    x_values.append(x); y_values.append(y); z_values.append(z)
            add_group(element, x_values, y_values, z_values)
        else:
            add_group(element, [float(row["x"])], [float(row["y"])], [float(row["z"])])
    return geo_model


def auto_orientations(geo_model: Any, element_names: str):
    import gempy as gp
    for name in [item.strip() for item in str(element_names).split(",") if item.strip()]:
        element = geo_model.structural_frame.get_element_by_name(name)
        orientations = gp.create_orientations_from_surface_points_coords(xyz_coords=element.surface_points.xyz)
        gp.add_orientations(
            geo_model=geo_model,
            x=orientations.data["X"],
            y=orientations.data["Y"],
            z=orientations.data["Z"],
            pole_vector=orientations.grads,
            elements_names=name,
        )
    return geo_model


def set_gempy_options(geo_model: Any, **params: Any):
    options = geo_model.interpolation_options
    if params.get("number_octree_levels_surface") not in (None, ""):
        options.number_octree_levels_surface = int(params["number_octree_levels_surface"])
    if params.get("kernel_range") not in (None, ""):
        options.kernel_options.range = float(params["kernel_range"])
    if params.get("octree_error_threshold") not in (None, ""):
        options.evaluation_options.octree_error_threshold = float(params["octree_error_threshold"])
    if params.get("evaluation_chunk_size") not in (None, ""):
        options.evaluation_options.evaluation_chunk_size = int(params["evaluation_chunk_size"])
    if str(params.get("verbose") or "unchanged") not in {"", "unchanged"}:
        options.evaluation_options.verbose = str(params["verbose"]).lower() == "true"
    if str(params.get("compute_condition_number") or "unchanged") not in {"", "unchanged"}:
        options.kernel_options.compute_condition_number = str(params["compute_condition_number"]).lower() == "true"
    if params.get("uni_degree") not in (None, ""):
        options.kernel_options.uni_degree = int(params["uni_degree"])
    if str(params.get("mesh_extraction") or "unchanged") not in {"", "unchanged"}:
        options.mesh_extraction = str(params["mesh_extraction"]).lower() == "true"
    kernel_function = str(params.get("kernel_function") or "").strip()
    if kernel_function:
        from gempy_engine.core.data.kernel_classes.kernel_functions import AvailableKernelFunctions
        options.kernel_function = getattr(AvailableKernelFunctions, kernel_function)
    return geo_model


def set_topography(
    geo_model: Any,
    mode: str = "file",
    filepath: Any = None,
    topography_table: Optional[pd.DataFrame] = None,
    topography_points_json: Any = None,
    topography_resolution: Any = None,
    random_z_fraction_min: float = 0.6,
    random_z_fraction_max: float = 1.0,
    fractal_dimension: float = 2.0,
):
    import gempy as gp
    mode = str(mode).lower()
    if mode == "preview":
        return geo_model
    if mode == "file":
        if not filepath:
            raise ValueError("Topography file path is required.")
        try:
            gp.set_topography_from_file(grid=geo_model.grid, filepath=str(filepath))
        except TypeError:
            gp.set_topography_from_file(geo_model.grid, str(filepath))
    elif mode == "arrays":
        if topography_table is not None:
            xyz = topography_table[["X", "Y", "Z"]].to_numpy(float)
        else:
            xyz = np.asarray(parse_json(topography_points_json, []), dtype=float).reshape(-1, 3)
        try:
            gp.set_topography_from_arrays(grid=geo_model.grid, xyz_vertices=xyz)
        except TypeError:
            gp.set_topography_from_arrays(geo_model.grid, xyz)
    elif mode == "random":
        extent = np.asarray(getattr(geo_model.grid.regular_grid, "extent", geo_model.grid.extent), dtype=float)
        zmin, zmax = extent[4], extent[5]
        dz = np.asarray([
            zmin + (zmax - zmin) * float(random_z_fraction_min),
            zmin + (zmax - zmin) * float(random_z_fraction_max),
        ])
        resolution = parse_json(topography_resolution, topography_resolution)
        try:
            gp.set_topography_from_random(
                grid=geo_model.grid,
                d_z=dz,
                topography_resolution=np.asarray(resolution, dtype=int) if resolution else None,
                fractal_dimension=float(fractal_dimension),
            )
        except TypeError:
            gp.set_topography_from_random(grid=geo_model.grid, d_z=dz)
    else:
        raise ValueError(f"Unknown topography mode: {mode}")
    return geo_model


def set_gempy_grid(geo_model: Any, mode: str = "activate", grid_points: Optional[pd.DataFrame] = None, **params: Any):
    import gempy as gp
    mode = str(mode).lower()
    if mode == "section":
        name = str(params.get("section_name") or "section")
        start = parse_json(params.get("section_start"), [0, 0])
        end = parse_json(params.get("section_end"), [1000, 1000])
        resolution = parse_json(params.get("section_resolution"), [100, 80])
        section_dict = {name: ([float(start[0]), float(start[1])], [float(end[0]), float(end[1])], [int(resolution[0]), int(resolution[1])])}
        try:
            gp.set_section_grid(grid=geo_model.grid, section_dict=section_dict)
        except TypeError:
            gp.set_section_grid(geo_model.grid, section_dict)
    elif mode == "custom":
        xyz = grid_points[["X", "Y", "Z"]].to_numpy(float) if grid_points is not None else np.asarray(parse_json(params.get("custom_points_json"), []), dtype=float).reshape(-1, 3)
        try:
            gp.set_custom_grid(grid=geo_model.grid, xyz_coord=xyz)
        except TypeError:
            gp.set_custom_grid(geo_model.grid, xyz)
    elif mode == "centered":
        centers = grid_points[["X", "Y", "Z"]].to_numpy(float) if grid_points is not None else np.asarray(parse_json(params.get("centers_json"), [[0, 0, 0]]), dtype=float)
        radius = np.asarray(parse_json(params.get("centered_radius"), [100, 100, 100]), dtype=float)
        resolution = np.asarray(parse_json(params.get("centered_resolution"), [10, 10, 10]), dtype=int)
        try:
            gp.set_centered_grid(grid=geo_model.grid, centers=centers, radius=radius, resolution=resolution)
        except TypeError:
            gp.set_centered_grid(geo_model.grid, centers, radius, resolution)
    elif mode == "activate":
        names = [item.strip().lower() for item in str(params.get("active_grids") or "regular").split(",") if item.strip()]
        grid_types = []
        for name in names:
            candidate = name.upper()
            if hasattr(gp.data.GridTypes, candidate):
                grid_types.append(getattr(gp.data.GridTypes, candidate))
        try:
            gp.set_active_grid(grid=geo_model.grid, grid_type=grid_types, reset=as_bool(params.get("reset_active_grids"), True))
        except TypeError:
            gp.set_active_grid(geo_model.grid, grid_types)
    else:
        raise ValueError(f"Unknown grid mode: {mode}")
    return geo_model


def set_finite_fault(geo_model: Any, finite_faults_json: Any, clear_existing: bool = False):
    import gempy as gp
    rows = parse_json(finite_faults_json, []) or []
    groups = list(geo_model.structural_frame.structural_groups)
    if clear_existing:
        for group in groups:
            try: group.faults_input_data = None
            except Exception: pass
    name_to_index = {str(getattr(group, "name", index)): index for index, group in enumerate(groups)}
    for row in rows:
        if not isinstance(row, dict) or not as_bool(row.get("enabled"), True):
            continue
        group_index = name_to_index.get(str(row.get("group_name") or ""), as_int(row.get("group_index"), None))
        if group_index is None or not (0 <= group_index < len(groups)):
            continue
        def vector(key: str, default: Sequence[float]) -> np.ndarray:
            values = _number_list(row.get(key)) or list(default)
            if len(values) == 1: values *= 3
            return np.asarray(values, dtype=float)
        center = vector("center", [0, 0, 0]); radius = vector("radius", [1, 1, 1]); max_slope = vector("max_slope", [1, 1, 1])
        scaled_center = geo_model.input_transform.apply(center.reshape(1, -1))[0]
        scaled_radius = geo_model.input_transform.scale_points(radius.reshape(1, -1))[0]
        implicit = gp.implicit_functions.ellipsoid_3d_factory(center=scaled_center, radius=scaled_radius, max_slope=max_slope)
        transform = gp.data.Transform(position=vector("transform_position", [0, 0, 0]), rotation=vector("transform_rotation", [0, 0, 0]), scale=vector("transform_scale", [1, 1, 1]))
        finite = gp.data.FiniteFaultData(implicit_function=implicit, implicit_function_transform=transform, pivot=scaled_center)
        groups[group_index].faults_input_data = gp.data.FaultsData(
            fault_values_everywhere=np.zeros(0), fault_values_on_sp=np.zeros(0), thickness=None,
            fault_values_ref=np.zeros(0), fault_values_rest=np.zeros(0), finite_fault_data=finite,
        )
    return geo_model


# ---------------------------------------------------------------------------
# Direct mesh and voxel operations
# ---------------------------------------------------------------------------


def _infer_spacing_from_centers(values: np.ndarray, decimals: int = 8) -> float:
    unique = np.unique(np.round(np.asarray(values, dtype=float), int(decimals)))
    differences = np.diff(unique)
    differences = differences[differences > 10 ** (-int(decimals) + 1)]
    if not differences.size:
        return 1.0
    return float(np.median(differences))


def mesh_to_voxel_model(
    mesh: Any,
    voxel_size: Any = None,
    voxel_size_y: Any = None,
    voxel_size_z: Any = None,
    target_cells_longest_axis: int = 80,
    max_voxels: int = 2_000_000,
    padding: float = 0.0,
    voxelization_mode: str = "inside_surface",
    distance_buffer: Any = None,
    source_scalar: str = "auto",
    output_scalar_name: str = "MaterialIDs",
    inside_tolerance: float = 1e-6,
    check_surface: bool = False,
    invert_inside: bool = False,
):
    import pyvista as pv
    from scipy.spatial import cKDTree
    surface = mesh.extract_surface().triangulate()
    bounds = np.asarray(surface.bounds, dtype=float)
    lengths = np.asarray([bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4]], dtype=float)
    sx = as_float(voxel_size, None)
    if sx is None or sx <= 0:
        sx = float(np.max(lengths) / max(int(target_cells_longest_axis), 1))
    sy = as_float(voxel_size_y, sx) or sx
    sz = as_float(voxel_size_z, sx) or sx
    spacing = np.asarray([sx, sy, sz], dtype=float)
    lower = np.asarray([bounds[0], bounds[2], bounds[4]], dtype=float) - float(padding)
    upper = np.asarray([bounds[1], bounds[3], bounds[5]], dtype=float) + float(padding)
    counts = np.maximum(np.ceil((upper-lower)/spacing).astype(int), 1)
    if int(np.prod(counts)) > int(max_voxels):
        raise ValueError(f"Candidate voxel count {int(np.prod(counts)):,} exceeds max_voxels={int(max_voxels):,}.")
    grid = pv.ImageData(dimensions=tuple((counts+1).tolist()), spacing=tuple(spacing.tolist()), origin=tuple(lower.tolist()))
    centers = np.asarray(grid.cell_centers().points)
    points = pv.PolyData(centers)
    mode = str(voxelization_mode).lower()
    if mode == "distance_to_surface":
        sampled = points.compute_implicit_distance(surface)
        threshold = as_float(distance_buffer, 0.75*float(np.min(spacing)))
        selected = np.abs(np.asarray(sampled.point_data["implicit_distance"])) <= float(threshold)
    else:
        selected_points = points.select_enclosed_points(surface, tolerance=float(inside_tolerance), check_surface=bool(check_surface))
        selected = np.asarray(selected_points.point_data["SelectedPoints"]).astype(bool)
        if invert_inside: selected = ~selected
    voxel_grid = grid.extract_cells(np.flatnonzero(selected))
    scalar_name = source_scalar
    if scalar_name in {"", "auto", None}:
        candidates = list(mesh.cell_data.keys()) + list(mesh.point_data.keys())
        scalar_name = next((name for name in ["MaterialIDs", "combined_element_id", "id", "lith_block"] if name in candidates), candidates[0] if candidates else None)
    if scalar_name:
        source_centers = np.asarray(mesh.cell_centers().points)
        tree = cKDTree(source_centers)
        _, nearest = tree.query(np.asarray(voxel_grid.cell_centers().points), k=1)
        if scalar_name in mesh.cell_data:
            values = np.asarray(mesh.cell_data[scalar_name])[nearest]
        else:
            point_tree = cKDTree(np.asarray(mesh.points)); _, point_ids = point_tree.query(np.asarray(voxel_grid.cell_centers().points), k=1)
            values = np.asarray(mesh.point_data[scalar_name])[point_ids]
        voxel_grid.cell_data[output_scalar_name] = values
    return voxel_grid


def clip_voxel_model_by_mask(
    base: Any,
    mask: Any,
    clip_mode: str = "same_xy_column_all_z",
    xy_expand_cells: int = 0,
    z_margin_cells: int = 0,
):
    base_centers = np.asarray(base.cell_centers().points)
    mask_centers = np.asarray(mask.cell_centers().points)
    sx = _infer_spacing_from_centers(base_centers[:,0]); sy = _infer_spacing_from_centers(base_centers[:,1]); sz = _infer_spacing_from_centers(base_centers[:,2])
    origin = np.minimum(base_centers.min(axis=0), mask_centers.min(axis=0))
    base_idx = np.rint((base_centers-origin)/np.asarray([sx,sy,sz])).astype(int)
    mask_idx = np.rint((mask_centers-origin)/np.asarray([sx,sy,sz])).astype(int)
    columns: Dict[tuple[int,int], List[int]] = {}
    for i,j,k in mask_idx:
        for di in range(-int(xy_expand_cells), int(xy_expand_cells)+1):
            for dj in range(-int(xy_expand_cells), int(xy_expand_cells)+1):
                columns.setdefault((int(i+di),int(j+dj)),[]).append(int(k))
    exact = {tuple(map(int,row)) for row in mask_idx}
    remove = np.zeros(base.n_cells,dtype=bool)
    mode = str(clip_mode).lower()
    for cell_id,(i,j,k) in enumerate(base_idx):
        if mode == "exact_overlap":
            remove[cell_id] = (int(i),int(j),int(k)) in exact
            continue
        ks = columns.get((int(i),int(j)))
        if not ks: continue
        if mode == "same_xy_column_above_mask": remove[cell_id] = int(k) >= min(ks)-int(z_margin_cells)
        elif mode == "same_xy_column_below_mask": remove[cell_id] = int(k) <= max(ks)+int(z_margin_cells)
        elif mode == "same_xy_column_inside_mask_z_range": remove[cell_id] = min(ks)-int(z_margin_cells) <= int(k) <= max(ks)+int(z_margin_cells)
        else: remove[cell_id] = True
    return base.extract_cells(np.flatnonzero(~remove)), base.extract_cells(np.flatnonzero(remove))


def merge_voxel_models(
    meshes: Sequence[Any],
    cell_data_name: str = "auto",
    output_scalar_name: str = "MaterialIDs",
    reindex_scope: str = "source_and_value",
    reindex_start_id: int = 1,
    first_input_wins: bool = True,
):
    import pyvista as pv
    if not meshes: raise ValueError("At least one voxel model is required.")
    all_centers = [np.asarray(mesh.cell_centers().points) for mesh in meshes]
    spacings = [np.asarray([_infer_spacing_from_centers(c[:,0]),_infer_spacing_from_centers(c[:,1]),_infer_spacing_from_centers(c[:,2])]) for c in all_centers]
    spacing = np.min(np.vstack(spacings),axis=0)
    origin = np.min(np.vstack([c.min(axis=0) for c in all_centers]),axis=0)
    entries: Dict[tuple[int,int,int], tuple[int,Any,int]] = {}
    mapping: Dict[tuple[Any,...],int] = {}
    next_id = int(reindex_start_id)
    sequence = list(range(len(meshes)-1,-1,-1)) if first_input_wins else list(range(len(meshes)))
    for source_index in sequence:
        mesh=meshes[source_index]; centers=all_centers[source_index]
        scalar = cell_data_name
        if scalar in {"", "auto", None}:
            scalar = next((n for n in ["MaterialIDs","combined_element_id","id","lith_block"] if n in mesh.cell_data), next(iter(mesh.cell_data.keys()),None))
        values=np.asarray(mesh.cell_data[scalar]) if scalar else np.ones(mesh.n_cells)
        indices=np.rint((centers-origin)/spacing).astype(int)
        for local,(key_arr,value) in enumerate(zip(indices,values)):
            key=tuple(map(int,key_arr))
            map_key=(source_index,str(value)) if reindex_scope=="source_and_value" else (str(value),)
            if map_key not in mapping:
                mapping[map_key]=next_id; next_id+=1
            entries[key]=(source_index,mapping[map_key],local)
    keys=np.asarray(list(entries.keys()),dtype=int)
    if not len(keys): return pv.UnstructuredGrid()
    mins=keys.min(axis=0); maxs=keys.max(axis=0); dims=maxs-mins+1
    grid=pv.ImageData(dimensions=tuple((dims+1).tolist()),spacing=tuple(spacing.tolist()),origin=tuple((origin+mins*spacing-spacing/2).tolist()))
    full_indices=np.arange(int(np.prod(dims))).reshape(tuple(dims),order="F")
    selected_ids=[]; scalar_values=[]; source_values=[]
    for key,(src,value,_) in entries.items():
        relative=np.asarray(key)-mins
        selected_ids.append(int(full_indices[tuple(relative)])); scalar_values.append(value); source_values.append(src)
    result=grid.extract_cells(np.asarray(selected_ids,dtype=int))
    result.cell_data[output_scalar_name]=np.asarray(scalar_values,dtype=np.int32)
    result.cell_data["merge_source_index"]=np.asarray(source_values,dtype=np.int32)
    return result


def hex_mesh_to_voxel_grid(mesh: Any, material_scalar: str = "auto", output_material_name: str = "MaterialIDs"):
    import pyvista as pv
    grid = mesh.cast_to_unstructured_grid().copy(deep=True)
    scalar = material_scalar
    if scalar in {"", "auto", None}:
        scalar = next((name for name in ["MaterialIDs","combined_element_id","id","lith_block"] if name in grid.cell_data), next(iter(grid.cell_data.keys()),None))
    if scalar and scalar in grid.cell_data:
        grid.cell_data[output_material_name]=np.asarray(grid.cell_data[scalar]).copy()
    return grid


def extract_voxel_boundaries(mesh: Any, output_dir: Path, output_prefix: str = "voxel", cell_data_name: str = "MaterialIDs", triangulate_shells: bool = True):
    """Extract and save six external boundary parts while preserving VTK arrays."""
    import pyvista as pv
    output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    surface=mesh.extract_surface(pass_pointid=True,pass_cellid=True)
    surface=surface.compute_normals(cell_normals=True,point_normals=False,auto_orient_normals=False,inplace=False)
    normals=np.asarray(surface.cell_data["Normals"])
    labels={
        "top": np.flatnonzero(normals[:,2] > 0.5),
        "bottom": np.flatnonzero(normals[:,2] < -0.5),
        "east": np.flatnonzero(normals[:,0] > 0.5),
        "west": np.flatnonzero(normals[:,0] < -0.5),
        "north": np.flatnonzero(normals[:,1] > 0.5),
        "south": np.flatnonzero(normals[:,1] < -0.5),
    }
    boundaries={name: surface.extract_cells(ids).extract_surface() for name,ids in labels.items()}
    side_mesh=boundaries["north"].merge(boundaries["south"]).merge(boundaries["east"]).merge(boundaries["west"])
    full_shell=surface
    if triangulate_shells:
        side_mesh=side_mesh.triangulate(); full_shell=full_shell.triangulate()
    boundaries["side_mesh"]=side_mesh; boundaries["full_shell"]=full_shell
    files={}
    for name,boundary in boundaries.items():
        vtp=output_dir/f"{output_prefix}_{name}.vtp"; vtu=output_dir/f"{output_prefix}_{name}.vtu"
        boundary.save(vtp); boundary.cast_to_unstructured_grid().save(vtu)
        files[name]={"vtp":vtp,"vtu":vtu,"mesh":boundary}
    return boundaries, files


def repair_reorder_voxel_mesh(
    mesh: Any,
    material_array: str = "MaterialIDs",
    bottom_z: float = -200.0,
    remove_stale_mapping_arrays: bool = True,
    add_node_material_ids: bool = True,
    keep_original_order_ids: bool = False,
    reindex_material_ids_from_zero: bool = True,
):
    """General repair/reorder export.

    The standalone version performs the deterministic parts of the editor node:
    bottom clipping, stale-ID removal, cell ordering, node ordering and zero-based
    material reindexing. For project-specific bottom-hole filling, retain the
    explicit repair function from the source notebook before this call.
    """
    import pyvista as pv
    grid=mesh.cast_to_unstructured_grid().copy(deep=True)
    centers=np.asarray(grid.cell_centers().points)
    dz=_infer_spacing_from_centers(centers[:,2],decimals=6)
    keep=(centers[:,2]-dz/2.0)>=float(bottom_z)-1e-6
    grid=grid.extract_cells(np.flatnonzero(keep))
    stale_point={"bulk_node_ids","vtkOriginalPointIds","PointMergeMap"}
    stale_cell={"bulk_element_ids","vtkOriginalCellIds","merge_source_index","merge_source_cell","number_bulk_elements"}
    if remove_stale_mapping_arrays:
        for name in list(grid.point_data.keys()):
            if name in stale_point: del grid.point_data[name]
        for name in list(grid.cell_data.keys()):
            if name in stale_cell: del grid.cell_data[name]
    if material_array not in grid.cell_data: raise KeyError(material_array)
    connectivity=np.asarray(grid.cell_connectivity,dtype=np.int64).reshape(grid.n_cells,-1)
    centers=np.asarray(grid.cell_centers().points); materials=np.asarray(grid.cell_data[material_array]).reshape(-1)
    order=np.lexsort((centers[:,0],centers[:,1],centers[:,2],materials))
    sorted_conn=connectivity[order]; flat=sorted_conn.reshape(-1)
    first=np.full(grid.n_points,np.iinfo(np.int64).max,dtype=np.int64); np.minimum.at(first,flat,np.arange(flat.size,dtype=np.int64))
    node_order=np.argsort(first,kind="stable"); old_to_new=np.empty(grid.n_points,dtype=np.int64); old_to_new[node_order]=np.arange(grid.n_points)
    cell_type=int(np.unique(grid.celltypes)[0])
    result=pv.UnstructuredGrid({pv.CellType(cell_type): old_to_new[sorted_conn]},np.asarray(grid.points)[node_order])
    for name,array in grid.point_data.items():
        if np.asarray(array).shape[0]==grid.n_points: result.point_data[name]=np.asarray(array)[node_order]
    for name,array in grid.cell_data.items():
        if np.asarray(array).shape[0]==grid.n_cells: result.cell_data[name]=np.asarray(array)[order]
    sorted_materials=np.asarray(result.cell_data[material_array]).reshape(-1)
    if add_node_material_ids:
        source_positions=first[node_order]//sorted_conn.shape[1]
        valid=first[node_order]!=np.iinfo(np.int64).max
        node_materials=np.full(result.n_points,-1,dtype=sorted_materials.dtype); node_materials[valid]=sorted_materials[source_positions[valid]]
        result.point_data["NodeMaterialIDs"]=node_materials
    if keep_original_order_ids:
        result.point_data["OriginalNodeIDs"]=node_order.astype(np.uint64); result.cell_data["OriginalElementIDs"]=order.astype(np.uint64)
    if reindex_material_ids_from_zero:
        unique=np.unique(sorted_materials); result.cell_data[material_array]=np.searchsorted(unique,sorted_materials).astype(np.int32)
        if "NodeMaterialIDs" in result.point_data:
            values=np.asarray(result.point_data["NodeMaterialIDs"]); mapped=np.full(values.shape,-1,dtype=np.int32); valid=np.isin(values,unique); mapped[valid]=np.searchsorted(unique,values[valid]); result.point_data["NodeMaterialIDs"]=mapped
    return result


def apply_gempy_edits(
    geo_model: Any,
    operations_json: Any,
    auto_create_missing_element: bool = False,
    new_element_relation: str = "ERODE",
    fail_on_edit_error: bool = True,
):
    """Apply exported editor operations with direct GemPy API calls.

    Exported UIDs normally have the form ``s_<internal index>`` and
    ``o_<internal index>``. The suffix is used as the GemPy table index.
    """
    import gempy as gp
    operations = parse_json(operations_json, []) or []
    reports = []

    def internal_index(action: Dict[str, Any]) -> int:
        if action.get("index") not in (None, ""):
            return int(action["index"])
        uid = str(action.get("uid") or action.get("_editor_uid") or "")
        try:
            return int(uid.split("_", 1)[1])
        except Exception as exc:
            raise ValueError(f"Operation has no usable index/UID: {action}") from exc

    for action in operations:
        if not isinstance(action, dict):
            continue
        op = str(action.get("op") or "").lower()
        kind = str(action.get("kind") or action.get("type") or "surface").lower()
        try:
            if op in {"add", "add_surface"} and not kind.startswith("ori"):
                element = str(action.get("element") or action.get("formation") or "")
                gp.add_surface_points(
                    geo_model=geo_model,
                    x=[float(action.get("X", action.get("x")))],
                    y=[float(action.get("Y", action.get("y")))],
                    z=[float(action.get("Z", action.get("z")))],
                    elements_names=element,
                )
            elif op in {"add_orientation", "add"} and kind.startswith("ori"):
                element = str(action.get("element") or action.get("formation") or "")
                vector = np.asarray([[
                    float(action.get("G_x", action.get("gx", 0.0))),
                    float(action.get("G_y", action.get("gy", 0.0))),
                    float(action.get("G_z", action.get("gz", 1.0))),
                ]])
                gp.add_orientations(
                    geo_model=geo_model,
                    x=[float(action.get("X", action.get("x")))],
                    y=[float(action.get("Y", action.get("y")))],
                    z=[float(action.get("Z", action.get("z")))],
                    pole_vector=vector,
                    elements_names=element,
                )
            elif op == "modify" and kind.startswith("ori"):
                index = internal_index(action)
                fields = {}
                for name in ["X", "Y", "Z", "G_x", "G_y", "G_z"]:
                    value = action.get(name, action.get(name.lower()))
                    if value not in (None, ""):
                        fields[name] = float(value)
                gp.modify_orientations(geo_model, index, **fields)
            elif op == "modify":
                index = internal_index(action)
                fields = {}
                for name in ["X", "Y", "Z"]:
                    value = action.get(name, action.get(name.lower()))
                    if value not in (None, ""):
                        fields[name] = float(value)
                gp.modify_surface_points(geo_model, index, **fields)
            elif op == "delete" and kind.startswith("ori"):
                gp.delete_orientations(geo_model, internal_index(action))
            elif op == "delete":
                gp.delete_surface_points(geo_model, internal_index(action))
            else:
                raise ValueError(f"Unknown operation: {op}/{kind}")
            reports.append({"operation": op, "kind": kind, "status": "applied"})
        except Exception as exc:
            reports.append({"operation": op, "kind": kind, "status": "error", "error": str(exc)})
    if fail_on_edit_error and any(report["status"] == "error" for report in reports):
        raise RuntimeError(f"Some GemPy edits failed: {reports}")
    return geo_model, reports


def upload_geodt_structure_version_to_kadi(
    files: Dict[str, Path],
    title: str = "GeoDT Input Structure Model",
    description: str = "Data from the structural model workflow, including the structural model, the total surfaces and all sub-surfaces.",
    subject: str = "Structure model in voxel grid from GemPy",
    collection_id: int = 7238,
    description_record_id: int = 80295,
    version: str = "0.1",
    tag: str = "geolab",
    update_description_record: bool = True,
    add_group_roles: bool = True,
    force_upload: bool = True,
    dry_run: bool = False,
    output_dir: Path = Path("outputs/kadi_upload"),
):
    """Prepare and optionally upload the project-specific GeoDT structure package."""
    from datetime import datetime
    import pyvista as pv
    from kadi_apy import KadiManager

    required_names = {
        "volume": "gempy_volume_with_topo.vtu",
        "south": "south.vtu", "north": "north.vtu",
        "bottom": "bottom.vtu", "top": "top.vtu",
        "west": "west.vtu", "east": "east.vtu",
        "side_mesh": "side_mesh.vtu", "full_shell": "full_shell.vtu",
    }
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    prepared = []
    for role, target_name in required_names.items():
        source = Path(files[role])
        target = output_dir / target_name
        mesh = pv.read(source)
        mesh.cast_to_unstructured_grid().save(target)
        prepared.append(target)
    report = {"dry_run": bool(dry_run), "prepared_files": [str(path) for path in prepared]}
    if dry_run:
        return report

    with KadiManager() as manager:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        full_title = f"{title} V{version}_{timestamp}"
        record = manager.record(create=True, identifier=full_title, title=full_title, description=description)
        creator = ""
        try: creator = manager.pat_user.meta["displayname"]
        except Exception: pass
        metadata = [
            {"key": "Subject", "type": "str", "value": subject},
            {"key": "Dataset Category", "type": "str", "value": "Mesh/CAD Data"},
            {"key": "Coverage", "type": "str", "value": "GeoDT"},
            {"key": "Reference System", "type": "str", "value": "EPSG:25832 (UTM Zone 32N)"},
            {"key": "Format", "type": "str", "value": "Visualization Toolkit (vtp, vtu, vti, ...)"},
            {"key": "Responsible Party", "type": "str", "value": creator},
            {"key": "Creator", "type": "str", "value": creator},
        ]
        record.edit(type="dataset", force=True)
        if tag: record.add_tag(tag)
        record.add_metadata(metadata_new=metadata, force=True)
        if add_group_roles:
            for group_id, role in [(143, "Admin"), (302, "Editor"), (122, "Member")]:
                record.add_group_role(group_id, role)
        manager.collection(id=int(collection_id)).add_record_link(record.id)
        if update_description_record:
            manager.record(id=int(description_record_id)).add_metadatum(
                {"key": "Record ID of the current version", "type": "int", "value": record.id},
                force=True,
            )
        for path in prepared:
            record.upload_file(str(path), file_name=path.name, force=bool(force_upload))
        report["record_id"] = record.id
    return report
