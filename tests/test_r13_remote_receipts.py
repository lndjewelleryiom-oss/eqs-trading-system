from hashlib import sha256
import json

from quant_system.research.r13_evidence_pipeline import (
    ArtifactRef,
    R13HistoricalEvidencePipeline,
)
from quant_system.research.raw_store import RawObjectDigest


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def test_r13_source_receipts_rehash_s3_objects(monkeypatch, tmp_path):
    digest = "a" * 64
    source = {
        "classification": "REAL_MARKET",
        "remote_raw_root": "s3://eqs-raw/r13",
        "receipts": [
            {
                "venue": "BYBIT_LINEAR",
                "label": "trades",
                "filename": "BYBIT_LINEAR/trades/2023-01-01/BTCUSDT.csv.gz",
                "sha256": digest,
                "available_at": "2023-01-01T23:59:59Z",
                "received_at": "2026-10-04T12:00:00Z",
                "status": "PASS",
            }
        ],
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = sha256(path.read_bytes()).hexdigest()

    seen = {}

    def fake_verify(root, filename, expected):
        seen["root"] = root
        seen["filename"] = filename
        seen["expected"] = expected
        return RawObjectDigest(
            uri="s3://eqs-raw/r13/" + filename,
            sha256=expected,
            byte_count=123,
        )

    monkeypatch.setattr(
        "quant_system.research.r13_evidence_pipeline.verify_raw_object",
        fake_verify,
    )

    pipeline = R13HistoricalEvidencePipeline()
    _, receipts, blockers = pipeline._source_receipts(
        ArtifactRef(path=path, sha256=source_sha)
    )

    assert blockers == ()
    assert receipts[0]["verified"] is True
    assert receipts[0]["actual_sha256"] == digest
    assert seen == {
        "root": "s3://eqs-raw/r13",
        "filename": "BYBIT_LINEAR/trades/2023-01-01/BTCUSDT.csv.gz",
        "expected": digest,
    }


def test_r13_remote_receipt_failure_remains_blocking(monkeypatch, tmp_path):
    source = {
        "classification": "REAL_MARKET",
        "remote_raw_root": "s3://eqs-raw/r13",
        "receipts": [
            {
                "venue": "OKX_SWAP",
                "label": "liquidations",
                "filename": "OKX_SWAP/liquidations/2023-01-01/PERPETUALS.csv.gz",
                "sha256": "b" * 64,
                "available_at": "2023-01-01T23:59:59Z",
                "received_at": "2026-10-04T12:00:00Z",
                "status": "PASS",
            }
        ],
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    def fail(*args, **kwargs):
        from quant_system.research.raw_store import RawStoreError
        raise RawStoreError("unable to read s3 raw object")

    monkeypatch.setattr(
        "quant_system.research.r13_evidence_pipeline.verify_raw_object",
        fail,
    )
    pipeline = R13HistoricalEvidencePipeline()
    _, receipts, blockers = pipeline._source_receipts(
        ArtifactRef(path=path, sha256=sha256(path.read_bytes()).hexdigest())
    )
    assert any(code.startswith("RAW_OBJECT_MISSING:") for code in blockers)
    assert receipts[0]["verified"] is False
