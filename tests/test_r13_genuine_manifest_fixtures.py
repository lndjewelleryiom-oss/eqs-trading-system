from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.research.r13_admission import R13AdmissionError, R13AdmissionLedger
from quant_system.research.r13_manifest import SEMANTIC_RULE_IDS, validate_manifest_schema

FIXTURE_ROOT = Path("tests/fixtures/r13_genuine_manifest")
CATALOG_PATH = FIXTURE_ROOT / "fixture_catalog.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


CATALOG = _load(CATALOG_PATH)
INVALID_CASES = tuple(
    pytest.param(
        item["rule_id"],
        item["file"],
        item["expected_error_code"],
        bool(item["requires_prior"]),
        id=item["rule_id"],
    )
    for item in CATALOG["invalid"]
)


def _fixture_set_hash() -> str:
    digest = sha256()
    for path in sorted(FIXTURE_ROOT.iterdir(), key=lambda item: item.name):
        if path.name == "fixture_set.sha256" or not path.is_file():
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_fixture_catalog_covers_exactly_m001_through_m024():
    assert tuple(item["rule_id"] for item in CATALOG["invalid"]) == SEMANTIC_RULE_IDS
    assert len(CATALOG["invalid"]) == 24
    assert len({item["file"] for item in CATALOG["invalid"]}) == 24
    assert len({item["expected_error_code"] for item in CATALOG["invalid"]}) == 24


def test_fixture_bytes_match_catalogue_hashes_and_set_seal():
    valid = CATALOG["valid"]
    assert sha256((FIXTURE_ROOT / valid["file"]).read_bytes()).hexdigest() == valid["sha256"]
    for item in CATALOG["invalid"]:
        actual = sha256((FIXTURE_ROOT / item["file"]).read_bytes()).hexdigest()
        assert actual == item["sha256"]
    expected_set_hash = (FIXTURE_ROOT / "fixture_set.sha256").read_text(encoding="ascii").strip()
    assert _fixture_set_hash() == expected_set_hash


def test_valid_genuine_fixture_is_schema_valid_and_admissible(tmp_path):
    manifest = _load(FIXTURE_ROOT / CATALOG["valid"]["file"])
    validate_manifest_schema(manifest)
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    record = ledger.admit(manifest, artifact_root=FIXTURE_ROOT)
    assert record.manifest.manifest_sha256 == manifest["manifest_hash"]
    assert ledger.verify_hash_chain()
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == 1
    ledger.close()


@pytest.mark.parametrize(
    "rule_id,filename,expected_code,requires_prior",
    INVALID_CASES,
)
def test_each_invalid_genuine_fixture_returns_exact_failure_code(
    rule_id,
    filename,
    expected_code,
    requires_prior,
    tmp_path,
):
    ledger = R13AdmissionLedger(tmp_path / f"{rule_id}.sqlite")
    initial_count = 0
    if requires_prior:
        prior = _load(FIXTURE_ROOT / CATALOG["valid"]["file"])
        ledger.admit(prior, artifact_root=FIXTURE_ROOT)
        initial_count = 1

    manifest = _load(FIXTURE_ROOT / filename)
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(manifest, artifact_root=FIXTURE_ROOT)

    assert exc.value.code == expected_code
    assert expected_code == f"{rule_id.replace('-', '_')}_FAILED"
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == initial_count
    assert ledger.verify_hash_chain()
    ledger.close()


def test_m012_fixture_remains_semantically_targeted_while_schema_version_is_frozen():
    item = next(entry for entry in CATALOG["invalid"] if entry["rule_id"] == "R13-M012")
    manifest = _load(FIXTURE_ROOT / item["file"])
    assert manifest["rule_catalogue"]["catalogue_version"] == "RR-001-030-v1"
    assert manifest["provenance"]["rule_catalog_version"] == "RR-001-030-v2"


def test_history_dependent_fixture_contracts_are_explicit():
    history = {
        item["rule_id"]: item["requires_prior"]
        for item in CATALOG["invalid"]
    }
    assert history["R13-M023"] is True
    assert history["R13-M024"] is True
    assert all(not required for rule, required in history.items() if rule not in {"R13-M023", "R13-M024"})
