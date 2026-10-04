from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    return (json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _validate_sealed_document(doc: dict[str, Any], *, field: str = "record_sha256") -> None:
    expected = doc.get(field)
    if not isinstance(expected, str):
        raise ValueError(f"missing {field}")
    body = dict(doc)
    body.pop(field, None)
    if sha256(_canonical(body)).hexdigest() != expected:
        raise ValueError(f"invalid {field}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    last: PermissionError | None = None
    for attempt in range(30):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    if last is not None:
        raise last
    raise RuntimeError("atomic publication failed")


def pointer_path(snapshot_path: str | Path) -> Path:
    path = Path(snapshot_path)
    return path.with_name(path.stem + ".pointer.json")


def generation_dir(snapshot_path: str | Path) -> Path:
    path = Path(snapshot_path)
    return path.parent / (path.stem + ".generations")


def publish_snapshot(snapshot_path: str | Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    path = Path(snapshot_path)
    _validate_sealed_document(snapshot)
    raw = _snapshot_bytes(snapshot)
    file_hash = sha256(raw).hexdigest()
    generation = int(snapshot.get("projection_generation", 0) or 0)
    stage = str(snapshot.get("projection_stage") or "UNKNOWN").replace("/", "_")
    name = f"g{generation:08d}_{stage}_{file_hash[:16]}.json"
    directory = generation_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != file_hash:
            raise RuntimeError("snapshot generation hash collision")
    else:
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    pointer: dict[str, Any] = {
        "schema_id": "EQS-CANONICAL-SNAPSHOT-POINTER-V1",
        "generation_file": name,
        "generation_sha256": file_hash,
        "projection_generation": generation,
        "projection_stage": snapshot.get("projection_stage"),
        "updated_at": snapshot.get("updated_at"),
    }
    pointer["record_sha256"] = sha256(_canonical(pointer)).hexdigest()
    _atomic_json(pointer_path(path), pointer)
    return pointer


def load_snapshot(snapshot_path: str | Path) -> dict[str, Any]:
    path = Path(snapshot_path)
    pointer = pointer_path(path)
    if pointer.is_file():
        pointer_doc = json.loads(pointer.read_text(encoding="utf-8"))
        _validate_sealed_document(pointer_doc)
        name = pointer_doc.get("generation_file")
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("invalid snapshot generation filename")
        target = generation_dir(path) / name
        raw = target.read_bytes()
        expected_file_hash = pointer_doc.get("generation_sha256")
        if not isinstance(expected_file_hash, str) or sha256(raw).hexdigest() != expected_file_hash:
            raise ValueError("snapshot generation file hash invalid")
        doc = json.loads(raw)
        _validate_sealed_document(doc)
        if int(doc.get("projection_generation", 0) or 0) != int(pointer_doc.get("projection_generation", -1)):
            raise ValueError("snapshot pointer generation mismatch")
        if doc.get("projection_stage") != pointer_doc.get("projection_stage"):
            raise ValueError("snapshot pointer stage mismatch")
        return doc
    if not path.is_file():
        raise FileNotFoundError(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    _validate_sealed_document(doc)
    return doc
