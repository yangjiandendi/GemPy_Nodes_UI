from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import multiprocessing
import json
import pickle
import zipfile
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .graph_executor import GraphExecutor
from .models import GraphRequest
from .node_registry import NODE_TYPES
from .notebook_export import create_notebook_export
from .runtime_store import cleanup_runtime_objects, get_runtime_object
from .storage import (
    WORKSPACE,
    UPLOAD_DIR,
    cleanup_generated_files,
    cleanup_missing_files,
    get_file_record,
    get_file_path,
    import_uploaded_file_with_record,
    list_files,
    register_uploaded_file,
    update_file_category,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
EXAMPLES_DIR = ROOT / "examples"

app = FastAPI(title="GemPy Node Editor", version="0.1.0")
executor = GraphExecutor()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _cleanup_generated_files_on_startup() -> None:
    """Clean stale generated files left by a previous non-graceful shutdown."""
    try:
        stats = cleanup_generated_files(include_outputs=True, include_tmp=True, include_popup_tmp=True, include_gempy_popups=True)
        print(f"[GemPy Node Editor] Startup generated-file cleanup: {stats}", flush=True)
    except Exception as exc:
        print(f"[GemPy Node Editor] Startup cleanup failed: {exc}", flush=True)


@app.on_event("shutdown")
def _cleanup_generated_files_on_shutdown() -> None:
    """Clean generated files when Uvicorn/FastAPI shuts down gracefully."""
    try:
        # Clear in-memory caches first so no stale RuntimeValue descriptors point
        # to output/tmp files after shutdown.
        try:
            executor.clear_cache()
        except Exception:
            pass
        try:
            cleanup_runtime_objects(max_age_seconds=0)
        except Exception:
            pass

        stats = cleanup_generated_files(include_outputs=True, include_tmp=True, include_popup_tmp=True, include_gempy_popups=True)
        print(f"[GemPy Node Editor] Shutdown generated-file cleanup: {stats}", flush=True)
    except Exception as exc:
        print(f"[GemPy Node Editor] Shutdown cleanup failed: {exc}", flush=True)


def _patch_pyvista_add_mesh_positional_color() -> None:
    """Allow old gempy_viewer code to run with PyVista >= 0.50.

    Some gempy_viewer versions call Plotter.add_mesh(mesh, color) with color as
    a positional argument. PyVista 0.50 made that a TypeError. This local patch
    converts that second positional argument into color=<...> before delegating
    to PyVista's original implementation.
    """
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
        # The caller will surface the real plotting/import error if plotting fails.
        pass


def _start_gui_process(target, name: str = "pyvista_gui") -> int:
    """Start a PyVista/VTK GUI in a separate process.

    On macOS, Cocoa/NSWindow must be created on the main thread. FastAPI request
    handlers may run outside the interpreter main thread, so starting PyVista
    directly in a thread can crash Python with:

        NSWindow should only be instantiated on the main thread

    Using a forked child process gives the GUI its own main thread while still
    allowing access to the already-created in-memory GemPy model on Unix/macOS.
    On non-fork platforms, this falls back to a normal multiprocessing process.
    """
    try:
        ctx = multiprocessing.get_context("fork")
    except Exception:
        ctx = multiprocessing.get_context()

    proc = ctx.Process(target=target, name=name)
    proc.daemon = False
    proc.start()
    return int(proc.pid)



def _popup_runtime_dir():
    from .storage import WORKSPACE as WORKSPACE_DIR
    d = WORKSPACE_DIR / "macos_popups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_mesh_for_standalone_popup(mesh, label: str) -> str:
    """Save an in-memory PyVista mesh to disk for a clean spawned viewer process."""
    import uuid
    safe = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(label))
    path = _popup_runtime_dir() / f"{safe}_{uuid.uuid4().hex}.vtp"
    mesh.save(path)
    return str(path)


def _launch_standalone_pyvista_popup(mesh_items, title="PyVista Viewer", show_axes=True):
    """Launch PyVista in a clean Python interpreter process.

    This avoids macOS Cocoa/NSWindow crashes caused by creating VTK windows from
    FastAPI/Uvicorn threads or forked server processes.
    """
    import uuid

    meshes = []
    for idx, item in enumerate(mesh_items):
        mesh = item.get("mesh")
        if mesh is None:
            continue
        label = item.get("label") or f"mesh_{idx}"
        meshes.append({
            "path": _save_mesh_for_standalone_popup(mesh, label),
            "label": label,
            "color": item.get("color"),
            "opacity": item.get("opacity", 1.0),
            "show_edges": item.get("show_edges", False),
            "scalars": item.get("scalars"),
        })

    cfg = {
        "title": title,
        "window_size": [1200, 850],
        "show_axes": show_axes,
        "meshes": meshes,
    }
    cfg_path = _popup_runtime_dir() / f"popup_{uuid.uuid4().hex}.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    runner = Path(__file__).resolve().parent / "pyvista_popup_standalone.py"
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(cfg_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return {"pid": int(proc.pid), "config": str(cfg_path), "mesh_count": len(meshes)}



def _launch_gempy_standalone_popup(config: Dict[str, Any]) -> Dict[str, Any]:
    """Launch GemPy/PyVista in a clean interpreter process.

    This avoids macOS Cocoa crashes from Uvicorn request threads and avoids
    forked-process thread inheritance. The GemPy model is passed through a
    temporary pickle file.
    """
    import uuid
    from .storage import WORKSPACE as WORKSPACE_DIR

    d = WORKSPACE_DIR / "gempy_popups"
    d.mkdir(parents=True, exist_ok=True)

    model = config.pop("_geo_model")
    model_path = d / f"geo_model_{uuid.uuid4().hex}.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(model, f)

    config["model_pickle"] = str(model_path)
    cfg_path = d / f"popup_{uuid.uuid4().hex}.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    runner = Path(__file__).resolve().parent / "gempy_popup_standalone.py"
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(cfg_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return {"pid": int(proc.pid), "config": str(cfg_path), "model_pickle": str(model_path)}



@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/version")
def version() -> Dict[str, Any]:
    return {
        "app": "GemPy Node Editor",
        "version": "0.1.0",
        "features": [
            "node_statuses",
            "run_log",
            "run_manifest",
            "project_json",
            "schema_checks",
            "execution_cache",
            "file_index_repair",
            "portable_project_zip",
            "macos_pyvista_process_fix",
            "file_workspace_rescan",
            "workflow_screenshot_export",
            "workflow_png_canvas_export",
            "workflow_transparent_background",
            "workflow_svg_pure_shapes",
            "ppt_compatible_svg_export",
            "workflow_export_full_text",
            "no_text_ellipsis_in_export",
            "workflow_png_dom_geometry",
        ],
    }


@app.get("/api/node-types")
def node_types() -> Dict[str, Any]:
    return {"node_types": NODE_TYPES}


@app.get("/api/execute/progress")
def execute_progress() -> Dict[str, Any]:
    return {"ok": True, "progress": executor.get_progress()}


@app.post("/api/execute/cancel")
def cancel_execute() -> Dict[str, Any]:
    return {"ok": True, "progress": executor.request_cancel()}


@app.get("/api/uploads")
def uploads(include_missing: bool = False) -> Dict[str, Any]:
    # Missing uploaded files are removed from the default UI list so stale
    # references do not remain visible after manual deletion.
    return {"files": list_files(kind="upload", include_missing=include_missing, remove_missing=not include_missing)}


@app.get("/api/files")
def files(include_missing: bool = False) -> Dict[str, Any]:
    return {"files": list_files(include_missing=include_missing, remove_missing=not include_missing)}


@app.post("/api/uploads/cleanup")
def cleanup_uploads() -> Dict[str, Any]:
    stats = cleanup_missing_files(kind="upload")
    return {"ok": True, "stats": stats, "files": list_files(kind="upload")}


@app.post("/api/uploads/category")
def set_upload_category(payload: Dict[str, Any]) -> Dict[str, Any]:
    file_id = str(payload.get("file_id") or "").strip()
    category = str(payload.get("category") or "Uncategorized").strip()
    if not file_id:
        raise HTTPException(status_code=400, detail="Missing file_id")
    try:
        file_record = update_file_category(file_id, category)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "file": {k: v for k, v in file_record.items() if k != "path"}, "files": list_files(kind="upload")}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), category: str = Form("Uncategorized")) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls", ".json", ".tif", ".tiff", ".vtk", ".vtp", ".vti", ".vtu", ".stl", ".ply", ".obj", ".npy"}:
        raise HTTPException(status_code=400, detail=f"Unsupported file suffix: {suffix}")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        record = register_uploaded_file(tmp_path, file.filename, category=category)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    return {"ok": True, "file": record}






def _example_manifest() -> Dict[str, Any]:
    manifest_path = EXAMPLES_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"schema_version": "gempy-node-editor-examples-v1", "examples": []}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"schema_version": "gempy-node-editor-examples-v1", "examples": []}
        data.setdefault("examples", [])
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read examples manifest: {exc}") from exc


def _used_upload_file_ids(project: Dict[str, Any]) -> set[str]:
    used: set[str] = set()
    for node in project.get("graph", {}).get("nodes", []):
        if not isinstance(node, dict):
            continue
        params = node.get("params") or {}
        if node.get("type") == "LoadUploadedFile" and params.get("file_id"):
            used.add(str(params.get("file_id")))
        # Some future/example nodes may reference files by *_file_id params.
        for key, value in params.items():
            if key.endswith("_file_id") and value:
                used.add(str(value))
    return used


def _find_example_upload_source(record: Dict[str, Any]) -> Path | None:
    data_dir = EXAMPLES_DIR / "data"
    stored_name = Path(str(record.get("stored_name") or "")).name
    original_name = Path(str(record.get("original_name") or "")).name
    candidates = []
    if stored_name:
        candidates.append(data_dir / stored_name)
    if original_name:
        candidates.append(data_dir / original_name)

    # Portable projects often store files with UUID prefixes; bundled examples
    # keep the clean original CSV names. If stored_name has a UUID prefix,
    # try the suffix after the first underscore.
    if "_" in stored_name:
        candidates.append(data_dir / stored_name.split("_", 1)[1])

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


@app.get("/api/examples")
def list_examples() -> Dict[str, Any]:
    return _example_manifest()


@app.post("/api/examples/{example_id}/load")
def load_example_project(example_id: str) -> Dict[str, Any]:
    manifest = _example_manifest()
    examples = manifest.get("examples") or []
    example = next((item for item in examples if item.get("id") == example_id), None)
    if example is None:
        raise HTTPException(status_code=404, detail=f"Unknown example id: {example_id}")

    project_rel = Path(str(example.get("project_file") or ""))
    if project_rel.is_absolute() or ".." in project_rel.parts:
        raise HTTPException(status_code=400, detail="Invalid example project path.")
    project_path = EXAMPLES_DIR / project_rel
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Example project file not found: {project_rel}")

    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read example project: {exc}") from exc

    used_ids = _used_upload_file_ids(project)
    imported = []
    missing = []

    for rec in project.get("uploads", []) or []:
        if not isinstance(rec, dict):
            continue
        fid = str(rec.get("file_id") or "")
        if used_ids and fid not in used_ids:
            continue
        src = _find_example_upload_source(rec)
        if src is None:
            missing.append(rec.get("original_name") or rec.get("stored_name") or fid)
            continue
        imported.append(import_uploaded_file_with_record(src, rec))

    return {
        "ok": True,
        "example": example,
        "project": project,
        "imported_files": imported,
        "missing_files": missing,
        "files": list_files(kind="upload"),
    }


@app.post("/api/project/export")
def export_portable_project(project: Dict[str, Any]) -> StreamingResponse:
    """Export the current workflow plus uploaded input files as a portable ZIP.

    A plain graph/project JSON only stores file ids.  Those ids are not enough
    on another computer unless the upload workspace is copied too.  This ZIP
    contains project.json and all currently available uploaded files, preserving
    file_id values so existing Load Uploaded File nodes continue to work after
    import.
    """
    files = list_files(kind="upload", include_missing=False, remove_missing=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        zip_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            portable_project = dict(project or {})
            portable_project["schema_version"] = "gempy-node-editor-portable-project-v19"
            portable_project["exported_uploads"] = files
            zf.writestr("project.json", json.dumps(portable_project, indent=2, ensure_ascii=False))
            for rec in files:
                try:
                    # Use the full record so get_file_path can repair paths if needed.
                    path = get_file_path(rec["file_id"])
                except Exception:
                    continue
                arc = f"uploads/{rec.get('stored_name') or path.name}"
                zf.write(path, arcname=arc)
    except Exception:
        try:
            zip_path.unlink()
        except Exception:
            pass
        raise

    def _iter_file():
        try:
            with open(zip_path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                zip_path.unlink()
            except Exception:
                pass

    headers = {"Content-Disposition": "attachment; filename=gempy_node_portable_project.zip"}
    return StreamingResponse(_iter_file(), media_type="application/zip", headers=headers)


@app.post("/api/notebook/export")
def export_workflow_notebook(payload: Dict[str, Any]) -> StreamingResponse:
    """Export a graph as a stepwise standalone Jupyter notebook.

    The portable ZIP mode bundles the notebook, requirements, original workflow
    JSON and referenced input files. The notebook contains ordinary Python helper
    functions and direct pandas/GemPy/PyVista calls; it does not execute NodeRegistry
    classes or RuntimeValue objects.
    """
    project = payload.get("project") if isinstance(payload, dict) else None
    options = payload.get("options") if isinstance(payload, dict) else None
    if not isinstance(project, dict):
        raise HTTPException(status_code=400, detail="Missing notebook export project payload.")

    try:
        export_path, download_name, media_type = create_notebook_export(
            project,
            options=options if isinstance(options, dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Notebook export failed: {exc}") from exc

    def _iter_export():
        try:
            with open(export_path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                export_path.unlink()
            except Exception:
                pass

    headers = {"Content-Disposition": f'attachment; filename="{download_name}"'}
    return StreamingResponse(_iter_export(), media_type=media_type, headers=headers)


@app.post("/api/project/import")
async def import_portable_project(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a portable project .zip file.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp.write(await file.read())
        zip_path = Path(tmp.name)
    imported = []
    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(td_path)
            project_path = td_path / "project.json"
            if not project_path.exists():
                raise HTTPException(status_code=400, detail="ZIP does not contain project.json.")
            project = json.loads(project_path.read_text(encoding="utf-8"))
            upload_records = project.get("exported_uploads") or project.get("uploads") or []
            for rec in upload_records:
                if not isinstance(rec, dict):
                    continue
                stored_name = Path(str(rec.get("stored_name") or "")).name
                candidates = []
                if stored_name:
                    candidates.append(td_path / "uploads" / stored_name)
                original_name = str(rec.get("original_name") or "")
                if original_name:
                    candidates.append(td_path / "uploads" / original_name)
                src = next((c for c in candidates if c.exists()), None)
                if src is None:
                    # Last-resort: match by file_id prefix.
                    fid = str(rec.get("file_id") or "")
                    if fid:
                        matches = list((td_path / "uploads").glob(f"{fid}_*")) if (td_path / "uploads").exists() else []
                        src = matches[0] if matches else None
                if src is not None:
                    imported.append(import_uploaded_file_with_record(src, rec))
    finally:
        try:
            zip_path.unlink()
        except Exception:
            pass
    return {"ok": True, "project": project, "imported_files": imported, "files": list_files(kind="upload")}

@app.get("/api/download/{file_id}")
def download(file_id: str) -> FileResponse:
    try:
        record = get_file_record(file_id)
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=record.get("original_name") or path.name)


@app.get("/api/raw/{file_id}")
def raw_file(file_id: str) -> FileResponse:
    """Serve a stored file inline, mainly for PNG plot previews in the inspector."""
    try:
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path)


@app.get("/api/pyvista/table/{file_id}")
def open_pyvista_table(file_id: str, x: str = "X", y: str = "Y", z: str = "Z", formation: str = "formation") -> Dict[str, Any]:
    """Open a local PyVista desktop window for a saved table preview.

    This intentionally starts a separate Python process so the FastAPI request
    can return immediately while the PyVista window stays open.
    """
    try:
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        import pyvista  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PyVista is not installed/importable in this Python environment: {exc}. Install requirements-gempy.txt.") from exc
    script = ROOT / "pyvista_preview.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="PyVista preview script is missing.")
    cmd = [sys.executable, str(script), str(path), "--x", x, "--y", y, "--z", z, "--formation", formation]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT.parent))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start PyVista preview process: {exc}") from exc
    return {"ok": True, "message": "PyVista preview process started. A local 3D window should open shortly."}


@app.get("/api/pyvista/voxel/{file_id}")
def open_pyvista_voxel(
    file_id: str,
    reshape: str = "",
    extent: str = "",
    show_edges: bool = False,
    threshold_background: bool = False,
) -> Dict[str, Any]:
    try:
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        import pyvista  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PyVista is not installed/importable in this Python environment: {exc}. Install requirements-gempy.txt.") from exc
    script = ROOT / "pyvista_voxel_preview.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="PyVista voxel preview script is missing.")
    cmd = [sys.executable, str(script), str(path), "--reshape", reshape or "", "--extent", extent or ""]
    if show_edges:
        cmd.append("--show-edges")
    if threshold_background:
        cmd.append("--threshold-background")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT.parent))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start PyVista voxel preview process: {exc}") from exc
    return {"ok": True, "message": "PyVista voxel preview process started. A local 3D window should open shortly."}


@app.get("/api/pyvista/mesh/{file_id}")
def open_pyvista_mesh(file_id: str, scalars: str = "", show_edges: bool = False, vector: str = "") -> Dict[str, Any]:
    try:
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        import pyvista  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PyVista is not installed/importable in this Python environment: {exc}. Install requirements-gempy.txt.") from exc
    script = ROOT / "pyvista_mesh_preview.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="PyVista mesh preview script is missing.")
    cmd = [sys.executable, str(script), str(path), "--scalars", scalars or ""]
    if vector:
        cmd.extend(["--vector", vector])
    if show_edges:
        cmd.append("--show-edges")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT.parent))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start PyVista mesh preview process: {exc}") from exc
    return {"ok": True, "message": "PyVista mesh preview process started. A local 3D window should open shortly."}



@app.get("/api/pyvista/boundaries/{file_id}")
def open_pyvista_boundaries(file_id: str) -> Dict[str, Any]:
    try:
        path = get_file_path(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        import pyvista  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PyVista is not installed/importable in this Python environment: {exc}. Install requirements-gempy.txt.") from exc
    script = ROOT / "pyvista_boundaries_preview.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="PyVista boundaries preview script is missing.")
    cmd = [sys.executable, str(script), str(path)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT.parent))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start PyVista boundaries preview process: {exc}") from exc
    return {"ok": True, "message": "PyVista boundaries preview process started. A local 3D window should open shortly."}


@app.get("/api/pyvista/gempy-model/{token}")
def open_pyvista_gempy_model(
    token: str,
    show_data: bool = True,
    show_lith: bool = True,
    show_surfaces: bool = True,
    show_topography: bool = True,
    show_boundaries: bool = True,
) -> Dict[str, Any]:
    cleanup_runtime_objects()
    try:
        stored = get_runtime_object(token, expected_kind="geo_model")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        import gempy_viewer  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"gempy_viewer is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    try:
        launched = _launch_gempy_standalone_popup({
            "mode": "gempy_3d",
            "_geo_model": stored.value,
            "show_data": show_data,
            "show_lith": show_lith,
            "show_surfaces": show_surfaces,
            "show_topography": show_topography,
            "show_boundaries": show_boundaries,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start standalone GemPy 3D viewer: {exc}") from exc

    return {
        "ok": True,
        **launched,
        "viewer_flags": {
            "show_data": show_data,
            "show_lith": show_lith,
            "show_surfaces": show_surfaces,
            "show_topography": show_topography,
            "show_boundaries": show_boundaries,
        },
        "message": "GemPy 3D viewer started in a clean standalone Python process.",
    }

@app.get("/api/pyvista/gempy-clipped-layers/{token}")
def open_pyvista_gempy_clipped_layers(
    token: str,
    clip_file_id: str = "",
    topography_file_id: str = "",
    cell_data_name: str = "id",
    layer_styles: str = "",
    invert: bool = False,
    crinkle: bool = True,
    topography_clip_enabled: bool = True,
    topography_invert: bool = False,
    crop_to_topography_xy: bool = True,
    show_edges: bool = False,
    show_base_gempy: bool = False,
    show_clip_mesh: bool = False,
    show_topography_mesh: bool = False,
) -> Dict[str, Any]:
    cleanup_runtime_objects()
    try:
        stored = get_runtime_object(token, expected_kind="geo_model")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    clip_path = None
    if clip_file_id:
        try:
            clip_path = get_file_path(clip_file_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Clipping mesh not found: {exc}") from exc

    top_path = None
    if topography_file_id:
        try:
            top_path = get_file_path(topography_file_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Topography/DEM mesh not found: {exc}") from exc

    try:
        import pyvista  # noqa: F401
        import gempy_viewer  # noqa: F401
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PyVista/gempy_viewer is not installed/importable: {exc}. Install requirements-gempy.txt.") from exc

    try:
        launched = _launch_gempy_standalone_popup({
            "mode": "clipped_layers",
            "_geo_model": stored.value,
            "clip_path": str(clip_path) if clip_path is not None else "",
            "top_path": str(top_path) if top_path is not None else "",
            "cell_data_name": cell_data_name,
            "layer_styles": layer_styles,
            "invert": invert,
            "crinkle": crinkle,
            "topography_clip_enabled": topography_clip_enabled,
            "topography_invert": topography_invert,
            "crop_to_topography_xy": crop_to_topography_xy,
            "show_edges": show_edges,
            "show_base_gempy": show_base_gempy,
            "show_clip_mesh": show_clip_mesh,
            "show_topography_mesh": show_topography_mesh,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start standalone clipped-layer viewer: {exc}") from exc

    return {"ok": True, **launched, "message": "Clipped layer viewer started in a clean standalone Python process."}

@app.post("/api/cache/clear")
def clear_execution_cache() -> Dict[str, Any]:
    removed = executor.clear_cache()
    return {"ok": True, "removed": removed}


@app.post("/api/execute")
def execute_graph(graph: GraphRequest) -> Dict[str, Any]:
    try:
        return executor.execute(graph)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
