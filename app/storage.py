from __future__ import annotations

import json
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
UPLOAD_DIR = WORKSPACE / "uploads"
OUTPUT_DIR = WORKSPACE / "outputs"
TMP_DIR = WORKSPACE / "tmp"
INDEX_PATH = WORKSPACE / "files_index.json"
CATEGORY_INDEX_PATH = WORKSPACE / "file_categories.json"

for p in (WORKSPACE, UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
    p.mkdir(parents=True, exist_ok=True)


def _load_index() -> Dict[str, Dict[str, Any]]:
    if not INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(index: Dict[str, Dict[str, Any]]) -> None:
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def _safe_name(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in (".", "_", "-", " "):
            keep.append(ch)
        else:
            keep.append("_")
    out = "".join(keep).strip().replace(" ", "_")
    return out or "file"




def _safe_category(category: Optional[str]) -> str:
    raw = str(category or "Uncategorized").strip().replace("\\\\", "/")
    raw = raw.strip("/")
    if not raw:
        return "Uncategorized"
    parts = []
    for part in raw.split("/"):
        safe = _safe_name(part)
        if safe in {".", "..", ""}:
            continue
        parts.append(safe)
    return "/".join(parts) if parts else "Uncategorized"



def _load_category_index() -> Dict[str, str]:
    if not CATEGORY_INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(CATEGORY_INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): _safe_category(v) for k, v in data.items() if str(k).strip()}
    except Exception:
        return {}


def _save_category_index(categories: Dict[str, str]) -> None:
    CATEGORY_INDEX_PATH.write_text(json.dumps(categories, indent=2, ensure_ascii=False), encoding="utf-8")


def _category_keys(file_id: str, record: Dict[str, Any]) -> List[str]:
    keys = []
    for value in [
        file_id,
        record.get("file_id"),
        record.get("stored_name"),
        record.get("relative_path"),
        record.get("original_name"),
    ]:
        if value:
            keys.append(str(value))
    # Deduplicate while preserving order.
    out = []
    seen = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _lookup_persisted_category(file_id: str, record: Dict[str, Any]) -> Optional[str]:
    cats = _load_category_index()
    for key in _category_keys(file_id, record):
        if key in cats and str(cats[key]).strip():
            return _safe_category(cats[key])
    return None


def _remember_file_category(file_id: str, record: Dict[str, Any], category: Optional[str] = None) -> None:
    cat = _safe_category(category or record.get("category") or record.get("folder") or "Uncategorized")
    cats = _load_category_index()
    changed = False
    for key in _category_keys(file_id, {**record, "file_id": file_id}):
        if cats.get(key) != cat:
            cats[key] = cat
            changed = True
    if changed:
        _save_category_index(cats)


def _kind_dir(kind: Optional[str]) -> Path:
    return OUTPUT_DIR if kind == "output" else UPLOAD_DIR


def _candidate_paths(record: Dict[str, Any]) -> List[Path]:
    """Return possible locations for a stored file.

    Older versions saved absolute paths in files_index.json.  Those absolute
    paths break when the project folder is moved or copied to a new computer.
    v19 therefore resolves files primarily from stored_name relative to the
    current workspace and only uses the old absolute path as a fallback.
    """
    candidates: List[Path] = []
    kind = record.get("kind")
    stored_name = record.get("stored_name") or record.get("relative_path")

    if stored_name:
        stored = Path(str(stored_name))
        if stored.is_absolute():
            candidates.append(stored)
        else:
            # New/portable location.
            candidates.append(_kind_dir(kind) / stored.name)
            # If the relative_path included a subfolder, try that too.
            candidates.append(WORKSPACE / stored)

    raw_path = record.get("path")
    if raw_path:
        try:
            candidates.append(Path(str(raw_path)))
        except Exception:
            pass

    # Deduplicate while preserving order.
    seen = set()
    out: List[Path] = []
    for c in candidates:
        key = str(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _resolve_existing_path(record: Dict[str, Any]) -> Optional[Path]:
    for path in _candidate_paths(record):
        if path.exists():
            return path
    return None


def _normalize_record(file_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Add portable metadata and repair stale absolute paths when possible."""
    out = dict(record)
    out.setdefault("file_id", file_id)
    out.setdefault("kind", "upload")
    persisted_category = _lookup_persisted_category(file_id, out)
    out["category"] = _safe_category(out.get("category") or out.get("folder") or persisted_category or "Uncategorized")
    if not out.get("original_name"):
        out["original_name"] = out.get("stored_name") or out.get("path") or file_id

    existing = _resolve_existing_path(out)
    if existing is not None:
        out["path"] = str(existing)
        # stored_name should be the basename under uploads/outputs for portability.
        if existing.parent in {UPLOAD_DIR, OUTPUT_DIR}:
            out["stored_name"] = existing.name
        elif not out.get("stored_name"):
            out["stored_name"] = existing.name
        out["relative_path"] = f"{out.get('kind', 'upload')}s/{out.get('stored_name', existing.name)}" if out.get("kind") in {"upload", "output"} else out.get("stored_name", existing.name)
        out["exists"] = True
    else:
        out["exists"] = False
    return out



_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _infer_record_from_workspace_file(path: Path, kind: str) -> Tuple[str, Dict[str, Any]]:
    """Create a file-index record for an existing workspace file.

    Uploaded/output files are stored as:
        <file_id>_<safe_original_name>

    If files_index.json is missing or stale but the files are still present in
    workspace/uploads, this reconstructs the record so the left Files panel and
    existing graph file_id references work again after restarting or moving the
    project folder.
    """
    stored_name = path.name
    prefix, sep, rest = stored_name.partition("_")
    if sep and _UUID_RE.match(prefix):
        file_id = prefix
        original_name = rest or stored_name
    else:
        # No id prefix. Use a stable id based on filename so repeated rescans do
        # not create duplicates.
        file_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gempy-node-editor:{kind}:{stored_name}"))
        original_name = stored_name

    rel_folder = "uploads" if kind == "upload" else "outputs"
    record = {
        "file_id": file_id,
        "kind": kind,
        "original_name": original_name,
        "stored_name": stored_name,
        "relative_path": f"{rel_folder}/{stored_name}",
        "path": str(path),
        "category": _lookup_persisted_category(file_id, {"stored_name": stored_name, "relative_path": f"{rel_folder}/{stored_name}", "original_name": original_name}) or ("Outputs" if kind == "output" else "Uncategorized"),
    }
    return file_id, record


def _scan_workspace_files(index: Dict[str, Dict[str, Any]], kind: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Add existing workspace files missing from files_index.json."""
    changed = 0
    scan_pairs = []
    if kind in (None, "upload"):
        scan_pairs.append(("upload", UPLOAD_DIR))
    # Outputs are intentionally not shown in the left Files panel, but they can
    # still be reconstructed when the full index is requested.
    if kind in (None, "output"):
        scan_pairs.append(("output", OUTPUT_DIR))

    existing_stored_names = {
        str(rec.get("stored_name") or Path(str(rec.get("path") or "")).name)
        for rec in index.values()
    }

    for scan_kind, folder in scan_pairs:
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if not path.is_file():
                continue
            if path.name in existing_stored_names:
                continue
            file_id, record = _infer_record_from_workspace_file(path, scan_kind)
            if file_id not in index:
                index[file_id] = record
                existing_stored_names.add(path.name)
                changed += 1

    return index, changed

def repair_index(remove_missing: bool = False, kind: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Repair stale paths in files_index.json and optionally remove missing records."""
    index = _load_index()
    index, discovered = _scan_workspace_files(index, kind=kind)
    repaired: Dict[str, Dict[str, Any]] = {}
    stats = {"total": len(index), "repaired": 0, "missing": 0, "removed": 0, "discovered": discovered}
    changed = bool(discovered)

    for file_id, record in index.items():
        if kind is not None and record.get("kind") != kind:
            # Keep unrelated records untouched.
            repaired[file_id] = record
            continue
        normalized = _normalize_record(file_id, record)
        if (
            normalized.get("path") != record.get("path")
            or normalized.get("stored_name") != record.get("stored_name")
            or normalized.get("relative_path") != record.get("relative_path")
            or normalized.get("category") != record.get("category")
        ):
            stats["repaired"] += 1
            changed = True
        if normalized.get("category"):
            _remember_file_category(file_id, normalized, normalized.get("category"))
        if not normalized.get("exists"):
            stats["missing"] += 1
            if remove_missing:
                stats["removed"] += 1
                changed = True
                continue
        repaired[file_id] = normalized

    if changed:
        # Do not persist the transient exists flag; it is recomputed on demand.
        persisted = {fid: {k: v for k, v in rec.items() if k != "exists"} for fid, rec in repaired.items()}
        _save_index(persisted)
        # Re-load normalized index for the return value.
        repaired = {fid: _normalize_record(fid, rec) for fid, rec in persisted.items()}
    return repaired, stats


def register_uploaded_file(src_path: Path, original_name: str, file_id: Optional[str] = None, category: Optional[str] = None) -> Dict[str, Any]:
    file_id = file_id or str(uuid.uuid4())
    safe = _safe_name(original_name)
    category = _safe_category(category)
    target = UPLOAD_DIR / f"{file_id}_{safe}"
    shutil.copyfile(src_path, target)
    index = _load_index()
    record = {
        "file_id": file_id,
        "kind": "upload",
        "original_name": original_name,
        "stored_name": target.name,
        "relative_path": f"uploads/{target.name}",
        "path": str(target),
        "category": category,
    }
    index[file_id] = record
    _save_index(index)
    _remember_file_category(file_id, record, category)
    return _normalize_record(file_id, record)


def register_output_file(path: Path, display_name: Optional[str] = None) -> Dict[str, Any]:
    file_id = str(uuid.uuid4())
    display = display_name or path.name
    safe = _safe_name(display)
    target = OUTPUT_DIR / f"{file_id}_{safe}"
    shutil.copyfile(path, target)
    index = _load_index()
    record = {
        "file_id": file_id,
        "kind": "output",
        "original_name": display,
        "stored_name": target.name,
        "relative_path": f"outputs/{target.name}",
        "path": str(target),
        "category": "Outputs",
    }
    index[file_id] = record
    _save_index(index)
    _remember_file_category(file_id, record, record.get("category"))
    return _normalize_record(file_id, record)


def list_files(kind: Optional[str] = None, include_missing: bool = False, remove_missing: bool = True) -> List[Dict[str, Any]]:
    # By default, the UI should not show stale records.  This also fixes the
    # case where a user manually deletes files from app/workspace/uploads.
    index, _stats = repair_index(remove_missing=remove_missing, kind=kind)
    values = list(index.values())
    if kind is not None:
        values = [x for x in values if x.get("kind") == kind]
    if not include_missing:
        values = [x for x in values if x.get("exists", False)]
    # Keep API output compact; path is internal and can leak machine-specific directories.
    cleaned = []
    for x in values:
        item = {k: v for k, v in x.items() if k != "path"}
        item["exists"] = bool(x.get("exists", False))
        cleaned.append(item)
    return sorted(cleaned, key=lambda x: (x.get("kind", ""), x.get("category", "Uncategorized"), x.get("original_name", "")))



def update_file_category(file_id: str, category: str) -> Dict[str, Any]:
    """Persistently update the category/folder metadata for one file."""
    category = _safe_category(category)
    index, _stats = repair_index(remove_missing=False)
    if file_id not in index:
        raise FileNotFoundError(f"Unknown file_id: {file_id}")
    record = dict(index[file_id])
    record["category"] = category
    index[file_id] = {k: v for k, v in record.items() if k != "exists"}
    _save_index(index)
    _remember_file_category(file_id, record, category)
    return _normalize_record(file_id, record)


def get_file_record(file_id: str) -> Dict[str, Any]:
    index, _stats = repair_index(remove_missing=False)
    if file_id not in index:
        raise FileNotFoundError(f"Unknown file_id: {file_id}. The file may not have been imported on this computer. Re-upload it or import a portable project ZIP.")
    record = index[file_id]
    if not record.get("exists"):
        names = [str(p) for p in _candidate_paths(record)]
        raise FileNotFoundError(
            "File record exists but the actual file is missing. "
            f"File: {record.get('original_name', file_id)}. Tried: {names}. "
            "Re-upload/reselect the file, restore app/workspace/uploads, or import a portable project ZIP."
        )
    return record


def get_file_path(file_id: str) -> Path:
    record = get_file_record(file_id)
    path = Path(record["path"])
    if not path.exists():
        # Should not happen after get_file_record, but keep a clear message.
        raise FileNotFoundError(f"File record exists but path is missing: {path}")
    return path


def make_runtime_path(prefix: str, suffix: str) -> Path:
    safe_prefix = _safe_name(prefix)
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return TMP_DIR / f"{safe_prefix}_{uuid.uuid4().hex}{safe_suffix}"


def cleanup_missing_files(kind: Optional[str] = "upload") -> Dict[str, int]:
    _index, stats = repair_index(remove_missing=True, kind=kind)
    return stats



def _remove_dir_contents(folder: Path) -> int:
    """Remove all children of a workspace subfolder without deleting the folder itself."""
    removed = 0
    folder.mkdir(parents=True, exist_ok=True)
    for child in list(folder.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    folder.mkdir(parents=True, exist_ok=True)
    return removed


def cleanup_generated_files(
    *,
    include_outputs: bool = True,
    include_tmp: bool = True,
    include_popup_tmp: bool = True,
    include_gempy_popups: bool = True,
) -> Dict[str, int]:
    """Clean generated files while preserving user uploads.

    Cleans:
      - app/workspace/outputs
      - app/workspace/tmp
      - app/workspace/macos_popups

    Does NOT clean:
      - app/workspace/uploads
      - upload records in files_index.json

    Output records are removed from files_index.json so the index does not
    accumulate stale generated-file entries.
    """
    stats = {
        "removed_output_files": 0,
        "removed_tmp_files": 0,
        "removed_popup_tmp_files": 0,
        "removed_gempy_popup_files": 0,
        "removed_output_index_records": 0,
    }

    if include_outputs:
        stats["removed_output_files"] = _remove_dir_contents(OUTPUT_DIR)

        index = _load_index()
        output_ids = [fid for fid, rec in index.items() if rec.get("kind") == "output"]
        for fid in output_ids:
            index.pop(fid, None)
        stats["removed_output_index_records"] = len(output_ids)
        _save_index(index)

    if include_tmp:
        stats["removed_tmp_files"] = _remove_dir_contents(TMP_DIR)

    if include_popup_tmp:
        popup_dir = WORKSPACE / "macos_popups"
        stats["removed_popup_tmp_files"] = _remove_dir_contents(popup_dir)

    if include_gempy_popups:
        gempy_popup_dir = WORKSPACE / "gempy_popups"
        stats["removed_gempy_popup_files"] = _remove_dir_contents(gempy_popup_dir)

    # Keep required workspace folders present after cleanup.
    for p in (WORKSPACE, UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
        p.mkdir(parents=True, exist_ok=True)

    return stats

def import_uploaded_file_with_record(src_path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Import a file from a portable project ZIP, preserving file_id."""
    file_id = str(record.get("file_id") or uuid.uuid4())
    original_name = str(record.get("original_name") or Path(src_path).name)
    stored_name = str(record.get("stored_name") or f"{file_id}_{_safe_name(original_name)}")
    target = UPLOAD_DIR / Path(stored_name).name
    shutil.copyfile(src_path, target)
    index = _load_index()
    category = _safe_category(record.get("category") or record.get("folder") or _lookup_persisted_category(file_id, record) or "Uncategorized")
    new_record = {
        "file_id": file_id,
        "kind": "upload",
        "original_name": original_name,
        "stored_name": target.name,
        "relative_path": f"uploads/{target.name}",
        "path": str(target),
        "category": category,
    }
    index[file_id] = new_record
    _save_index(index)
    _remember_file_category(file_id, new_record, category)
    return _normalize_record(file_id, new_record)
