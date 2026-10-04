from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _pointer_path(base_path: Path) -> Path:
    return base_path.with_name(base_path.stem + ".pointer.json")


def _generation_dir(base_path: Path) -> Path:
    return base_path.parent / (base_path.stem + ".generations")


def publish_json_generation(
    base_path: str | Path,
    document: dict[str, Any],
    *,
    seal_field: str = "record_sha256",
) -> Path:
    """Publish an immutable JSON generation and atomically swing a small pointer.

    The existing legacy base file is never truncated or overwritten by this
    function. Readers that understand the pointer see the new generation;
    legacy readers keep the previous complete base file.
    """
    base = Path(base_path)
    if not isinstance(document.get(seal_field), str):
        raise ValueError(f"document is missing {seal_field}")
    body = dict(document)
    expected = body.pop(seal_field)
    if sha256(_canonical(body)).hexdigest() != expected:
        raise ValueError("document seal is invalid")

    generation_dir = _generation_dir(base)
    generation_dir.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    file_hash = sha256(payload).hexdigest()
    generation_name = f"{expected}_{file_hash[:16]}.json"
    generation_path = generation_dir / generation_name

    if generation_path.exists():
        if sha256(generation_path.read_bytes()).hexdigest() != file_hash:
            raise ValueError("existing immutable generation hash mismatch")
    else:
        with generation_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    pointer = {
        "schema_id": "EQS-IMMUTABLE-JSON-POINTER-V1",
        "generation_file": generation_name,
        "generation_sha256": file_hash,
        "document_seal_field": seal_field,
        "document_seal": expected,
    }
    pointer["record_sha256"] = sha256(_canonical(pointer)).hexdigest()
    pointer_path = _pointer_path(base)
    temp = pointer_path.with_suffix(pointer_path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(pointer, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, pointer_path)
    return generation_path


def read_published_json(
    base_path: str | Path,
    *,
    seal_field: str = "record_sha256",
    maximum_bytes: int = 8_000_000,
) -> dict[str, Any]:
    """Read the pointed immutable generation, falling back to the legacy file."""
    base = Path(base_path)
    pointer_path = _pointer_path(base)
    target = base
    expected_file_hash: str | None = None

    if pointer_path.is_file():
        if pointer_path.stat().st_size > 128_000:
            raise ValueError("publication pointer exceeds bounded size")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer_body = dict(pointer)
        pointer_seal = pointer_body.pop("record_sha256", None)
        if not isinstance(pointer_seal, str) or sha256(_canonical(pointer_body)).hexdigest() != pointer_seal:
            raise ValueError("publication pointer seal invalid")
        generation_name = pointer.get("generation_file")
        if not isinstance(generation_name, str) or Path(generation_name).name != generation_name:
            raise ValueError("invalid generation file name")
        generation_dir = _generation_dir(base).resolve()
        target = (generation_dir / generation_name).resolve()
        if not target.is_relative_to(generation_dir):
            raise ValueError("generation path escapes publication directory")
        expected_file_hash = pointer.get("generation_sha256")
        if not isinstance(expected_file_hash, str):
            raise ValueError("publication pointer missing generation hash")

    if not target.is_file():
        raise FileNotFoundError(target)
    if target.stat().st_size > maximum_bytes:
        raise ValueError("published JSON exceeds bounded size")
    raw = target.read_bytes()
    if expected_file_hash is not None and sha256(raw).hexdigest() != expected_file_hash:
        raise ValueError("published generation file hash mismatch")
    document = json.loads(raw)
    body = dict(document)
    expected = body.pop(seal_field, None)
    if not isinstance(expected, str) or sha256(_canonical(body)).hexdigest() != expected:
        raise ValueError("published document seal invalid")
    return document
