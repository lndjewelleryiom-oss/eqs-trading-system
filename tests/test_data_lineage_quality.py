from datetime import datetime, timedelta, timezone
import json

from quant_system.data.calendar_source import load_calendar_record
from quant_system.data.catalog import DatasetCatalog
from quant_system.data.ingestion import ingest_payload
from quant_system.data.models import MarketObservation
from quant_system.data.quality import QualityDisposition, RevisionQualityPipeline
from quant_system.data.raw_store import ImmutableRawStore


class Connector:
    source_name = "fixture-source"

    def __init__(self, payload: bytes):
        self.payload = payload

    def fetch(self) -> bytes:
        return self.payload


def make_obs(*, revision=0, value=1.0, confidence=1.0):
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    return MarketObservation("d", "ABC", t, t, t, t, value, revision=revision, confidence=confidence)


def test_ingestion_links_immutable_raw_blob_to_persisted_manifest(tmp_path):
    raw = ImmutableRawStore(tmp_path / "raw")
    catalog = DatasetCatalog(tmp_path / "catalog")
    receipt = ingest_payload(
        Connector(b"abc"), raw_store=raw, catalog=catalog, dataset_id="bars", schema_version="1", latency_class="batch"
    )
    manifest = catalog.get(receipt.manifest_fingerprint)
    assert manifest.source == "fixture-source"
    assert manifest.content_hashes == (receipt.raw_blob.content_hash,)
    assert raw.get(receipt.raw_blob.content_hash) == b"abc"


def test_catalog_detects_tampering(tmp_path):
    catalog = DatasetCatalog(tmp_path)
    raw = ImmutableRawStore(tmp_path / "raw")
    receipt = ingest_payload(Connector(b"abc"), raw_store=raw, catalog=catalog, dataset_id="d", schema_version="1")
    path = tmp_path / f"{receipt.manifest_fingerprint}.json"
    payload = json.loads(path.read_text())
    payload["source"] = "tampered"
    path.write_text(json.dumps(payload))
    try:
        catalog.get(receipt.manifest_fingerprint)
    except RuntimeError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("tampered manifest should fail")


def test_versioned_calendar_source_loads_holidays_and_timezone():
    record = load_calendar_record(json.dumps({
        "calendar_id": "TEST_XNYS",
        "timezone_name": "America/New_York",
        "open_time": "09:30:00",
        "close_time": "16:00:00",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": ["2026-01-01"],
        "source": "synthetic-fixture",
        "version": "2026-test",
    }).encode())
    calendar = record.build()
    holiday_midday = datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
    regular_midday = datetime(2026, 1, 2, 17, tzinfo=timezone.utc)
    assert not calendar.is_open(holiday_midday)
    assert calendar.is_open(regular_midday)
    assert len(record.fingerprint()) == 64


def test_revision_pipeline_quarantines_low_confidence_and_out_of_order_revision():
    pipeline = RevisionQualityPipeline()
    low = pipeline.process(make_obs(confidence=0.4))
    assert low.disposition == QualityDisposition.QUARANTINE
    assert len(pipeline.quarantine) == 1

    assert pipeline.process(make_obs(revision=2, value=2)).disposition == QualityDisposition.ACCEPT
    old = pipeline.process(make_obs(revision=1, value=1.5))
    assert old.disposition == QualityDisposition.QUARANTINE
    assert "OUT_OF_ORDER_REVISION" in old.reason_codes


def test_revision_pipeline_rejects_conflicting_same_revision():
    pipeline = RevisionQualityPipeline()
    assert pipeline.process(make_obs(revision=0, value=1)).disposition == QualityDisposition.ACCEPT
    conflict = pipeline.process(make_obs(revision=0, value=2))
    assert conflict.disposition == QualityDisposition.REJECT
    assert "CONFLICTING_SAME_REVISION" in conflict.reason_codes
    assert len(pipeline.rejected) == 1
