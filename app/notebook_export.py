from __future__ import annotations

import json
import math
import pprint
import re
import tempfile
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .node_registry import NODE_TYPES
from .storage import get_file_path, get_file_record

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
HELPER_SOURCE_PATH = APP_ROOT / "notebook_standalone_runtime.py"


SUPPORTED_NODE_TYPES = {
    "LoadUploadedFile",
    "LoadGemPyModelJson",
    "LoadKadiFile",
    "MergeTables",
    "ValidateGeoTable",
    "ConvertOrientationAngles",
    "CleanGeoTable",
    "SampleGeoTable",
    "Visualization",
    "PlotGemPy2D",
    "PlotGemPy3D",
    "ThickenMesh",
    "RemoveLocalTopLayer",
    "CombineMeshes",
    "OGSIdentifyFullMesh",
    "SaveTableCsv",
    "CreateGemPyModel",
    "ConfigureStructuralFrame",
    "ComputeGemPyModel",
    "ExtractGemPyArray",
    "SaveGemPyModelJson",
    "SaveArray",
    "UploadFileToKadi",
    "PyVistaClippedLayerViewer",
    "MeshToVoxelModel",
    "ClipVoxelModelByMask",
    "MergeVoxelModels",
    "HexMeshToVoxelGrid",
    "ExtractVoxelBoundaries",
    "RepairReorderVoxelMesh",
    "SetFiniteFault",
    "AddSurfacePoints",
    "AutoOrientations",
    "SetTopography",
    "SetGemPyGrid",
    "SetGemPyOptions",
    "InteractiveGemPyModelDataEditor",
    "UploadGeoDTStructureVersionToKadi",
}


class NotebookExportError(ValueError):
    pass


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        json.dumps(value, allow_nan=False)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _safe_identifier(value: str, prefix: str = "node") -> str:
    text = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value or "")).strip("_")
    if not text:
        text = prefix
    if text[0].isdigit():
        text = f"{prefix}_{text}"
    return text.lower()


def _py(value: Any) -> str:
    return pprint.pformat(_json_safe(value), width=100, sort_dicts=False)


def _used_upload_file_ids(project: Dict[str, Any]) -> set[str]:
    used: set[str] = set()
    graph = project.get("graph") or project
    for node in graph.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        params = node.get("params") or {}
        if node.get("type") == "LoadUploadedFile" and params.get("file_id"):
            used.add(str(params.get("file_id")))
        for key, value in params.items():
            key_text = str(key)
            if key_text == "file_id" or key_text.endswith("_file_id"):
                if value:
                    used.add(str(value))
            elif key_text.endswith("_file_ids") and value:
                if isinstance(value, (list, tuple)):
                    used.update(str(item) for item in value if item)
                else:
                    used.update(part.strip() for part in str(value).split(",") if part.strip())
    return used


def _topological_order(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> List[str]:
    node_ids = [str(node.get("id")) for node in nodes]
    existing = set(node_ids)
    indegree = {node_id: 0 for node_id in node_ids}
    outgoing: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("from_node") or "")
        target = str(edge.get("to_node") or "")
        if source not in existing or target not in existing:
            raise NotebookExportError(f"Edge references a missing node: {source!r} -> {target!r}")
        outgoing[source].append(target)
        indegree[target] += 1
    queue = deque(node_id for node_id in node_ids if indegree[node_id] == 0)
    order: List[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for target in outgoing.get(node_id, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(node_ids):
        raise NotebookExportError("The workflow contains a cycle and cannot be exported as a sequential notebook.")
    return order


def _node_meta_map() -> Dict[str, Dict[str, Any]]:
    return {str(item.get("type")): item for item in NODE_TYPES}


def _markdown_cell(source: str, **metadata: Any) -> Dict[str, Any]:
    return {"cell_type": "markdown", "metadata": metadata, "source": source.splitlines(keepends=True)}


def _code_cell(source: str, **metadata: Any) -> Dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": metadata,
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


def _input_manifest(project: Dict[str, Any], include_inputs: bool) -> Tuple[List[Dict[str, Any]], List[Tuple[Path, str]]]:
    records: List[Dict[str, Any]] = []
    files_to_bundle: List[Tuple[Path, str]] = []
    for file_id in sorted(_used_upload_file_ids(project)):
        try:
            record = dict(get_file_record(file_id))
            path = get_file_path(file_id)
        except Exception:
            continue
        stored_name = Path(str(record.get("stored_name") or path.name)).name
        item = {
            "file_id": file_id,
            "original_name": record.get("original_name") or path.name,
            "stored_name": stored_name,
            "bundle_name": stored_name,
            "category": record.get("category") or "Uncategorized",
        }
        records.append(item)
        if include_inputs:
            files_to_bundle.append((path, f"inputs/{stored_name}"))
    return records, files_to_bundle


def _build_port_variables(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    meta = _node_meta_map()
    result: Dict[str, Dict[str, str]] = {}
    for index, node in enumerate(nodes, start=1):
        node_id = str(node.get("id"))
        prefix = f"step_{index:02d}_{_safe_identifier(node_id)}"
        output_ports = meta.get(str(node.get("type")), {}).get("outputs") or []
        result[node_id] = {
            str(port.get("name")): f"{prefix}_{_safe_identifier(str(port.get('name')))}"
            for port in output_ports
        }
    return result


def _edge_inputs(
    node_id: str,
    edges: List[Dict[str, Any]],
    port_variables: Dict[str, Dict[str, str]],
) -> Dict[str, List[str]]:
    inputs: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        if str(edge.get("to_node")) != node_id:
            continue
        source_id = str(edge.get("from_node"))
        source_port = str(edge.get("from_port"))
        target_port = str(edge.get("to_port"))
        variable = port_variables.get(source_id, {}).get(source_port)
        if variable:
            inputs[target_port].append(variable)
    return inputs


def _one(inputs: Dict[str, List[str]], port: str, default: str = "None") -> str:
    values = inputs.get(port) or []
    return values[-1] if values else default


def _many(inputs: Dict[str, List[str]], *ports: str) -> List[str]:
    values: List[str] = []
    for port in ports:
        values.extend(inputs.get(port) or [])
    return values


def _file_expr(params: Dict[str, Any], manifest_by_id: Dict[str, Dict[str, Any]], param_name: str = "file_id") -> str:
    file_id = str(params.get(param_name) or "")
    record = manifest_by_id.get(file_id)
    if record:
        return f'INPUT_DIR / {_py(record["bundle_name"])}'
    if file_id:
        return f'INPUT_FILES.get({_py(file_id)}, Path({_py(file_id)}))'
    return "None"


def _unsupported_message(node_types: List[str]) -> str:
    return (
        "Standalone notebook export does not yet have a direct Python exporter for: "
        + ", ".join(sorted(node_types))
        + ". Remove those nodes or use a supported equivalent. The exporter will not silently insert NodeRegistry/RuntimeValue code."
    )


def _node_code(
    index: int,
    node: Dict[str, Any],
    edges: List[Dict[str, Any]],
    port_vars: Dict[str, Dict[str, str]],
    manifest_by_id: Dict[str, Dict[str, Any]],
) -> str:
    node_id = str(node.get("id"))
    node_type = str(node.get("type"))
    params = dict(node.get("params") or {})
    outputs = port_vars[node_id]
    inputs = _edge_inputs(node_id, edges, port_vars)
    lines: List[str] = [f"# Step {index}: {node_type}"]

    def out(port: str, fallback: Optional[str] = None) -> str:
        return outputs.get(port) or fallback or f"step_{index:02d}_{_safe_identifier(node_id)}_{_safe_identifier(port)}"

    if node_type == "LoadUploadedFile":
        file_var = out("file")
        loaded_var = f"{file_var}_loaded"
        lines += [
            f"{file_var} = {_file_expr(params, manifest_by_id)}",
            f"{loaded_var} = load_input_file({file_var}, file_type={_py(params.get('file_type', 'auto'))}, sheet_name={_py(params.get('sheet_name', ''))})",
        ]
        for port in ["table", "mesh", "array", "json", "text"]:
            if port in outputs:
                lines.append(f"{out(port)} = {loaded_var}.get({_py(port)})")
        lines += [f"preview_loaded_data({loaded_var})"]

    elif node_type == "LoadGemPyModelJson":
        path_expr = _one(inputs, "file", _file_expr(params, manifest_by_id))
        lines += [
            f"{out('file', 'gempy_model_json_path')} = Path({path_expr})",
            f"{out('geo_model')} = load_gempy_model_json({out('file', 'gempy_model_json_path')})",
            f"display(structural_group_summary({out('geo_model')}))",
        ]

    elif node_type == "LoadKadiFile":
        record_id = params.get("record_id") or params.get("record") or ""
        file_name = params.get("file_name") or params.get("filename") or ""
        lines += [
            f"{out('file')} = load_from_kadi({_py(str(record_id))}, {_py(str(file_name))}, INPUT_DIR)",
            f"_loaded_{index} = load_input_file({out('file')}, file_type={_py(params.get('file_type', 'auto'))}, sheet_name={_py(params.get('sheet_name', ''))})",
        ]
        for port in ["table", "mesh", "array"]:
            if port in outputs:
                lines.append(f"{out(port)} = _loaded_{index}.get({_py(port)})")
        lines.append(f"preview_loaded_data(_loaded_{index})")

    elif node_type == "MergeTables":
        values = _many(inputs, "tables")
        lines += [
            f"{out('table')} = merge_tables([{', '.join(values)}], axis={int(params.get('axis', 0) or 0)}, ignore_index={bool(params.get('ignore_index', True))}, add_source_column={bool(params.get('add_source_column', False))}, source_column={_py(params.get('source_column', '_merge_source'))})",
            f"display({out('table')}.head(15))",
            f"print('Merged table shape:', {out('table')}.shape)",
        ]

    elif node_type == "ValidateGeoTable":
        source = _one(inputs, "table")
        lines += [
            f"{out('table')} = {source}",
            f"{out('report')} = validate_geological_table({source}, table_kind={_py(params.get('table_kind', 'auto'))})",
            f"display({out('report')})",
        ]

    elif node_type == "ConvertOrientationAngles":
        source = _one(inputs, "table")
        lines += [
            f"{out('table')} = convert_orientation_angles({source}, azimuth_col={_py(params.get('azimuth_col', 'auto'))}, dip_col={_py(params.get('dip_col', 'auto'))}, polarity_col={_py(params.get('polarity_col', 'auto'))}, azimuth_is_dip_direction={bool(params.get('azimuth_is_dip_direction', True))}, normalize_vectors={bool(params.get('normalize_vectors', True))}, overwrite_existing_gradients={bool(params.get('overwrite_existing_gradients', False))}, drop_angle_columns={bool(params.get('drop_angle_columns', False))})",
            f"display({out('table')}.head(15))",
        ]
        if "report" in outputs:
            lines.append(f"{out('report')} = {{'rows': len({out('table')}), 'columns': list({out('table')}.columns)}}")

    elif node_type == "CleanGeoTable":
        source = _one(inputs, "table")
        lines += [
            f"{out('table')} = clean_geological_table({source}, coord_cols={_py(params.get('coord_cols', 'X,Y,Z'))}, grad_cols={_py(params.get('grad_cols', 'G_x,G_y,G_z'))}, formation_col={_py(params.get('formation_col', 'formation'))}, formation_to_str={bool(params.get('formation_to_str', True))}, numeric_xyz={bool(params.get('numeric_xyz', True))}, numeric_gradients={bool(params.get('numeric_gradients', True))}, drop_missing_xyz={bool(params.get('drop_missing_xyz', True))}, remove_zero_gradients={bool(params.get('remove_zero_gradients', True))})",
            f"display({out('table')}.head(15))",
            f"print('Cleaned table shape:', {out('table')}.shape)",
        ]
        if "report" in outputs:
            lines.append(f"{out('report')} = {{'rows': len({out('table')}), 'columns': list({out('table')}.columns)}}")

    elif node_type == "SampleGeoTable":
        source = _one(inputs, "table")
        lines += [
            f"{out('table')} = sample_geological_table({source}, n={int(params.get('n', 100) or 100)}, group_col={_py(params.get('group_col', 'formation'))}, feature_cols={_py(params.get('feature_cols', 'X,Y,Z'))}, allocation={_py(params.get('allocation', 'equal'))}, method={_py(params.get('method', 'fast'))}, random_state={int(params.get('random_state', 42) or 42)})",
            f"display({out('table')}.head(15))",
            f"print('Sampled rows:', len({out('table')}))",
        ]
        if "report" in outputs:
            lines.append(f"{out('report')} = {{'rows': len({out('table')})}}")

    elif node_type == "CreateGemPyModel":
        surface = _one(inputs, "surface_points")
        orientation = _one(inputs, "orientations")
        lines += [
            f"{out('geo_model')} = create_gempy_model(",
            f"    surface_points={surface},",
            f"    orientations={orientation},",
            f"    project_name={_py(params.get('project_name') or 'GemPy Model')},",
            f"    extent={_py(params.get('extent'))},",
            f"    refinement={int(params.get('refinement', 3) or 3)},",
            f"    resolution={_py(params.get('resolution'))},",
            f"    auto_extent_from_data={bool(params.get('auto_extent_from_data', True))},",
            f"    extent_user_overridden={bool(params.get('extent_user_overridden', False))},",
            f"    extent_padding_percent={float(params.get('extent_padding_percent', 5) or 5)},",
            f"    apply_surface_point_nugget={bool(params.get('apply_surface_point_nugget', True))},",
            f"    surface_point_nugget={float(params.get('surface_point_nugget', 0.01) or 0.01)},",
            f"    working_dir=OUTPUT_DIR,",
            f")",
            f"print({out('geo_model')})",
            f"display(structural_group_summary({out('geo_model')}))",
        ]

    elif node_type == "ConfigureStructuralFrame":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')} = configure_structural_frame(",
            f"    {source},",
            f"    mapping_json={_py(params.get('mapping_json'))},",
            f"    groups_json={_py(params.get('groups_json'))},",
            f"    anisotropy={_py(params.get('anisotropy', 'NONE'))},",
            f"    remove_default_formation={bool(params.get('remove_default_formation', True))},",
            f"    fault_relations_json={_py(params.get('fault_relations_json'))},",
            f")",
            f"display(structural_group_summary({out('geo_model')}))",
        ]
        if "report" in outputs:
            lines.append(f"{out('report')} = structural_group_summary({out('geo_model')})")

    elif node_type == "SetFiniteFault":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')} = set_finite_fault({source}, finite_faults_json={_py(params.get('finite_faults_json'))}, clear_existing={bool(params.get('clear_existing', False))})",
            f"display(structural_group_summary({out('geo_model')}))",
        ]

    elif node_type == "AddSurfacePoints":
        source = _one(inputs, "geo_model")
        points_table = _one(inputs, "points_table", "None")
        lines += [
            f"{out('geo_model')} = add_surface_points({source}, points_table={points_table}, points_json={_py(params.get('points_json'))})",
            f"print('Surface points added.')",
        ]

    elif node_type == "AutoOrientations":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')} = auto_orientations({source}, element_names={_py(params.get('element_names', ''))})",
            f"print('Auto orientations added for:', {_py(params.get('element_names', ''))})",
        ]

    elif node_type == "SetTopography":
        source = _one(inputs, "geo_model")
        connected_file = _one(inputs, "topography_file", "None")
        connected_table = _one(inputs, "topography_table", "None")
        parameter_file = _file_expr(params, manifest_by_id, "topography_file_id")
        filepath_expr = connected_file if connected_file != "None" else (parameter_file if parameter_file != "None" else _py(params.get("filepath", "")))
        lines += [
            f"{out('geo_model')} = set_topography({source}, mode={_py(params.get('mode', 'file'))}, filepath={filepath_expr}, topography_table={connected_table}, topography_points_json={_py(params.get('topography_points_json'))}, topography_resolution={_py(params.get('topography_resolution'))}, random_z_fraction_min={float(params.get('random_z_fraction_min', 0.6) or 0.6)}, random_z_fraction_max={float(params.get('random_z_fraction_max', 1.0) or 1.0)}, fractal_dimension={float(params.get('fractal_dimension', 2.0) or 2.0)})",
            f"{out('report')} = {{'mode': {_py(params.get('mode', 'file'))}}}",
            f"display({out('report')})",
        ]

    elif node_type == "SetGemPyGrid":
        source = _one(inputs, "geo_model")
        points_table = _one(inputs, "grid_points", "None")
        lines += [
            f"{out('geo_model')} = set_gempy_grid({source}, mode={_py(params.get('mode', 'activate'))}, grid_points={points_table}, section_name={_py(params.get('section_name'))}, section_start={_py(params.get('section_start'))}, section_end={_py(params.get('section_end'))}, section_resolution={_py(params.get('section_resolution'))}, custom_points_json={_py(params.get('custom_points_json'))}, centers_json={_py(params.get('centers_json'))}, centered_radius={_py(params.get('centered_radius'))}, centered_resolution={_py(params.get('centered_resolution'))}, active_grids={_py(params.get('active_grids'))}, reset_active_grids={bool(params.get('reset_active_grids', True))})",
            f"{out('report')} = {{'mode': {_py(params.get('mode', 'activate'))}, 'active_grids': str(getattr({out('geo_model')}.grid, 'active_grids', ''))}}",
            f"display({out('report')})",
        ]

    elif node_type == "SetGemPyOptions":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')} = set_gempy_options({source}, **{_py(params)})",
            f"print('GemPy interpolation options updated.')",
        ]

    elif node_type == "ComputeGemPyModel":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')} = {source}",
            f"{out('solution')} = compute_gempy_model({source}, backend={_py(params.get('backend', 'PYTORCH'))}, dtype={_py(params.get('dtype', 'float64'))}, mesh_extraction={bool(params.get('mesh_extraction', False))}, retry_without_mesh_extraction={bool(params.get('retry_without_mesh_extraction', True))})",
            f"display(summarize_gempy_solution({out('solution')}))",
        ]

    elif node_type == "PlotGemPy3D":
        source = _one(inputs, "geo_model")
        viewer_var = f"_gempy_3d_viewer_{index}"
        mesh_var = out(
            "mesh",
            f"step_{index:02d}_gempy_3d_surface_mesh",
        )
        report_var = out(
            "report",
            f"step_{index:02d}_gempy_3d_report",
        )
        file_var = out(
            "file",
            f"step_{index:02d}_gempy_3d_surface_file",
        )

        surface_file_name = str(
            params.get("surface_mesh_file_name")
            or "gempy_3d_surfaces.vtp"
        )

        save_surface_mesh = bool(
            params.get("save_surface_mesh", True)
        )

        lines += [
            f"{viewer_var} = plot_gempy_3d({source}, show_data={bool(params.get('show_data', True))}, show_lith={bool(params.get('show_lith', True))}, show_surfaces={bool(params.get('show_surfaces', True))}, show_topography={bool(params.get('show_topography', True))}, show_boundaries={bool(params.get('show_boundaries', True))})",
        ]

        if save_surface_mesh:
            lines += [
                f"{mesh_var}, {file_var}, {report_var} = export_gempy_surface_mesh(",
                f"    {source},",
                f"    OUTPUT_DIR / {_py(surface_file_name)},",
                f")",
            ]
        else:
            lines += [
                f"{mesh_var}, {report_var} = extract_gempy_surface_mesh({source})",
                f"{file_var} = None",
            ]

        lines += [
            f"display({report_var})",
            f"if {mesh_var} is not None:",
            f"    visualize({mesh_var}, scalars='element_id', show_edges=False)",
            f"    print('Surface mesh saved:', {file_var})",
            f"else:",
            f"    print({report_var}.get('message', 'No surface mesh available.'))",
        ]

    elif node_type == "PlotGemPy2D":
        source = _one(inputs, "geo_model")
        file_name = str(params.get("file_name") or "gempy_2d_plot.png")

        cell_number_value = params.get("cell_number")
        if isinstance(cell_number_value, str):
            cell_number_value = cell_number_value.strip()
            if cell_number_value.lower() in {"", "none", "null", "nan"}:
                cell_number_value = None
        if cell_number_value is not None:
            try:
                cell_number_value = int(float(cell_number_value))
            except (TypeError, ValueError):
                pass

        lines += [
            f"{out('file')} = OUTPUT_DIR / {_py(file_name)}",
            f"{out('report') if 'report' in outputs else f'_plot2d_result_{index}'}, _plot2d_figure_{index} = plot_gempy_2d({source}, direction={_py(params.get('direction', 'z'))}, cell_number={_py(cell_number_value)}, show_data={bool(params.get('show_data', True))}, show_lith={bool(params.get('show_lith', True))}, show_boundaries={bool(params.get('show_boundaries', True))}, show_scalar={bool(params.get('show_scalar', False))}, dpi={int(params.get('dpi', 160) or 160)}, output_path={out('file')})",
            f"display(_plot2d_figure_{index})",
            f"print('Saved:', {out('file')})",
        ]

    elif node_type == "ExtractGemPyArray":
        source = _one(inputs, "solution")
        lines += [
            f"{out('array')} = extract_gempy_array({source}, output_name={_py(params.get('output_name', 'lith_block'))}, reshape={_py(params.get('reshape'))})",
            f"print('Array:', {out('array')}.shape, {out('array')}.dtype)",
            f"display({out('array')}.reshape(-1)[:100])",
        ]

    elif node_type == "InteractiveGemPyModelDataEditor":
        source = _one(inputs, "geo_model")
        lines += [
            f"{out('geo_model')}, {out('report')} = apply_gempy_edits({source}, operations_json={_py(params.get('operations_json'))}, auto_create_missing_element={bool(params.get('auto_create_missing_element', False))}, new_element_relation={_py(params.get('new_element_relation', 'ERODE'))}, fail_on_edit_error={bool(params.get('fail_on_edit_error', True))})",
            f"display({out('report')})",
        ]

    elif node_type == "Visualization":
        source = _one(
            inputs,
            "value",
            _one(
                inputs,
                "data",
                _one(
                    inputs,
                    "table",
                    _one(
                        inputs,
                        "mesh",
                        _one(inputs, "array"),
                    ),
                ),
            ),
        )

        # A common visual-editor workflow connects PlotGemPy3D.report to a
        # Visualization node. In a standalone notebook that report is only
        # metadata, while the same upstream node also produces the actual mesh.
        # Redirect such a connection to the upstream mesh automatically.
        visualization_note = None
        for incoming_edge in edges:
            if str(incoming_edge.get("to_node")) != node_id:
                continue

            source_node_id = str(
                incoming_edge.get("from_node")
            )
            source_port = str(
                incoming_edge.get("from_port")
            )

            source_outputs = port_vars.get(
                source_node_id,
                {},
            )

            if (
                source_port == "report"
                and source_outputs.get("mesh")
            ):
                source = source_outputs["mesh"]
                visualization_note = (
                    "Visualization received an upstream report, so the "
                    "standalone notebook uses the corresponding mesh output."
                )
                break

        report_var = out(
            "report",
            f"step_{index:02d}_visualization",
        )

        if visualization_note:
            lines.append(
                f"print({_py(visualization_note)})"
            )

        lines += [
            f"{report_var} = visualize({source}, scalars={_py(params.get('mesh_scalars', 'auto'))}, show_edges={bool(params.get('show_edges', True))}, rows={int(params.get('rows', 15) or 15)}, filter_by_scalar_values={bool(params.get('filter_by_scalar_values', False))}, scalar_filter_values={_py(params.get('scalar_filter_values', ''))})",
        ]

    elif node_type == "CombineMeshes":
        mesh_inputs = _many(inputs, "meshes", "mesh", "mesh_1", "mesh_2", "mesh_3", "mesh_4", "mesh_5", "mesh_6", "mesh_7", "mesh_8")
        lines += [
            f"{out('mesh')} = combine_meshes([{', '.join(mesh_inputs)}], merge_points={bool(params.get('merge_points', False))}, clean_output={bool(params.get('clean_output', True))})",
            f"visualize({out('mesh')}, scalars={_py(params.get('mesh_scalars', 'auto'))}, show_edges={bool(params.get('show_edges', True))})",
        ]
        if "file" in outputs:
            file_name = str(params.get("output_file_name") or "combined_mesh.vtu")
            lines += [f"{out('file')} = save_mesh({out('mesh')}, OUTPUT_DIR / {_py(file_name)})", f"print('Saved:', {out('file')})"]

    elif node_type == "ThickenMesh":
        source = _one(inputs, "mesh")
        file_name = str(params.get("output_file_name") or "thickened_mesh.vtp")
        lines += [
            f"{out('mesh')} = thicken_mesh({source}, distance={float(params.get('buffer_distance', 0) or 0)}, mode={_py(params.get('mode', 'symmetric'))}, close_sides={bool(params.get('close_sides', True))}, triangulate={bool(params.get('triangulate_input', True))}, consistent_normals={bool(params.get('consistent_normals', True))}, auto_orient_normals={bool(params.get('auto_orient_normals', True))}, flip_normals={bool(params.get('flip_normals', False))}, clean_input={bool(params.get('clean_input', True))}, clean_output={bool(params.get('clean_output', True))})",
            f"visualize({out('mesh')}, scalars={_py(params.get('mesh_scalars', 'auto'))}, show_edges={bool(params.get('show_edges', True))})",
        ]
        if "file" in outputs:
            lines += [f"{out('file')} = save_mesh({out('mesh')}, OUTPUT_DIR / {_py(file_name)})", f"print('Saved:', {out('file')})"]

    elif node_type == "RemoveLocalTopLayer":
        input_mesh_source = _one(inputs, "input_mesh", "None")
        if input_mesh_source == "None":
            selected = _file_expr(params, manifest_by_id, "input_mesh_file_id")
            if selected != "None":
                input_mesh_source = f"load_input_file({selected}).get('mesh')"

        topography_source = _one(inputs, "topography_mesh", "None")
        if topography_source == "None":
            selected = _file_expr(params, manifest_by_id, "topography_mesh_file_id")
            if selected != "None":
                topography_source = f"load_input_file({selected}).get('mesh')"
        if topography_source == "None":
            raise NotebookExportError(
                "Remove Local Top Layer requires a connected DEM/topography mesh "
                "or a selected topography mesh file."
            )

        thickness = float(params.get("top_layer_thickness", 20.0) or 20.0)
        lowered_var = out(
            "lowered_topography",
            f"step_{index:02d}_local_cutoff_dem",
        )
        report_var = out(
            "report",
            f"step_{index:02d}_remove_local_top_report",
        )
        result_file_var = out(
            "file",
            f"step_{index:02d}_local_top_result_file",
        )
        lowered_file_var = out(
            "lowered_topography_file",
            f"step_{index:02d}_local_cutoff_dem_file",
        )
        result_file_name = str(
            params.get("output_file_name")
            or "mesh_without_local_top_20m.vtu"
        )
        lowered_file_name = str(
            params.get("lowered_dem_file_name")
            or "dem_local_top_minus_20m.vtp"
        )

        if input_mesh_source == "None":
            lines += [
                f"{lowered_var}, _offset_report_{index} = offset_topography_vertical({topography_source}, thickness={thickness}, triangulate={bool(params.get('triangulate_dem', True))}, clean={bool(params.get('clean_lowered_dem', False))})",
                f"{out('mesh')} = {lowered_var}",
                f"{report_var} = {{'operation': 'create_local_cutoff_dem_only', 'input_mode': 'dem_only', 'local_cutoff_surface': _offset_report_{index}}}",
            ]
        else:
            lines += [
                f"{out('mesh')}, {lowered_var}, {report_var} = remove_local_top_layer(",
                f"    {input_mesh_source},",
                f"    {topography_source},",
                f"    thickness={thickness},",
                f"    sampling_method={_py(params.get('dem_sampling_method', 'auto'))},",
                f"    selection_mode={_py(params.get('selection_mode', 'cell_center'))},",
                f"    crop_to_dem_xy={bool(params.get('crop_to_dem_xy', True))},",
                f"    clean_output={bool(params.get('clean_output_mesh', True))},",
                f")",
            ]

        lines += [
            f"{result_file_var} = save_mesh_compatible({out('mesh')}, OUTPUT_DIR / {_py(result_file_name)})",
            f"{lowered_file_var} = save_mesh_compatible({lowered_var}, OUTPUT_DIR / {_py(lowered_file_name)})",
            f"{report_var}['file'] = str({result_file_var})",
            f"{report_var}['lowered_topography_file'] = str({lowered_file_var})",
            f"display({report_var})",
            f"visualize({out('mesh')}, scalars={_py(params.get('mesh_scalars', 'auto'))}, show_edges={bool(params.get('show_edges', True))})",
            f"print('Saved local top-layer result:', {result_file_var})",
            f"print('Saved local cutoff DEM:', {lowered_file_var})",
        ]


    elif node_type == "PyVistaClippedLayerViewer":
        input_mesh_source = _one(
            inputs,
            "input_mesh",
            _one(inputs, "mesh", "None"),
        )
        geo_model_source = _one(
            inputs,
            "geo_model",
            "None",
        )

        if (
            input_mesh_source == "None"
            and geo_model_source == "None"
        ):
            raise NotebookExportError(
                "Clipping Tool requires either a connected geo_model "
                "or a connected input_mesh."
            )

        clip_source = _one(
            inputs,
            "clip_mesh",
            "None",
        )
        if clip_source == "None":
            selected = _file_expr(
                params,
                manifest_by_id,
                "clip_mesh_file_id",
            )
            if selected != "None":
                clip_source = (
                    f"load_input_file({selected}).get('mesh')"
                )

        topography_source = _one(
            inputs,
            "topography_mesh",
            "None",
        )
        if topography_source == "None":
            selected = _file_expr(
                params,
                manifest_by_id,
                "topography_mesh_file_id",
            )
            if selected != "None":
                topography_source = (
                    f"load_input_file({selected}).get('mesh')"
                )

        file_name = str(
            params.get("output_file_name")
            or (
                "clipped_mesh.vtu"
                if input_mesh_source != "None"
                else "clipped_gempy_layers.vtu"
            )
        )

        if input_mesh_source != "None":
            lines += [
                f"{out('mesh')}, {out('report')} = clip_pyvista_dataset(",
                f"    {input_mesh_source},",
                f"    clip_mesh={clip_source},",
                f"    topography_mesh={topography_source},",
                f"    invert={bool(params.get('invert', False))},",
                f"    crinkle={bool(params.get('crinkle', True))},",
                f"    topography_clip_enabled={bool(params.get('topography_clip_enabled', True))},",
                f"    topography_invert={bool(params.get('topography_invert', False))},",
                f"    crop_to_topography_xy={bool(params.get('crop_to_topography_xy', True))},",
                f"    clean_output_mesh={bool(params.get('clean_output_mesh', True))},",
                f")",
            ]
        else:
            lines += [
                f"{out('mesh')}, {out('report')} = build_clipped_gempy_layer_mesh(",
                f"    {geo_model_source},",
                f"    clip_mesh={clip_source},",
                f"    topography_mesh={topography_source},",
                f"    cell_data_name={_py(params.get('cell_data_name', 'id'))},",
                f"    layer_styles={_py(params.get('layer_styles_json'))},",
                f"    invert={bool(params.get('invert', False))},",
                f"    crinkle={bool(params.get('crinkle', True))},",
                f"    topography_clip_enabled={bool(params.get('topography_clip_enabled', True))},",
                f"    topography_invert={bool(params.get('topography_invert', False))},",
                f"    crop_to_topography_xy={bool(params.get('crop_to_topography_xy', True))},",
                f"    clean_output_mesh={bool(params.get('clean_output_mesh', True))},",
                f")",
            ]

        file_variable = out(
            "file",
            f"step_{index:02d}_clipped_mesh_file",
        )
        lines += [
            f"{file_variable} = save_mesh_compatible(",
            f"    {out('mesh')},",
            f"    OUTPUT_DIR / {_py(file_name)},",
            f")",
            f"{out('report')}['file'] = str({file_variable})",
            f"display({out('report')})",
            f"visualize({out('mesh')}, scalars={_py(params.get('cell_data_name', params.get('mesh_scalars', 'auto')))}, show_edges={bool(params.get('show_edges', False))})",
            f"print('Saved clipped mesh:', {file_variable})",
        ]


    elif node_type == "MeshToVoxelModel":
        source = _one(inputs, "mesh", "None")
        if source == "None":
            selected = _file_expr(params, manifest_by_id, "mesh_file_id")
            source = f"load_input_file({selected}).get('mesh')"
        file_name = str(params.get("file_name") or "mesh_voxel_model.vtu")
        lines += [
            f"{out('voxel_model')} = mesh_to_voxel_model({source}, voxel_size={_py(params.get('voxel_size'))}, voxel_size_y={_py(params.get('voxel_size_y'))}, voxel_size_z={_py(params.get('voxel_size_z'))}, target_cells_longest_axis={int(params.get('target_cells_longest_axis', 80) or 80)}, max_voxels={int(params.get('max_voxels', 2000000) or 2000000)}, padding={float(params.get('padding', 0.0) or 0.0)}, voxelization_mode={_py(params.get('voxelization_mode', 'inside_surface'))}, distance_buffer={_py(params.get('distance_buffer'))}, distance_chunk_size={int(params.get('distance_chunk_size', 200000) or 200000)}, source_scalar={_py(params.get('source_scalar', 'auto'))}, output_scalar_name={_py(params.get('output_scalar_name', 'MaterialIDs'))}, inside_tolerance={float(params.get('inside_tolerance', 1e-6) or 1e-6)}, check_surface={bool(params.get('check_surface', False))}, invert_inside={bool(params.get('invert_inside', False))})",
            f"{out('voxel_grid')} = {out('voxel_model')}",
            f"{out('file')} = save_mesh({out('voxel_model')}, OUTPUT_DIR / {_py(file_name)})",
            f"{out('report')} = {{'n_cells': int({out('voxel_model')}.n_cells), 'n_points': int({out('voxel_model')}.n_points), 'file': str({out('file')})}}",
            f"display({out('report')})",
            f"visualize({out('voxel_model')}, scalars={_py(params.get('output_scalar_name', 'MaterialIDs'))}, show_edges={bool(params.get('show_edges', True))})",
        ]

    elif node_type == "ClipVoxelModelByMask":
        base = _one(inputs, "base_voxel_model")
        mask = _one(inputs, "mask_voxel_model")
        file_name = str(params.get("file_name") or "clipped_voxel_model.vtu")
        lines += [
            f"{out('voxel_model')}, _removed_voxels_{index} = clip_voxel_model_by_mask({base}, {mask}, clip_mode={_py(params.get('clip_mode', 'same_xy_column_all_z'))}, xy_expand_cells={int(params.get('xy_expand_cells', 0) or 0)}, z_margin_cells={int(params.get('z_margin_cells', 0) or 0)})",
            f"{out('voxel_grid')} = {out('voxel_model')}",
            f"{out('removed_voxels')} = _removed_voxels_{index}",
            f"{out('file')} = save_mesh({out('voxel_model')}, OUTPUT_DIR / {_py(file_name)})",
            f"{out('report')} = {{'kept_cells': int({out('voxel_model')}.n_cells), 'removed_cells': int(_removed_voxels_{index}.n_cells)}}",
            f"display({out('report')})",
        ]

    elif node_type == "MergeVoxelModels":
        models = _many(inputs, "voxel_models")
        file_name = str(params.get("file_name") or "merged_voxel_model.vtu")
        lines += [
            f"{out('voxel_model')} = merge_voxel_models([{', '.join(models)}], cell_data_name={_py(params.get('cell_data_name', 'auto'))}, output_scalar_name={_py(params.get('output_scalar_name', 'MaterialIDs'))}, reindex_scope={_py(params.get('reindex_scope', 'source_and_value'))}, reindex_start_id={int(params.get('reindex_start_id', 1) or 1)}, first_input_wins={bool(params.get('first_input_wins', True))}, target_voxel_size_mode={_py(params.get('target_voxel_size_mode', 'smallest_input'))}, resample_to_target_grid={bool(params.get('resample_to_target_grid', True))}, voxel_size={_py(params.get('voxel_size'))}, voxel_size_y={_py(params.get('voxel_size_y'))}, voxel_size_z={_py(params.get('voxel_size_z'))}, max_merged_voxels={int(params.get('max_merged_voxels', 2000000) or 2000000)})",
            f"{out('voxel_grid')} = {out('voxel_model')}",
            f"{out('file')} = save_mesh({out('voxel_model')}, OUTPUT_DIR / {_py(file_name)})",
            f"{out('report')} = {{'n_cells': int({out('voxel_model')}.n_cells), 'input_count': {len(models)}}}",
            f"display({out('report')})",
            f"visualize({out('voxel_model')}, scalars={_py(params.get('output_scalar_name', 'MaterialIDs'))}, show_edges={bool(params.get('show_edges', True))})",
        ]

    elif node_type == "HexMeshToVoxelGrid":
        source = _one(inputs, "mesh", "None")
        if source == "None":
            selected = _file_expr(params, manifest_by_id, "mesh_file_id")
            source = f"load_input_file({selected}).get('mesh')"
        file_name = str(params.get("file_name") or "voxel_grid.vtu")
        lines += [
            f"{out('voxel_grid')} = hex_mesh_to_voxel_grid({source}, material_scalar={_py(params.get('material_scalar', 'auto'))}, output_material_name={_py(params.get('output_material_name', 'MaterialIDs'))})",
            f"{out('file')} = save_mesh({out('voxel_grid')}, OUTPUT_DIR / {_py(file_name)})",
            f"visualize({out('voxel_grid')}, scalars={_py(params.get('output_material_name', 'MaterialIDs'))}, show_edges={bool(params.get('show_edges', True))})",
        ]

    elif node_type == "ExtractVoxelBoundaries":
        source = _one(inputs, "mesh", "None")
        if source == "None":
            selected = _file_expr(params, manifest_by_id, "mesh_file_id")
            source = f"load_input_file({selected}).get('mesh')"
        prefix = str(params.get("output_prefix") or "voxel")
        lines += [
            f"_boundaries_{index}, _boundary_files_{index} = extract_voxel_boundaries({source}, OUTPUT_DIR, output_prefix={_py(prefix)}, cell_data_name={_py(params.get('cell_data_name', 'MaterialIDs'))}, triangulate_shells={bool(params.get('triangulate_shells', True))}, decimals={int(params.get('decimals', 8) or 8)}, add_top_risers={bool(params.get('add_top_risers', True))})",
            f"{out('report')} = {{name: {{'cells': int(mesh.n_cells), 'point_data': list(mesh.point_data.keys()), 'cell_data': list(mesh.cell_data.keys())}} for name, mesh in _boundaries_{index}.items()}}",
            f"display({out('report')})",
        ]
        for name in ["top","bottom","north","south","east","west","side_mesh","full_shell"]:
            if name in outputs:
                lines.append(f"{out(name)} = _boundary_files_{index}[{_py(name)}]['vtp']")
            if f"{name}_vtu" in outputs:
                lines.append(f"{out(name + '_vtu')} = _boundary_files_{index}[{_py(name)}]['vtu']")
        if "side_mesh_obj" in outputs: lines.append(f"{out('side_mesh_obj')} = _boundaries_{index}['side_mesh']")
        if "full_shell_obj" in outputs: lines.append(f"{out('full_shell_obj')} = _boundaries_{index}['full_shell']")
        if "selected_preview" in outputs: lines.append(f"{out('selected_preview')} = None")

    elif node_type == "RepairReorderVoxelMesh":
        source = _one(inputs, "mesh", "None")
        if source == "None":
            selected = _file_expr(params, manifest_by_id, "mesh_file_id")
            source = f"load_input_file({selected}).get('mesh')"
        file_name = str(params.get("file_name") or "repaired_reordered_voxel_model.vtu")
        lines += [
            f"{out('mesh')} = repair_reorder_voxel_mesh({source}, **{_py({k: v for k, v in params.items() if k not in {'mesh_file_id', 'file_name', 'preview_scalar', 'show_edges'}})})",
            f"{out('voxel_model')} = {out('mesh')}",
            f"{out('file')} = save_mesh({out('mesh')}, OUTPUT_DIR / {_py(file_name)})",
            f"{out('report')} = {{'n_cells': int({out('mesh')}.n_cells), 'n_points': int({out('mesh')}.n_points), 'materials': np.unique(np.asarray({out('mesh')}.cell_data[{_py(params.get('material_array', 'MaterialIDs'))}])).tolist()}}",
            f"display({out('report')})",
        ]

    elif node_type == "OGSIdentifyFullMesh":
        source = _one(inputs, "mesh")
        file_name = str(params.get("file_name") or "identified_full_mesh.vtu")
        lines += [
            f"{out('file')} = OUTPUT_DIR / {_py(file_name)}",
            f"{out('mesh')} = ogs_identify_full_mesh({source}, {out('file')}, executable_path={_py(params.get('executable_path', ''))}, ogs_bin_dir={_py(params.get('ogs_bin_dir', ''))}, search_length={float(params.get('search_length', 1e-6) or 1e-6)})",
            f"{out('report')} = {{'point_data': list({out('mesh')}.point_data.keys()), 'cell_data': list({out('mesh')}.cell_data.keys())}}",
            f"display({out('report')})",
            f"visualize({out('mesh')}, scalars={_py(params.get('preview_scalar', 'MaterialIDs'))}, show_edges={bool(params.get('show_edges', True))})",
        ]

    elif node_type == "SaveTableCsv":
        source = _one(inputs, "table")
        file_name = str(params.get("file_name") or "table.csv")
        lines += [f"{out('file')} = save_table_csv({source}, OUTPUT_DIR / {_py(file_name)})", f"print('Saved:', {out('file')})"]

    elif node_type == "SaveArray":
        source = _one(inputs, "array")
        fmt = str(params.get("format") or "npy")
        file_name = str(params.get("file_name") or f"array.{fmt}")
        lines += [f"{out('file')} = save_array({source}, OUTPUT_DIR / {_py(file_name)}, fmt={_py(fmt)})", f"print('Saved:', {out('file')})"]

    elif node_type == "SaveGemPyModelJson":
        source = _one(inputs, "geo_model")
        file_name = str(params.get("file_name") or "gempy_model.json")
        lines += [f"{out('file')} = save_gempy_model_json({source}, OUTPUT_DIR / {_py(file_name)})", f"print('Saved:', {out('file')})"]

    elif node_type == "UploadGeoDTStructureVersionToKadi":
        role_names = ["volume", "south", "north", "bottom", "top", "west", "east", "side_mesh", "full_shell"]
        file_entries = []
        for role in role_names:
            file_entries.append(f"{_py(role)}: {_one(inputs, role)}")
        lines += [
            f"_geodt_files_{index} = {{{', '.join(file_entries)}}}",
            f"{out('report')} = upload_geodt_structure_version_to_kadi(_geodt_files_{index}, title={_py(params.get('title', 'GeoDT Input Structure Model'))}, description={_py(params.get('description', ''))}, subject={_py(params.get('subject', 'Structure model in voxel grid from GemPy'))}, collection_id={int(params.get('collection_id', 7238) or 7238)}, description_record_id={int(params.get('description_record_id', 80295) or 80295)}, version={_py(params.get('version', '0.1'))}, tag={_py(params.get('tag', 'geolab'))}, update_description_record={bool(params.get('update_description_record', True))}, add_group_roles={bool(params.get('add_group_roles', True))}, force_upload={bool(params.get('force_upload', True))}, dry_run={bool(params.get('dry_run', False))}, output_dir=OUTPUT_DIR / 'kadi_upload')",
            f"display({out('report')})",
        ]

    elif node_type == "UploadFileToKadi":
        source = _one(inputs, "file")
        record_id = params.get("record_id") or ""
        lines += [
            f"{out('report')} = upload_file_to_kadi({source}, record_id={_py(str(record_id))}, force={bool(params.get('force', False))})",
            f"display({out('report')})",
        ]

    else:
        raise NotebookExportError(_unsupported_message([node_type]))

    return "\n".join(lines) + "\n"


def build_notebook(project: Dict[str, Any], input_manifest: List[Dict[str, Any]], include_workflow_json: bool = True) -> Dict[str, Any]:
    graph = project.get("graph") or project
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    if not nodes:
        raise NotebookExportError("The workflow does not contain any nodes.")

    unsupported = sorted({str(node.get("type")) for node in nodes if str(node.get("type")) not in SUPPORTED_NODE_TYPES})
    if unsupported:
        raise NotebookExportError(_unsupported_message(unsupported))

    order = _topological_order(nodes, edges)
    node_by_id = {str(node.get("id")): node for node in nodes}
    ordered_nodes = [node_by_id[node_id] for node_id in order]
    port_vars = _build_port_variables(ordered_nodes)
    meta = _node_meta_map()
    manifest_by_id = {str(item.get("file_id")): item for item in input_manifest}

    workflow_lines = [
        f"{index}. **{meta.get(str(node.get('type')), {}).get('title', node.get('type'))}** (`{node.get('type')}`)"
        for index, node in enumerate(ordered_nodes, start=1)
    ]

    input_files_dict = {
        str(item.get("file_id")): str(item.get("bundle_name"))
        for item in input_manifest
    }

    cells: List[Dict[str, Any]] = []
    cells.append(_markdown_cell(
        "# Standalone workflow exported from GemPy Node Editor\n\n"
        "This is a **normal Python/Jupyter workflow**, not a copy of the node editor runtime. "
        "The notebook uses ordinary Python variables and direct pandas, GemPy, GemPy Viewer and PyVista function calls. "
        "All helper function definitions required by the exported workflow are included in the notebook.\n\n"
        "## Workflow\n\n" + "\n".join(workflow_lines)
    ))
    cells.append(_markdown_cell(
        "## Installation\n\n"
        "The portable ZIP contains `requirements.txt`. Install it in the active environment before running the notebook:\n\n"
        "```bash\npython -m pip install -r requirements.txt\n```\n\n"
        "GemPy's PyTorch backend may require a system-specific PyTorch installation."
    ))
    cells.append(_code_cell(
        "from pathlib import Path\n"
        "from IPython.display import display\n"
        "import json\n"
        "import numpy as np\n"
        "import pandas as pd\n\n"
        "NOTEBOOK_DIR = Path.cwd()\n"
        "INPUT_DIR = NOTEBOOK_DIR / 'inputs'\n"
        "OUTPUT_DIR = NOTEBOOK_DIR / 'outputs'\n"
        "OUTPUT_DIR.mkdir(parents=True, exist_ok=True)\n\n"
        f"INPUT_FILE_NAMES = {_py(input_files_dict)}\n"
        "INPUT_FILES = {file_id: INPUT_DIR / name for file_id, name in INPUT_FILE_NAMES.items()}\n"
        "display(INPUT_FILES)\n",
        tags=["configuration"],
    ))

    helper_source = HELPER_SOURCE_PATH.read_text(encoding="utf-8")
    geometry_source = (APP_ROOT / "notebook_geometry_helpers.py").read_text(encoding="utf-8")
    helper_source += "\nNOTEBOOK_GEOMETRY_SOURCE = " + repr(geometry_source) + "\n"
    cells.append(_markdown_cell(
        "## Standalone helper functions\n\n"
        "The following cell contains normal reusable Python functions. It does not import `NODE_REGISTRY`, `RuntimeValue`, "
        "the FastAPI application, or any node-editor execution classes."
    ))
    cells.append(_code_cell(helper_source, tags=["standalone-functions"]))

    incoming_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        incoming_by_node[str(edge.get("to_node"))].append(edge)

    for index, node in enumerate(ordered_nodes, start=1):
        node_id = str(node.get("id"))
        node_type = str(node.get("type"))
        title = meta.get(node_type, {}).get("title", node_type)
        description = meta.get(node_type, {}).get("description", "")
        connection_lines = []
        for edge in incoming_by_node.get(node_id, []):
            source = node_by_id[str(edge.get("from_node"))]
            source_title = meta.get(str(source.get("type")), {}).get("title", source.get("type"))
            connection_lines.append(f"- `{source_title}.{edge.get('from_port')}` → `{edge.get('to_port')}`")
        if not connection_lines:
            connection_lines.append("- No connected upstream inputs.")
        cells.append(_markdown_cell(
            f"## Step {index}: {title}\n\n"
            f"{description}\n\n"
            "**Inputs**\n\n" + "\n".join(connection_lines) + "\n\n"
            "<details><summary>Exported parameters</summary>\n\n"
            "```json\n" + json.dumps(_json_safe(node.get("params") or {}), ensure_ascii=False, indent=2) + "\n```\n"
            "</details>"
        ))
        cells.append(_code_cell(
            _node_code(index, node, edges, port_vars, manifest_by_id),
            tags=["workflow-step", f"node-{_safe_identifier(node_id)}"],
        ))

    cells.append(_markdown_cell(
        "## Generated output files\n\n"
        "This final cell lists files saved by the workflow under `outputs/`."
    ))
    cells.append(_code_cell(
        "print('Output directory:', OUTPUT_DIR)\n"
        "if OUTPUT_DIR.exists():\n"
        "    for path in sorted(OUTPUT_DIR.rglob('*')):\n"
        "        if path.is_file():\n"
        "            print(' -', path.relative_to(NOTEBOOK_DIR))\n"
    ))

    if include_workflow_json:
        cells.append(_markdown_cell(
            "## Original visual workflow JSON\n\n"
            "This data is preserved only for traceability. It is not used to execute the notebook."
        ))
        cells.append(_code_cell(
            "ORIGINAL_WORKFLOW = " + _py(project) + "\nORIGINAL_WORKFLOW",
            tags=["workflow-json"],
            jupyter={"source_hidden": True},
        ))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3", "mimetype": "text/x-python", "codemirror_mode": {"name": "ipython", "version": 3}, "pygments_lexer": "ipython3", "nbconvert_exporter": "python", "file_extension": ".py"},
            "gempy_node_editor": {
                "version": "0.1",
                "schema_version": "gempy-node-editor-standalone-notebook-v2",
                "execution_style": "direct-python-functions",
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _requirements_text() -> str:
    path = PROJECT_ROOT / "requirements.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "pandas\nnumpy\nscipy\nmatplotlib\npyvista\nvtk\ngempy\ngempy-viewer\nkadi-apy\n"


def create_notebook_export(project: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Tuple[Path, str, str]:
    options = dict(options or {})
    package_mode = str(options.get("package_mode") or "portable_zip")
    include_inputs = bool(options.get("include_inputs", True))
    include_requirements = bool(options.get("include_requirements", True))
    include_workflow_json = bool(options.get("include_workflow_json", True))

    input_manifest, files_to_bundle = _input_manifest(project, include_inputs=include_inputs)
    notebook = build_notebook(project, input_manifest=input_manifest, include_workflow_json=include_workflow_json)
    notebook_bytes = json.dumps(notebook, ensure_ascii=False, indent=1).encode("utf-8")

    if package_mode == "ipynb":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ipynb") as temporary:
            temporary.write(notebook_bytes)
            path = Path(temporary.name)
        return path, "gempy_workflow_standalone.ipynb", "application/x-ipynb+json"

    if package_mode != "portable_zip":
        raise NotebookExportError(f"Unknown notebook export package_mode: {package_mode}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temporary:
        zip_path = Path(temporary.name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("workflow_standalone.ipynb", notebook_bytes)
        if include_requirements:
            archive.writestr("requirements.txt", _requirements_text())
        if include_workflow_json:
            archive.writestr("workflow.json", json.dumps(_json_safe(project), ensure_ascii=False, indent=2))
        for source_path, archive_name in files_to_bundle:
            if source_path.exists():
                archive.write(source_path, arcname=archive_name)
        archive.writestr(
            "README.txt",
            "Standalone GemPy Node Editor notebook export\n"
            "=============================================\n\n"
            "The notebook uses ordinary Python variables and direct function calls.\n"
            "It does not use NODE_REGISTRY, RuntimeValue, BaseNode, GraphExecutor,\n"
            "or the FastAPI application.\n\n"
            "1. Extract this ZIP.\n"
            "2. Activate a Python environment.\n"
            "3. Run: python -m pip install -r requirements.txt\n"
            "4. Open workflow_standalone.ipynb.\n"
            "5. Execute the cells from top to bottom.\n",
        )

    return zip_path, "gempy_workflow_standalone_notebook.zip", "application/zip"
