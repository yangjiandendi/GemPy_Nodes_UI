from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import main as app_main
from app import storage


def configure_workspace(root: Path) -> None:
    storage.WORKSPACE = root
    storage.UPLOAD_DIR = root / "uploads"
    storage.OUTPUT_DIR = root / "outputs"
    storage.TMP_DIR = root / "tmp"
    storage.INDEX_PATH = root / "files_index.json"
    storage.CATEGORY_INDEX_PATH = root / "file_categories.json"
    for folder in (storage.WORKSPACE, storage.UPLOAD_DIR, storage.OUTPUT_DIR, storage.TMP_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix=".gempy_upload_delete_test_", dir=PROJECT_ROOT) as tmp:
        root = Path(tmp)
        configure_workspace(root / "workspace")

        source = root / "source.csv"
        source.write_text("X,Y,Z\n1,2,3\n", encoding="utf-8")
        record = storage.register_uploaded_file(source, "source.csv", category="Test folder")
        stored_path = storage.get_file_path(record["file_id"])
        assert stored_path.exists()

        deleted = storage.delete_uploaded_file(record["file_id"])
        assert deleted["deleted_from_disk"] is True
        assert not stored_path.exists()
        assert storage.list_files(kind="upload") == []
        assert record["file_id"] not in json.loads(storage.INDEX_PATH.read_text(encoding="utf-8"))

        api_source = root / "api_source.csv"
        api_source.write_text("X,Y,Z\n4,5,6\n", encoding="utf-8")
        api_record = storage.register_uploaded_file(api_source, "api_source.csv")
        api_result = app_main.delete_upload(api_record["file_id"])
        assert api_result["ok"] is True
        assert api_result["deleted"]["file_id"] == api_record["file_id"]
        assert api_result["files"] == []

        external = root / "must_not_delete.csv"
        external.write_text("safe\n", encoding="utf-8")
        unsafe_id = "unsafe-legacy-record"
        storage.INDEX_PATH.write_text(
            json.dumps(
                {
                    unsafe_id: {
                        "file_id": unsafe_id,
                        "kind": "upload",
                        "original_name": external.name,
                        "stored_name": external.name,
                        "path": str(external),
                        "category": "Legacy",
                    }
                }
            ),
            encoding="utf-8",
        )
        try:
            storage.delete_uploaded_file(unsafe_id)
        except PermissionError:
            pass
        else:
            raise AssertionError("Expected deletion outside workspace/uploads to be refused")
        assert external.exists()

    print("upload deletion tests: PASS")


if __name__ == "__main__":
    main()
