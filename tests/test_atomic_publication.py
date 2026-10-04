from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.performance.publication import publish_json_generation, read_published_json


def seal(document: dict) -> dict:
    body=dict(document)
    body["record_sha256"]=sha256(
        json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    ).hexdigest()
    return body


def test_publish_uses_immutable_generation_and_pointer_without_truncating_legacy(tmp_path: Path) -> None:
    legacy=tmp_path/"state.json"
    legacy.write_text('{"legacy":"complete"}\n',encoding="utf-8")
    before=legacy.read_bytes()
    doc=seal({"schema_id":"TEST","value":1})
    generation=publish_json_generation(legacy,doc)
    assert generation.is_file()
    assert legacy.read_bytes()==before
    assert read_published_json(legacy)==doc
    pointer=tmp_path/"state.pointer.json"
    assert pointer.is_file()


def test_new_generation_replaces_pointer_not_old_generation(tmp_path: Path) -> None:
    base=tmp_path/"state.json"
    first=seal({"schema_id":"TEST","value":1})
    second=seal({"schema_id":"TEST","value":2})
    p1=publish_json_generation(base,first)
    p2=publish_json_generation(base,second)
    assert p1.is_file() and p2.is_file() and p1!=p2
    assert read_published_json(base)==second
    assert json.loads(p1.read_text())==first


def test_reader_rejects_corrupt_pointer_and_generation(tmp_path: Path) -> None:
    base=tmp_path/"state.json"
    doc=seal({"schema_id":"TEST","value":1})
    generation=publish_json_generation(base,doc)
    pointer=tmp_path/"state.pointer.json"
    p=json.loads(pointer.read_text())
    p["generation_sha256"]="0"*64
    pointer.write_text(json.dumps(p),encoding="utf-8")
    with pytest.raises(ValueError):
        read_published_json(base)

    publish_json_generation(base,doc)
    generation.write_text('{"broken":true}',encoding="utf-8")
    with pytest.raises(ValueError):
        read_published_json(base)


def test_invalid_document_seal_never_publishes(tmp_path: Path) -> None:
    base=tmp_path/"state.json"
    with pytest.raises(ValueError,match="seal"):
        publish_json_generation(base,{"record_sha256":"bad","value":1})
    assert not (tmp_path/"state.pointer.json").exists()
