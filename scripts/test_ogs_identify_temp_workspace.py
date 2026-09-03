from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pyvista as pv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import notebook_standalone_runtime as standalone  # noqa: E402
from app import voxel_repair_nodes as editor  # noqa: E402


def _fake_ogs_run(command, check=True):
    assert check is True
    prefix = Path(command[command.index("-o") + 1])
    input_path = Path(command[-1])
    assert input_path.parent == prefix.parent
    assert "gempy_ogs_identify_" in prefix.parent.name

    mesh = pv.read(input_path).cast_to_unstructured_grid()
    mesh.point_data["bulk_node_ids"] = np.arange(mesh.n_points, dtype=np.uint64)
    mesh.cell_data["bulk_element_ids"] = np.arange(mesh.n_cells, dtype=np.uint64)
    generated_path = prefix.parent / f"{prefix.name}{input_path.name}"
    mesh.save(generated_path, binary=True)


def _mesh() -> pv.UnstructuredGrid:
    grid = pv.ImageData(dimensions=(3, 3, 3), spacing=(1, 1, 1))
    result = grid.cast_to_unstructured_grid()
    result.cell_data["MaterialIDs"] = np.ones(result.n_cells, dtype=np.int32)
    return result


def test_candidate_sort_ignores_disappeared_paths(root: Path) -> None:
    older = root / "older.vtu"
    newer = root / "newer.vtu"
    missing = root / "already_gone.vtu"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    assert editor._newest_existing_paths([missing, older, newer]) == [newer, older]
    assert standalone._newest_existing_paths([missing, older, newer]) == [newer, older]


def test_editor_ogs_uses_isolated_temp_directory(root: Path) -> None:
    input_path = root / "editor_input.vtu"
    output_path = root / "editor_output.vtu"
    _mesh().save(input_path, binary=True)
    editor.pv = pv
    with patch.object(editor, "find_identify_subdomains_executable", return_value=Path(sys.executable)), patch.object(editor.subprocess, "run", side_effect=_fake_ogs_run):
        result_path = editor.apply_ogs_identify_to_full_mesh(input_path, output_path, None)
    assert result_path == output_path.resolve()
    result = pv.read(result_path)
    assert "bulk_node_ids" in result.point_data
    assert "bulk_element_ids" in result.cell_data


def test_standalone_ogs_uses_isolated_temp_directory(root: Path) -> None:
    output_path = root / "standalone_output.vtu"
    with patch.object(standalone, "find_identify_subdomains", return_value=Path(sys.executable)), patch.object(standalone.subprocess, "run", side_effect=_fake_ogs_run):
        result = standalone.ogs_identify_full_mesh(_mesh(), output_path)
    assert output_path.exists()
    assert "bulk_node_ids" in result.point_data
    assert "bulk_element_ids" in result.cell_data


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gempy_ogs_regression_") as temporary_directory:
        root = Path(temporary_directory)
        test_candidate_sort_ignores_disappeared_paths(root)
        test_editor_ogs_uses_isolated_temp_directory(root)
        test_standalone_ogs_uses_isolated_temp_directory(root)
    print("OGS temporary-workspace regression tests passed.")


if __name__ == "__main__":
    main()
