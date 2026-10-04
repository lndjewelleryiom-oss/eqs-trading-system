from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FIXTURES = TESTS / "fixtures" / "r13_genuine_manifest"
sys.path.insert(0, str(TESTS))

from r13_manifest_support import build_valid_manifest, h, refresh_manifest_hash  # noqa: E402


def write_json(path: Path, payload: object) -> str:
    raw = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def mutate(rule_id: str, manifest: dict[str, object]) -> None:
    dataset = manifest["dataset"]
    feature = manifest["feature_binding"]
    universe = manifest["universe"]
    lifecycle = manifest["lifecycle"]
    coverage = manifest["coverage"]
    eligibility = manifest["eligibility"]
    validator = manifest["validator"]
    provenance = manifest["provenance"]

    if rule_id == "R13-M001":
        dataset["coverage_start"] = "2026-10-01T00:00:00Z"
    elif rule_id == "R13-M002":
        dataset["decision_time"] = "2026-09-01T00:00:00Z"
    elif rule_id == "R13-M003":
        validator["run_completed_at"] = "2026-10-01T08:00:00Z"
    elif rule_id == "R13-M004":
        feature["dataset_manifest_fingerprint"] = h("wrong-dataset")
    elif rule_id == "R13-M005":
        feature["universe_fingerprint"] = h("wrong-universe")
    elif rule_id == "R13-M006":
        provenance["universe_snapshot_sha256"] = h("wrong-snapshot")
    elif rule_id == "R13-M007":
        provenance["lifecycle_event_set_sha256"] = h("wrong-lifecycle")
    elif rule_id == "R13-M008":
        provenance["coverage_record_set_sha256"] = h("wrong-coverage")
    elif rule_id == "R13-M009":
        provenance["eligibility_record_set_sha256"] = h("wrong-eligibility")
    elif rule_id == "R13-M010":
        provenance["validator_version"] = "v1.0.1"
    elif rule_id == "R13-M011":
        provenance["validator_build_sha256"] = h("wrong-validator")
    elif rule_id == "R13-M012":
        provenance["rule_catalog_version"] = "RR-001-030-v2"
    elif rule_id == "R13-M013":
        provenance["rule_catalog_sha256"] = h("wrong-catalog")
    elif rule_id == "R13-M014":
        provenance["pit_certification_sha256"] = h("wrong-pit")
    elif rule_id == "R13-M015":
        eligibility["record_count"] = 2
    elif rule_id == "R13-M016":
        coverage["records"][0]["observed_intervals"] = 11
    elif rule_id == "R13-M017":
        coverage["records"][0]["partial_intervals"] = 1
    elif rule_id == "R13-M018":
        eligibility["records"][0]["effective_to"] = "2022-12-31T00:00:00Z"
    elif rule_id == "R13-M019":
        eligibility["records"][0]["source_universe_fingerprint"] = h("wrong-universe")
    elif rule_id == "R13-M020":
        provenance["source_receipt_index_sha256"] = h("not-in-inventory")
    elif rule_id == "R13-M021":
        extra = FIXTURES / "extra.bin"
        extra.write_bytes(b"actual")
        manifest["artifact_inventory"].append({
            "artifact_id": "bad-extra",
            "artifact_type": "OTHER",
            "path_or_uri": "extra.bin",
            "sha256": sha256(b"different").hexdigest(),
            "byte_size": len(b"actual"),
            "classification": "DERIVED_REAL_MARKET",
        })
    elif rule_id == "R13-M022":
        manifest["manifest_hash"] = "0" * 64
        return
    elif rule_id == "R13-M023":
        manifest["manifest_generation"] = 1
        manifest["manifest_id"] = "r13m_m023case"
        manifest["dataset"]["dataset_version"] = "R1.3-m023"
    elif rule_id == "R13-M024":
        manifest["manifest_generation"] = 2
        manifest["manifest_id"] = "r13m_fixture0001"
        manifest["dataset"]["dataset_version"] = "R1.3-m024"
    else:
        raise ValueError(rule_id)
    refresh_manifest_hash(manifest)
def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    valid = build_valid_manifest(
        FIXTURES,
        generation=1,
        manifest_id="r13m_fixture0001",
    )
    valid["dataset"]["dataset_version"] = "R1.3-fixture-g1"
    refresh_manifest_hash(valid)
    valid_file = "valid_genuine_manifest.json"
    valid_sha = write_json(FIXTURES / valid_file, valid)

    entries = []
    for index in range(1, 25):
        rule_id = f"R13-M{index:03d}"
        fixture = deepcopy(valid)
        fixture["manifest_id"] = f"r13m_m{index:03d}case"
        fixture["dataset"]["dataset_version"] = f"R1.3-{rule_id.lower()}"
        refresh_manifest_hash(fixture)
        mutate(rule_id, fixture)
        filename = f"invalid_{rule_id.lower().replace('-', '_')}.json"
        digest = write_json(FIXTURES / filename, fixture)
        entries.append({
            "rule_id": rule_id,
            "expected_error_code": f"R13_M{index:03d}_FAILED",
            "file": filename,
            "sha256": digest,
            "requires_prior": index in (23, 24),
            "prior_file": valid_file if index in (23, 24) else None,
        })

    catalogue = {
        "schema_version": "EQS-R1.3-GENUINE-MANIFEST-FIXTURE-CATALOG-v1",
        "valid": {"file": valid_file, "sha256": valid_sha},
        "invalid": entries,
        "artifact_root": ".",
        "semantic_rules": [f"R13-M{i:03d}" for i in range(1, 25)],
    }
    catalogue_path = FIXTURES / "fixture_catalog.json"
    write_json(catalogue_path, catalogue)
    fixture_hash = sha256()
    for path in sorted(FIXTURES.iterdir(), key=lambda item: item.name):
        if path.name == "fixture_set.sha256" or not path.is_file():
            continue
        fixture_hash.update(path.name.encode("utf-8"))
        fixture_hash.update(b"\0")
        fixture_hash.update(path.read_bytes())
        fixture_hash.update(b"\0")
    (FIXTURES / "fixture_set.sha256").write_text(fixture_hash.hexdigest() + "\n", encoding="ascii")
    print(FIXTURES)
    print("invalid_fixtures=24")
    print("fixture_set_sha256=" + fixture_hash.hexdigest())


if __name__ == "__main__":
    main()
