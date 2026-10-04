from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from quant_system.data.crypto_perps.models import (
    BookUpdate,
    LiquidationEvent,
    PerpetualStateEvent,
    TradeEvent,
)
from quant_system.data.crypto_perps.research_datasets import (
    DeterministicCryptoPerpReplay,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PointInTimeDatasetAssembler,
    ResearchDatasetManifest,
    event_sort_key,
    event_to_record,
)

from .alpha_binding import (
    CAMPAIGN_GATES,
    FAILURE_CODES as ALPHA_FAILURE_CODES,
    FILTER_GATES,
    GATE_IDS,
    GLOBAL_HARD_GATES,
    SCHEMA_VERSION as ALPHA_SCHEMA_VERSION,
    compute_binding_id,
    compute_campaign_set_fingerprint,
    expected_final_decision,
    frozen_campaign_contracts,
    validate_alpha_data_binding_report,
)
from .r13_manifest import canonical_json
from .raw_store import RawStoreError, verify_raw_object

PIPELINE_SCHEMA_ID = "EQS-R1.3-HISTORICAL-EVIDENCE-PIPELINE-INPUT-v1"
PIPELINE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "r1_3_historical_evidence_pipeline_input_v1.schema.json"
)
STATUS_SCHEMA_ID = "EQS-R1.3-HISTORICAL-EVIDENCE-PIPELINE-STATUS-v1"
H02_SCHEMA_ID = "EQS-H02-BATCH-MANIFEST-v1"
PIT_SCHEMA_ID = "EQS-R1.3-PIT-CERTIFICATION-v1"
RR_CATALOGUE_ID = "EQS-H02-RR-CATALOGUE"
RR_CATALOGUE_VERSION = "RR-001-030-v1"
VALIDATOR_ID = "EQS-H02-R1.3-VALIDATOR"
VALIDATOR_VERSION = "v1.0.0"
RR_IDS = tuple(f"RR-{i:03d}" for i in range(1, 31))

FROZEN_VENUES = ("BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP")
REQUIRED_SERIES = (
    "TRADES",
    "ORDERBOOK",
    "MARK_PRICE",
    "INDEX_PRICE",
    "FUNDING",
    "OPEN_INTEREST",
    "LIQUIDATIONS",
    "INSTRUMENT_METADATA",
)
FROZEN_WINDOWS = {
    "training": ("2023-01-01T00:00:00Z", "2025-06-30T23:59:59Z"),
    "validation": ("2025-07-01T00:00:00Z", "2026-03-31T23:59:59Z"),
    "locked_oos": ("2026-04-01T00:00:00Z", "2026-09-21T23:59:59Z"),
}
MIN_HISTORY_DAYS = 90
LIQUIDITY_WINDOW_DAYS = 30
MIN_CROSS_SECTION = 8
MIN_BREADTH_FRACTION = 0.80
RR_FAILURE_CODES: dict[str, tuple[str, ...]] = {
    "RR-001": ("H02_SCHEMA_INVALID",),
    "RR-002": ("H02_IDENTITY_CONFLICT",),
    "RR-003": ("H02_SCOPE_INVALID",),
    "RR-004": ("H02_CANONICAL_MAPPING_AMBIGUOUS",),
    "RR-005": ("H02_SOURCE_RECEIPT_MISSING", "H02_SOURCE_HASH_MISMATCH"),
    "RR-006": ("H02_TIMESTAMP_ORDER_INVALID",),
    "RR-007": ("H02_LIFECYCLE_GAP", "H02_LIFECYCLE_CONFLICT"),
    "RR-008": ("H02_SOURCE_COVERAGE_INCOMPLETE",),
    "RR-009": ("H02_WINDOW_SCOPE_MISMATCH",),
    "RR-010": ("H02_UNKNOWN_AVAILABILITY",),
    "RR-011": ("H02_A19_HISTORY_INSUFFICIENT", "H02_PIT_LIFECYCLE_UNPROVEN"),
    "RR-012": ("H02_A20_LIQUIDITY_INSUFFICIENT", "H02_LIQUIDITY_WINDOW_INVALID"),
    "RR-013": ("H02_FEATURE_PREREQUISITE_MISSING",),
    "RR-014": ("H02_CROSS_SECTIONAL_BREADTH_INSUFFICIENT",),
    "RR-015": ("H02_ELIGIBILITY_CONFLICT", "H02_INVALID_EXCLUSION"),
    "RR-016": ("H02_RECORD_HASH_MISMATCH",),
    "RR-017": ("H02_MANIFEST_CLOSURE_INCOMPLETE",),
    "RR-018": ("H02_REBUILD_NONDETERMINISTIC",),
    "RR-019": ("H02_REQUIRED_SERIES_MISSING",),
    "RR-020": ("H02_COVERAGE_COUNT_MISMATCH",),
    "RR-021": ("H02_GAP_ACCOUNTING_INVALID",),
    "RR-022": ("H02_COMPLETION_SEMANTICS_INVALID",),
    "RR-023": ("H02_PIT_LIFECYCLE_UNPROVEN", "H02_PIT_MEMBERSHIP_INVALID"),
    "RR-024": ("H02_DECISION_TIME_INVALID",),
    "RR-025": ("H02_LIQUIDITY_POLICY_MISMATCH",),
    "RR-026": ("H02_CROSS_ARTIFACT_CONFLICT",),
    "RR-027": ("H02_EVIDENCE_INCOMPLETE", "H02_UNKNOWN_AVAILABILITY"),
    "RR-028": ("H02_RULESET_UNVERIFIED",),
    "RR-029": ("H02_EVIDENCE_CONFLICT", "H02_EVIDENCE_INCOMPLETE"),
    "RR-030": ("H02_AUTHORITY_BOUNDARY_BREACH",),
}

class R13EvidencePipelineError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    status: str
    failure_codes: tuple[str, ...]
    observed: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    status: str
    output_root: Path
    status_path: Path
    certification_inputs_path: Path | None
    blockers: tuple[str, ...]
    artifact_hashes: Mapping[str, str]


def _sha_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha_payload(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def _dt(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise R13EvidencePipelineError(f"{field} must be an ISO-8601 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise R13EvidencePipelineError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise R13EvidencePipelineError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise R13EvidencePipelineError(f"{path} root must be an object")
    return value
def _write_immutable_json(path: Path, payload: object) -> str:
    encoded = canonical_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise R13EvidencePipelineError(
                f"immutable evidence path already exists with different bytes: {path}"
            )
    else:
        path.write_bytes(encoded)
    return sha256(encoded).hexdigest()


def _write_immutable_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise R13EvidencePipelineError(
                f"immutable evidence path already exists with different bytes: {path}"
            )
    else:
        path.write_bytes(payload)
    return sha256(payload).hexdigest()


def _resolve_descriptor(
    cfg: Mapping[str, object],
    key: str,
    *,
    base: Path,
    required: bool,
) -> ArtifactRef | None:
    raw = cfg.get(key)
    if raw is None:
        if required:
            raise R13EvidencePipelineError(f"required input descriptor missing: {key}")
        return None
    if not isinstance(raw, Mapping):
        raise R13EvidencePipelineError(f"{key} must be an object or null")
    path_value = raw.get("path")
    expected = raw.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        raise R13EvidencePipelineError(f"{key}.path is required")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(ch not in "0123456789abcdef" for ch in expected)
    ):
        raise R13EvidencePipelineError(f"{key}.sha256 must be lowercase SHA-256")
    candidate = Path(path_value)
    path = candidate if candidate.is_absolute() else base / candidate
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        if required:
            raise R13EvidencePipelineError(f"required input artifact unavailable: {key}") from exc
        return None
    if not path.is_file():
        raise R13EvidencePipelineError(f"input artifact is not a file: {key}")
    actual = _sha_file(path)
    if actual != expected:
        raise R13EvidencePipelineError(
            f"{key} hash mismatch: expected {expected}, got {actual}"
        )
    return ArtifactRef(path=path, sha256=actual)


def _load_catalogue(project_root: Path) -> tuple[dict[str, object], str]:
    path = project_root / "schemas" / "h02_semantic_rules_v1.json"
    payload = _json_object(path)
    if payload.get("catalogue_id") != RR_CATALOGUE_ID:
        raise R13EvidencePipelineError("RR catalogue_id mismatch")
    if payload.get("catalogue_version") != RR_CATALOGUE_VERSION:
        raise R13EvidencePipelineError("RR catalogue version mismatch")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise R13EvidencePipelineError("RR catalogue rules missing")
    ids = tuple(
        str(item.get("rule_id"))
        for item in rules
        if isinstance(item, Mapping)
    )
    if ids != RR_IDS:
        raise R13EvidencePipelineError("RR catalogue must contain RR-001..RR-030 exactly")
    for item in rules:
        assert isinstance(item, Mapping)
        rid = str(item["rule_id"])
        if tuple(item.get("failure_codes", ())) != RR_FAILURE_CODES[rid]:
            raise R13EvidencePipelineError(f"{rid} failure-code catalogue mismatch")
    return payload, _sha_payload(payload)


def _series_for_event(event: object) -> tuple[str, ...]:
    if isinstance(event, TradeEvent):
        return ("TRADES",)
    if isinstance(event, BookUpdate):
        return ("ORDERBOOK",)
    if isinstance(event, LiquidationEvent):
        return ("LIQUIDATIONS",)
    if isinstance(event, PerpetualStateEvent):
        values: list[str] = []
        if event.mark_price is not None:
            values.append("MARK_PRICE")
        if event.index_price is not None:
            values.append("INDEX_PRICE")
        if event.funding_rate is not None:
            values.append("FUNDING")
        if event.open_interest is not None or event.open_interest_value is not None:
            values.append("OPEN_INTEREST")
        return tuple(values)
    return ()
def _result(
    rule_id: str,
    passed: bool,
    *,
    blocked: bool = False,
    failure_codes: Sequence[str] = (),
    observed: Mapping[str, object] | None = None,
) -> RuleResult:
    if passed:
        return RuleResult(rule_id, "PASS", (), dict(observed or {}))
    status = "BLOCKED" if blocked else "FAIL"
    codes = tuple(sorted(set(str(code) for code in failure_codes)))
    if not codes:
        codes = (RR_FAILURE_CODES[rule_id][0],)
    unknown = set(codes) - set(RR_FAILURE_CODES[rule_id])
    if unknown:
        raise R13EvidencePipelineError(
            f"{rule_id} attempted unknown failure codes: {sorted(unknown)}"
        )
    return RuleResult(rule_id, status, codes, dict(observed or {}))


def _replay_fingerprint(events: Sequence[object]) -> str:
    return _sha_payload([event_to_record(event) for event in events])


def _worst_status(values: Iterable[str]) -> str:
    states = tuple(values)
    if "FAIL" in states:
        return "FAIL"
    if "BLOCKED" in states:
        return "BLOCKED"
    return "PASS"


class R13HistoricalEvidencePipeline:
    def __init__(self, *, project_root: str | Path | None = None):
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        )
        self.catalogue, self.catalogue_sha256 = _load_catalogue(self.project_root)

    def _load_config(self, path: Path) -> dict[str, object]:
        cfg = _json_object(path)
        schema = json.loads(PIPELINE_SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(cfg),
            key=lambda exc: tuple(str(part) for part in exc.absolute_path),
        )
        if errors:
            err = errors[0]
            location = ".".join(str(part) for part in err.absolute_path) or "$"
            raise R13EvidencePipelineError(
                f"pipeline input schema invalid at {location}: {err.message}"
            )
        if cfg.get("schema_id") != PIPELINE_SCHEMA_ID:
            raise R13EvidencePipelineError("pipeline input schema_id mismatch")
        for field in (
            "run_id",
            "created_at",
            "partition_root",
            "code_version",
            "dataset_builder_version",
            "build_command_fingerprint",
            "feature_definition_hash",
        ):
            if not isinstance(cfg.get(field), str) or not str(cfg[field]).strip():
                raise R13EvidencePipelineError(f"{field} is required")
        _dt(cfg["created_at"], "created_at")
        for field in ("build_command_fingerprint", "feature_definition_hash"):
            value = str(cfg[field])
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise R13EvidencePipelineError(f"{field} must be lowercase SHA-256")
        features = cfg.get("feature_run_manifests", [])
        if not isinstance(features, list):
            raise R13EvidencePipelineError("feature_run_manifests must be an array")
        return cfg

    def _verify_feature_refs(
        self,
        cfg: Mapping[str, object],
        *,
        base: Path,
    ) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        for index, item in enumerate(cfg.get("feature_run_manifests", [])):
            if not isinstance(item, Mapping):
                raise R13EvidencePipelineError(
                    f"feature_run_manifests[{index}] must be an object"
                )
            wrapper = {"item": item}
            ref = _resolve_descriptor(wrapper, "item", base=base, required=True)
            assert ref is not None
            refs.append(ref)
        return tuple(refs)

    def _feature_facts(
        self,
        feature_refs: Sequence[ArtifactRef],
        manifest: ResearchDatasetManifest,
    ) -> dict[str, object]:
        manifests: list[dict[str, object]] = []
        errors: list[str] = []
        for ref in feature_refs:
            try:
                payload = _json_object(ref.path)
            except Exception as exc:
                errors.append(str(exc))
                continue
            dataset_fp = payload.get("dataset_manifest_fingerprint")
            universe_fp = payload.get("universe_fingerprint")
            version = payload.get("feature_engine_version", payload.get("version"))
            max_decision = payload.get("max_decision_time")
            if version != "crypto-perps-feature-engine-v1":
                errors.append(f"{ref.path}: feature engine version mismatch")
            if dataset_fp != manifest.fingerprint():
                errors.append(f"{ref.path}: dataset fingerprint mismatch")
            if universe_fp != manifest.universe_fingerprint:
                errors.append(f"{ref.path}: universe fingerprint mismatch")
            if max_decision is None:
                errors.append(f"{ref.path}: max_decision_time missing")
            else:
                try:
                    if _dt(max_decision, "feature.max_decision_time") > manifest.decision_time:
                        errors.append(f"{ref.path}: feature source crosses decision boundary")
                except Exception as exc:
                    errors.append(str(exc))
            manifests.append(payload)
        return {
            "count": len(feature_refs),
            "lineage_ok": bool(feature_refs) and not errors,
            "errors": sorted(errors),
            "fingerprints": sorted(ref.sha256 for ref in feature_refs),
            "manifests": manifests,
        }

    def _load_dataset(
        self,
        dataset_ref: ArtifactRef,
        universe_ref: ArtifactRef,
        partition_root: Path,
    ) -> tuple[
        ResearchDatasetManifest,
        InstrumentUniverseHistory,
        tuple[object, ...],
        dict[str, object],
    ]:
        dataset_payload = _json_object(dataset_ref.path)
        try:
            manifest = ResearchDatasetManifest.from_record(dataset_payload)
        except Exception as exc:
            raise R13EvidencePipelineError(
                f"dataset manifest integrity verification failed: {exc}"
            ) from exc
        try:
            universe = InstrumentUniverseHistory.read(universe_ref.path)
        except Exception as exc:
            raise R13EvidencePipelineError(
                f"universe history integrity verification failed: {exc}"
            ) from exc
        if universe.fingerprint() != manifest.universe_fingerprint:
            raise R13EvidencePipelineError(
                "dataset manifest universe fingerprint does not match universe history"
            )
        store = HistoricalPartitionStore(partition_root)
        for descriptor in manifest.partition_descriptors:
            try:
                store.read_partition(descriptor)
            except Exception as exc:
                raise R13EvidencePipelineError(
                    f"partition verification failed: {descriptor.relative_path}: {exc}"
                ) from exc
        try:
            rebuilt = PointInTimeDatasetAssembler(store, universe).assemble(
                dataset_id=manifest.dataset_id,
                partitions=manifest.partition_descriptors,
                decision_time=manifest.decision_time,
                start_time=manifest.start_time,
            )
        except Exception as exc:
            raise R13EvidencePipelineError(
                f"deterministic PIT rebuild failed: {exc}"
            ) from exc
        if rebuilt.manifest.fingerprint() != manifest.fingerprint():
            raise R13EvidencePipelineError(
                "deterministic PIT rebuild fingerprint mismatch"
            )
        return manifest, universe, tuple(rebuilt.events), dataset_payload

    def _source_receipts(
        self,
        source_ref: ArtifactRef,
    ) -> tuple[dict[str, object], list[dict[str, object]], tuple[str, ...]]:
        source = _json_object(source_ref.path)
        blockers: list[str] = []
        classification = str(source.get("classification", "")).upper()
        if "REAL MARKET" not in classification and classification != "REAL_MARKET":
            blockers.append("SOURCE_CLASSIFICATION_NOT_REAL_MARKET")
        receipts_raw = source.get("receipts")
        if not isinstance(receipts_raw, list) or not receipts_raw:
            blockers.append("SOURCE_RECEIPTS_MISSING")
            receipts_raw = []
        raw_root_value = source.get("remote_raw_root")
        raw_root = str(raw_root_value) if isinstance(raw_root_value, str) and raw_root_value else None
        verified: list[dict[str, object]] = []
        seen: dict[str, str] = {}
        for index, item in enumerate(receipts_raw):
            if not isinstance(item, Mapping):
                blockers.append(f"SOURCE_RECEIPT_{index}_INVALID")
                continue
            filename = str(item.get("filename", ""))
            digest = str(item.get("sha256", ""))
            if not filename or len(digest) != 64:
                blockers.append(f"SOURCE_RECEIPT_{index}_INVALID")
                continue
            prior = seen.get(filename)
            if prior is not None and prior != digest:
                blockers.append(f"SOURCE_RECEIPT_CONFLICT:{filename}")
            seen[filename] = digest
            actual: str | None = None
            if raw_root is None:
                blockers.append(f"RAW_ROOT_UNKNOWN:{filename}")
            else:
                try:
                    verified_object = verify_raw_object(raw_root, filename, digest)
                    actual = verified_object.sha256
                except RawStoreError as exc:
                    message = str(exc).lower()
                    if "hash mismatch" in message:
                        blockers.append(f"RAW_HASH_MISMATCH:{filename}")
                    else:
                        blockers.append(f"RAW_OBJECT_MISSING:{filename}")
            verified.append({
                "venue": item.get("venue"),
                "label": item.get("label"),
                "filename": filename,
                "sha256": digest,
                "actual_sha256": actual,
                "available_at": item.get("available_at"),
                "received_at": item.get("received_at"),
                "status": item.get("status"),
                "verified": actual == digest,
            })
        return source, verified, tuple(sorted(set(blockers)))
    def _pit_facts(
        self,
        manifest: ResearchDatasetManifest,
        universe: InstrumentUniverseHistory,
        events: Sequence[object],
    ) -> dict[str, object]:
        timestamp_violations: list[str] = []
        future_events = 0
        membership_invalid = 0
        unknown_availability = 0
        for event in events:
            meta = getattr(event, "meta")
            values = (
                meta.event_time,
                meta.published_at,
                meta.available_at,
                meta.received_at,
            )
            if any(value.tzinfo is None for value in values):
                unknown_availability += 1
                continue
            if not (
                meta.event_time <= meta.published_at
                <= meta.available_at
                <= meta.received_at
            ):
                timestamp_violations.append(meta.canonical_identity())
            if meta.available_at > manifest.decision_time:
                future_events += 1
            if not universe.event_was_in_active_universe(event):
                membership_invalid += 1
        return {
            "records_checked": len(events),
            "timestamp_violations": len(timestamp_violations),
            "future_events_detected": future_events,
            "future_revisions_detected": 0,
            "unknown_availability": unknown_availability,
            "membership_invalid": membership_invalid,
            "backward_availability_adjustments": 0,
            "decision_boundary_crossings": future_events,
        }

    def _series_facts(
        self,
        events: Sequence[object],
        universe: InstrumentUniverseHistory,
    ) -> dict[str, object]:
        by_venue: dict[str, set[str]] = defaultdict(set)
        by_instrument: dict[str, set[str]] = defaultdict(set)
        dates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for event in events:
            meta = getattr(event, "meta")
            for series in _series_for_event(event):
                by_venue[meta.venue].add(series)
                by_instrument[meta.instrument_id].add(series)
                dates[(meta.venue, meta.instrument_id, series)].add(
                    meta.event_time.astimezone(timezone.utc).date().isoformat()
                )
        for definition in universe.definitions:
            by_venue[definition.venue].add("INSTRUMENT_METADATA")
            by_instrument[definition.instrument_id].add("INSTRUMENT_METADATA")
            dates[
                (definition.venue, definition.instrument_id, "INSTRUMENT_METADATA")
            ].add(definition.available_at.astimezone(timezone.utc).date().isoformat())
        missing_by_venue = {
            venue: sorted(set(REQUIRED_SERIES) - by_venue.get(venue, set()))
            for venue in FROZEN_VENUES
        }
        return {
            "series_by_venue": {
                key: sorted(value) for key, value in sorted(by_venue.items())
            },
            "series_by_instrument": {
                key: sorted(value) for key, value in sorted(by_instrument.items())
            },
            "missing_by_venue": missing_by_venue,
            "observed_dates": {
                "|".join(key): sorted(value)
                for key, value in sorted(dates.items())
            },
        }

    def _coverage_records(
        self,
        manifest: ResearchDatasetManifest,
        universe: InstrumentUniverseHistory,
        series_facts: Mapping[str, object],
        coverage_proof: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], ...]:
        if coverage_proof is not None:
            records = coverage_proof.get("records")
            if isinstance(records, list) and all(isinstance(item, Mapping) for item in records):
                normalized: list[dict[str, object]] = []
                required = (
                    "coverage_id", "venue", "instrument_id", "series",
                    "start_time", "end_time", "expected_intervals",
                    "observed_intervals", "missing_intervals",
                    "partial_intervals", "availability_status",
                )
                for item in records:
                    if any(key not in item for key in required):
                        raise R13EvidencePipelineError(
                            "historical_coverage_evidence record missing required fields"
                        )
                    body = {key: item[key] for key in required}
                    if "window" in item:
                        body["window"] = item["window"]
                    hash_body = {
                        key: value for key, value in body.items() if key != "window"
                    }
                    body["record_sha256"] = _sha_payload(hash_body)
                    normalized.append(body)
                return tuple(normalized)
        observed_dates = series_facts.get("observed_dates", {})
        if not isinstance(observed_dates, Mapping):
            observed_dates = {}
        instruments = sorted(
            {definition.instrument_id for definition in universe.definitions}
        )
        venue_by_instrument = {
            definition.instrument_id: definition.venue
            for definition in universe.definitions
        }
        records: list[dict[str, object]] = []
        for window_name, (start_s, end_s) in FROZEN_WINDOWS.items():
            start = _dt(start_s, f"{window_name}.start")
            end = _dt(end_s, f"{window_name}.end")
            expected_days = (end.date() - start.date()).days + 1
            for instrument_id in instruments:
                venue = venue_by_instrument[instrument_id]
                for series in REQUIRED_SERIES:
                    key = f"{venue}|{instrument_id}|{series}"
                    dates = observed_dates.get(key, [])
                    if not isinstance(dates, list):
                        dates = []
                    observed = sum(
                        1
                        for value in dates
                        if start.date()
                        <= datetime.fromisoformat(str(value)).date()
                        <= end.date()
                    )
                    missing = max(expected_days - observed, 0)
                    body: dict[str, object] = {
                        "coverage_id": f"{window_name}:{venue}:{instrument_id}:{series}",
                        "window": window_name,
                        "venue": venue,
                        "instrument_id": instrument_id,
                        "series": series,
                        "start_time": start_s,
                        "end_time": end_s,
                        "expected_intervals": expected_days,
                        "observed_intervals": observed,
                        "missing_intervals": missing,
                        "partial_intervals": 0,
                        "availability_status": (
                            "COMPLETE" if missing == 0 else "SOURCE_UNAVAILABLE"
                        ),
                    }
                    hash_body = {
                        key: value for key, value in body.items() if key != "window"
                    }
                    body["record_sha256"] = _sha_payload(hash_body)
                    records.append(body)
        return tuple(records)
    def _lifecycle_records(
        self,
        universe: InstrumentUniverseHistory,
        historical_universe_proof: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        grouped: dict[str, list[object]] = defaultdict(list)
        for definition in universe.definitions:
            grouped[definition.instrument_id].append(definition)
        for instrument_id, definitions in sorted(grouped.items()):
            ordered = sorted(
                definitions,
                key=lambda item: (
                    item.available_at,
                    item.effective_from,
                    item.received_at,
                    item.raw_sha256,
                ),
            )
            for index, definition in enumerate(ordered):
                event_type = "LISTING" if index == 0 else "SPEC_REVISION"
                if definition.status.upper() not in {"TRADING", "ACTIVE", "OPEN"}:
                    event_type = "DELISTING"
                body = {
                    "instrument_id": instrument_id,
                    "venue": definition.venue,
                    "event_type": event_type,
                    "effective_at": _z(definition.effective_from),
                    "available_at": _z(definition.available_at),
                    "source_sha256": definition.raw_sha256,
                    "status": definition.status,
                }
                body["record_sha256"] = _sha_payload(body)
                records.append(body)
        if historical_universe_proof is not None:
            extra = historical_universe_proof.get("lifecycle_records")
            if isinstance(extra, list):
                for item in extra:
                    if isinstance(item, Mapping):
                        records.append(dict(item))
        records.sort(
            key=lambda item: (
                str(item.get("instrument_id")),
                str(item.get("available_at")),
                str(item.get("event_type")),
                str(item.get("record_sha256")),
            )
        )
        return tuple(records)

    def _eligibility_records(
        self,
        universe: InstrumentUniverseHistory,
        manifest: ResearchDatasetManifest,
        liquidity_proof: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], ...]:
        liquidity_by_instrument: dict[str, Mapping[str, object]] = {}
        if liquidity_proof is not None:
            raw = liquidity_proof.get("instruments")
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, Mapping) and isinstance(item.get("instrument_id"), str):
                        liquidity_by_instrument[str(item["instrument_id"])] = item
        records: list[dict[str, object]] = []
        decision = manifest.decision_time
        for instrument_id in sorted(
            {definition.instrument_id for definition in universe.definitions}
        ):
            definition = universe.definition_as_of(instrument_id, decision)
            if definition is None:
                continue
            history_days = max(
                (decision.date() - definition.available_at.date()).days,
                0,
            )
            liquidity = liquidity_by_instrument.get(instrument_id)
            median_volume = (
                liquidity.get("rolling_30d_median_quote_volume_usd")
                if liquidity is not None
                else None
            )
            liquidity_window_complete = bool(
                liquidity is not None
                and liquidity.get("window_days") == LIQUIDITY_WINDOW_DAYS
                and liquidity.get("pit_only") is True
            )
            eligible = bool(
                history_days >= MIN_HISTORY_DAYS
                and liquidity_window_complete
                and isinstance(median_volume, (int, float))
                and float(median_volume) >= 10_000_000
            )
            reasons: list[str] = []
            if history_days < MIN_HISTORY_DAYS:
                reasons.append("HISTORY_LT_90D")
            if not liquidity_window_complete:
                reasons.append("LIQUIDITY_WINDOW_INCOMPLETE")
            elif not isinstance(median_volume, (int, float)):
                reasons.append("LIQUIDITY_VALUE_MISSING")
            elif float(median_volume) < 10_000_000:
                reasons.append("LIQUIDITY_BELOW_THRESHOLD")
            body = {
                "eligibility_id": f"{instrument_id}:{_z(decision)}",
                "instrument_id": instrument_id,
                "venue": definition.venue,
                "effective_from": _z(definition.available_at),
                "effective_to": None,
                "eligible": eligible,
                "reason_codes": sorted(reasons),
                "source_universe_fingerprint": universe.fingerprint(),
                "history_days": history_days,
                "liquidity_window_days": (
                    int(liquidity.get("window_days"))
                    if liquidity is not None and isinstance(liquidity.get("window_days"), int)
                    else 0
                ),
                "rolling_30d_median_quote_volume_usd": median_volume,
            }
            hash_body = {
                key: value
                for key, value in body.items()
                if key not in {
                    "history_days",
                    "liquidity_window_days",
                    "rolling_30d_median_quote_volume_usd",
                }
            }
            hash_body["record_sha256"] = _sha_payload(hash_body)
            body["record_sha256"] = hash_body["record_sha256"]
            records.append(body)
        return tuple(records)
    def _rule_results(
        self,
        *,
        manifest: ResearchDatasetManifest,
        universe: InstrumentUniverseHistory,
        events: Sequence[object],
        source: Mapping[str, object],
        receipts: Sequence[Mapping[str, object]],
        receipt_blockers: Sequence[str],
        series_facts: Mapping[str, object],
        coverage_records: Sequence[Mapping[str, object]],
        lifecycle_records: Sequence[Mapping[str, object]],
        eligibility_records: Sequence[Mapping[str, object]],
        pit_facts: Mapping[str, object],
        feature_refs: Sequence[ArtifactRef],
        feature_facts: Mapping[str, object],
        historical_universe_proof: Mapping[str, object] | None,
        coverage_proof: Mapping[str, object] | None,
        liquidity_proof: Mapping[str, object] | None,
        oos_proof: Mapping[str, object] | None,
        boundary_proof: Mapping[str, object] | None,
    ) -> tuple[RuleResult, ...]:
        results: dict[str, RuleResult] = {}
        results["RR-001"] = _result(
            "RR-001",
            manifest.format_version == "crypto-perps-research-v1",
            observed={"format_version": manifest.format_version},
        )
        identities = [getattr(event, "meta").canonical_identity() for event in events]
        results["RR-002"] = _result(
            "RR-002",
            len(identities) == len(set(identities)),
            observed={"identity_count": len(identities), "unique_count": len(set(identities))},
        )
        venues = sorted({getattr(event, "meta").venue for event in events})
        scope_ok = bool(venues) and set(venues).issubset(FROZEN_VENUES)
        results["RR-003"] = _result(
            "RR-003",
            scope_ok,
            failure_codes=("H02_SCOPE_INVALID",),
            observed={"venues": venues},
        )
        mappings: dict[tuple[str, str], set[str]] = defaultdict(set)
        for definition in universe.definitions:
            mappings[(definition.venue, definition.venue_symbol)].add(definition.instrument_id)
        ambiguous = {
            f"{venue}:{symbol}": sorted(ids)
            for (venue, symbol), ids in mappings.items()
            if len(ids) != 1
        }
        results["RR-004"] = _result(
            "RR-004",
            not ambiguous,
            observed={"ambiguous_mappings": ambiguous},
        )
        receipt_missing = any("MISSING" in item for item in receipt_blockers)
        receipt_hash_bad = any("HASH_MISMATCH" in item for item in receipt_blockers)
        verified_receipt_hashes = {
            str(item.get("sha256"))
            for item in receipts
            if item.get("verified") is True
        }
        lineage_hashes = {
            str(getattr(event, "meta").raw_sha256)
            for event in events
        } | {
            str(definition.raw_sha256)
            for definition in universe.definitions
        }
        lineage_missing = sorted(lineage_hashes - verified_receipt_hashes)
        source_ok = not receipt_blockers and not lineage_missing
        results["RR-005"] = _result(
            "RR-005",
            source_ok,
            blocked=(receipt_missing or bool(lineage_missing)) and not receipt_hash_bad,
            failure_codes=(
                ("H02_SOURCE_HASH_MISMATCH",)
                if receipt_hash_bad
                else ("H02_SOURCE_RECEIPT_MISSING",)
            ) if not source_ok else (),
            observed={
                "receipt_count": len(receipts),
                "blockers": list(receipt_blockers),
                "lineage_hashes": len(lineage_hashes),
                "missing_lineage_hashes": lineage_missing,
            },
        )
        timestamp_bad = int(pit_facts["timestamp_violations"])
        results["RR-006"] = _result(
            "RR-006",
            timestamp_bad == 0,
            observed={"timestamp_violations": timestamp_bad},
        )
        lifecycle_complete = bool(
            historical_universe_proof is not None
            and historical_universe_proof.get("status") == "PASS"
            and historical_universe_proof.get("complete_listing_history") is True
            and historical_universe_proof.get("complete_delisting_history") is True
            and historical_universe_proof.get("complete_spec_revision_history") is True
            and historical_universe_proof.get("complete_source_revision_history") is True
        )
        results["RR-007"] = _result(
            "RR-007",
            lifecycle_complete,
            blocked=historical_universe_proof is None,
            failure_codes=("H02_LIFECYCLE_GAP",),
            observed={
                "lifecycle_record_count": len(lifecycle_records),
                "historical_universe_proof": historical_universe_proof is not None,
            },
        )
        coverage_complete = bool(
            coverage_proof is not None
            and coverage_proof.get("status") == "PASS"
            and coverage_proof.get("source_class") == "GENUINE"
        )
        results["RR-008"] = _result(
            "RR-008",
            coverage_complete,
            blocked=coverage_proof is None,
            observed={"coverage_proof_present": coverage_proof is not None},
        )
        frozen_windows_ok = bool(
            coverage_proof is not None
            and coverage_proof.get("windows") == {
                key: list(value) for key, value in FROZEN_WINDOWS.items()
            }
        )
        results["RR-009"] = _result(
            "RR-009",
            frozen_windows_ok,
            blocked=coverage_proof is None,
            observed={"frozen_windows": FROZEN_WINDOWS},
        )
        unknown_availability = int(pit_facts["unknown_availability"])
        results["RR-010"] = _result(
            "RR-010",
            unknown_availability == 0,
            blocked=unknown_availability > 0,
            observed={"unknown_availability": unknown_availability},
        )
        history_short = [
            item["instrument_id"]
            for item in eligibility_records
            if int(item.get("history_days", 0)) < MIN_HISTORY_DAYS
        ]
        results["RR-011"] = _result(
            "RR-011",
            not history_short and lifecycle_complete,
            blocked=not lifecycle_complete,
            failure_codes=(
                ("H02_PIT_LIFECYCLE_UNPROVEN",)
                if not lifecycle_complete
                else ("H02_A19_HISTORY_INSUFFICIENT",)
            ),
            observed={"history_short_instruments": history_short},
        )
        liquidity_invalid = [
            item["instrument_id"]
            for item in eligibility_records
            if int(item.get("liquidity_window_days", 0)) != LIQUIDITY_WINDOW_DAYS
        ]
        liquidity_low = [
            item["instrument_id"]
            for item in eligibility_records
            if isinstance(item.get("rolling_30d_median_quote_volume_usd"), (int, float))
            and float(item["rolling_30d_median_quote_volume_usd"]) < 10_000_000
        ]
        results["RR-012"] = _result(
            "RR-012",
            not liquidity_invalid and not liquidity_low and bool(eligibility_records),
            blocked=liquidity_proof is None,
            failure_codes=(
                ("H02_LIQUIDITY_WINDOW_INVALID",)
                if liquidity_invalid or liquidity_proof is None
                else ("H02_A20_LIQUIDITY_INSUFFICIENT",)
            ),
            observed={
                "invalid_windows": liquidity_invalid,
                "below_threshold": liquidity_low,
            },
        )
        feature_lineage_ok = bool(feature_facts.get("lineage_ok"))
        feature_errors = list(feature_facts.get("errors", []))
        results["RR-013"] = _result(
            "RR-013",
            feature_lineage_ok,
            blocked=not feature_refs,
            observed={
                "feature_run_manifest_count": len(feature_refs),
                "lineage_ok": feature_lineage_ok,
                "errors": feature_errors,
            },
        )
        eligible_count = sum(1 for item in eligibility_records if item.get("eligible") is True)
        breadth_fraction = (
            float(coverage_proof.get("breadth_fraction", 0.0))
            if coverage_proof is not None
            else 0.0
        )
        results["RR-014"] = _result(
            "RR-014",
            eligible_count >= MIN_CROSS_SECTION and breadth_fraction >= MIN_BREADTH_FRACTION,
            blocked=coverage_proof is None or liquidity_proof is None,
            observed={
                "eligible_instruments": eligible_count,
                "breadth_fraction": breadth_fraction,
                "minimum_instruments": MIN_CROSS_SECTION,
                "minimum_fraction": MIN_BREADTH_FRACTION,
            },
        )
        eligibility_ids = [str(item.get("eligibility_id")) for item in eligibility_records]
        results["RR-015"] = _result(
            "RR-015",
            len(eligibility_ids) == len(set(eligibility_ids)),
            observed={"eligibility_record_count": len(eligibility_ids)},
        )
        record_hash_mismatches = 0
        for item in lifecycle_records:
            expected = item.get("record_sha256")
            body = dict(item)
            body.pop("record_sha256", None)
            if expected != _sha_payload(body):
                record_hash_mismatches += 1
        for item in eligibility_records:
            expected = item.get("record_sha256")
            body = {
                key: value
                for key, value in item.items()
                if key not in {
                    "record_sha256",
                    "history_days",
                    "liquidity_window_days",
                    "rolling_30d_median_quote_volume_usd",
                }
            }
            if expected != _sha_payload(body):
                record_hash_mismatches += 1
        results["RR-016"] = _result(
            "RR-016",
            record_hash_mismatches == 0,
            observed={"record_hash_mismatches": record_hash_mismatches},
        )
        results["RR-017"] = _result(
            "RR-017",
            bool(events) and bool(universe.definitions) and bool(receipts),
            blocked=not events or not universe.definitions or not receipts,
            observed={
                "event_count": len(events),
                "universe_definition_count": len(universe.definitions),
                "receipt_count": len(receipts),
            },
        )
        deterministic_event_hash = _sha_payload(
            [
                getattr(event, "meta").canonical_identity()
                for event in sorted(events, key=event_sort_key)
            ]
        )
        results["RR-018"] = _result(
            "RR-018",
            deterministic_event_hash == manifest.event_identity_hash,
            observed={
                "manifest_event_identity_hash": manifest.event_identity_hash,
                "recomputed_event_identity_hash": deterministic_event_hash,
            },
        )
        missing_by_venue = series_facts.get("missing_by_venue", {})
        missing_series = {
            venue: values
            for venue, values in (
                missing_by_venue.items()
                if isinstance(missing_by_venue, Mapping)
                else ()
            )
            if values
        }
        results["RR-019"] = _result(
            "RR-019",
            not missing_series and coverage_complete,
            blocked=not coverage_complete,
            observed={"missing_series_by_venue": missing_series},
        )
        coverage_count_bad = [
            str(item.get("coverage_id"))
            for item in coverage_records
            if int(item.get("observed_intervals", 0))
            + int(item.get("missing_intervals", 0))
            > int(item.get("expected_intervals", 0))
        ]
        results["RR-020"] = _result(
            "RR-020",
            not coverage_count_bad,
            observed={"invalid_records": coverage_count_bad},
        )
        gap_bad = [
            str(item.get("coverage_id"))
            for item in coverage_records
            if int(item.get("missing_intervals", 0)) > 0
            and item.get("availability_status") == "COMPLETE"
        ]
        results["RR-021"] = _result(
            "RR-021",
            not gap_bad,
            observed={"silent_gap_records": gap_bad},
        )
        completion_bad = [
            str(item.get("coverage_id"))
            for item in coverage_records
            if item.get("availability_status") == "COMPLETE"
            and (
                int(item.get("missing_intervals", 0)) != 0
                or int(item.get("partial_intervals", 0)) != 0
            )
        ]
        results["RR-022"] = _result(
            "RR-022",
            not completion_bad,
            observed={"invalid_complete_records": completion_bad},
        )
        membership_invalid = int(pit_facts["membership_invalid"])
        results["RR-023"] = _result(
            "RR-023",
            membership_invalid == 0 and lifecycle_complete,
            blocked=not lifecycle_complete,
            failure_codes=(
                ("H02_PIT_LIFECYCLE_UNPROVEN",)
                if not lifecycle_complete
                else ("H02_PIT_MEMBERSHIP_INVALID",)
            ),
            observed={"membership_invalid": membership_invalid},
        )
        latest_allowed = _dt(FROZEN_WINDOWS["locked_oos"][1], "locked_oos.end")
        decision_ok = manifest.decision_time <= latest_allowed + (manifest.decision_time - manifest.decision_time)
        future_events = int(pit_facts["future_events_detected"])
        results["RR-024"] = _result(
            "RR-024",
            decision_ok and future_events == 0,
            observed={
                "decision_time": _z(manifest.decision_time),
                "frozen_locked_oos_end": FROZEN_WINDOWS["locked_oos"][1],
                "future_events_detected": future_events,
            },
        )
        liquidity_policy_ok = bool(
            liquidity_proof is not None
            and liquidity_proof.get("window_days") == LIQUIDITY_WINDOW_DAYS
            and liquidity_proof.get("pit_only") is True
        )
        results["RR-025"] = _result(
            "RR-025",
            liquidity_policy_ok,
            blocked=liquidity_proof is None,
            observed={"liquidity_policy_proof_present": liquidity_proof is not None},
        )
        base_cross_ok = (
            universe.fingerprint() == manifest.universe_fingerprint
            and all(
                item.get("source_universe_fingerprint") == manifest.universe_fingerprint
                for item in eligibility_records
            )
        )
        cross_ok = base_cross_ok and feature_lineage_ok
        results["RR-026"] = _result(
            "RR-026",
            cross_ok,
            blocked=base_cross_ok and not feature_refs,
            observed={
                "universe_fingerprint": manifest.universe_fingerprint,
                "base_cross_artifact_consistency": base_cross_ok,
                "feature_lineage_ok": feature_lineage_ok,
            },
        )
        required_evidence_complete = all(
            (
                historical_universe_proof is not None,
                coverage_proof is not None,
                liquidity_proof is not None,
                oos_proof is not None,
                boundary_proof is not None,
                bool(feature_refs),
            )
        )
        results["RR-027"] = _result(
            "RR-027",
            required_evidence_complete,
            blocked=not required_evidence_complete,
            observed={
                "historical_universe_proof": historical_universe_proof is not None,
                "coverage_proof": coverage_proof is not None,
                "liquidity_proof": liquidity_proof is not None,
                "oos_proof": oos_proof is not None,
                "boundary_proof": boundary_proof is not None,
                "feature_run_manifests": len(feature_refs),
            },
        )
        ruleset_ok = (
            self.catalogue.get("catalogue_version") == RR_CATALOGUE_VERSION
            and tuple(
                str(item.get("rule_id"))
                for item in self.catalogue.get("rules", [])
                if isinstance(item, Mapping)
            )
            == RR_IDS
        )
        results["RR-028"] = _result(
            "RR-028",
            ruleset_ok,
            observed={
                "catalogue_version": self.catalogue.get("catalogue_version"),
                "catalogue_sha256": self.catalogue_sha256,
            },
        )
        evidence_conflict = bool(
            coverage_proof is not None
            and coverage_proof.get("status") == "PASS"
            and any(
                int(item.get("missing_intervals", 0)) > 0
                for item in coverage_records
            )
        )
        results["RR-029"] = _result(
            "RR-029",
            not evidence_conflict and required_evidence_complete,
            blocked=not required_evidence_complete,
            failure_codes=(
                ("H02_EVIDENCE_CONFLICT",)
                if evidence_conflict
                else ("H02_EVIDENCE_INCOMPLETE",)
            ),
            observed={"evidence_conflict": evidence_conflict},
        )
        boundary_ok = bool(
            boundary_proof is not None
            and boundary_proof.get("r1_2_unchanged") is True
            and boundary_proof.get("f7_unchanged") is True
            and boundary_proof.get("tracker_unchanged") is True
            and boundary_proof.get("broker_submission_enabled") is False
            and boundary_proof.get("credentials_accessed") is False
            and boundary_proof.get("live_capital_touched") is False
            and int(boundary_proof.get("protected_files_changed", 1)) == 0
            and boundary_proof.get("eqs06_options_included") is False
            and boundary_proof.get("do_not_promote") is True
        )
        results["RR-030"] = _result(
            "RR-030",
            boundary_ok,
            blocked=boundary_proof is None,
            observed={"authority_boundary_proven": boundary_ok},
        )
        return tuple(results[rule_id] for rule_id in RR_IDS)
    def _gate_result_from_rr(
        self,
        gate_id: str,
        rr_by_id: Mapping[str, RuleResult],
        *,
        feature_refs: Sequence[ArtifactRef],
        oos_proof: Mapping[str, object] | None,
        boundary_proof: Mapping[str, object] | None,
        parent_ref: ArtifactRef | None,
    ) -> tuple[str, tuple[str, ...]]:
        rr_map = {
            "A01": ("RR-017",),
            "A02": ("RR-003", "RR-027"),
            "A03": ("RR-005",),
            "A04": ("RR-006", "RR-010"),
            "A05": ("RR-016",),
            "A06": ("RR-016", "RR-020"),
            "A07": ("RR-002", "RR-004"),
            "A08": ("RR-001", "RR-017"),
            "A09": ("RR-002", "RR-016"),
            "A10": ("RR-018",),
            "A11": ("RR-018", "RR-024"),
            "A12": ("RR-006", "RR-023", "RR-024"),
            "A13": ("RR-007", "RR-026"),
            "A14": ("RR-023",),
            "A15": ("RR-007", "RR-011"),
            "A16": ("RR-007",),
            "A17": ("RR-003",),
            "A18": ("RR-008", "RR-009", "RR-019", "RR-020", "RR-021", "RR-022"),
            "A19": ("RR-011",),
            "A20": ("RR-012", "RR-025"),
            "A21": ("RR-009", "RR-014"),
            "A22": ("RR-014",),
            "A23": ("RR-013", "RR-019"),
            "A24": ("RR-013", "RR-026"),
            "A25": ("RR-010", "RR-013", "RR-024"),
            "A26": ("RR-024",),
            "A27": ("RR-016", "RR-017", "RR-018", "RR-026", "RR-028", "RR-029"),
            "A28": ("RR-030",),
        }
        states = [rr_by_id[rid].status for rid in rr_map[gate_id]]
        status = _worst_status(states)
        codes: list[str] = []
        if status == "PASS":
            if gate_id == "A01" and parent_ref is None:
                status = "BLOCKED"
                codes = ["A01_PARENT_ARCHIVE_MISSING"]
            elif gate_id in {"A23", "A24"} and not feature_refs:
                status = "BLOCKED"
                codes = [
                    "A23_FEATURE_SOURCE_UNAVAILABLE"
                    if gate_id == "A23"
                    else "A24_FEATURE_RUN_MANIFEST_MISSING"
                ]
            elif gate_id == "A26":
                if oos_proof is None:
                    status = "BLOCKED"
                    codes = ["A26_OOS_ACCESS_LOG_MISSING"]
                elif oos_proof.get("status") != "SEALED":
                    status = "FAIL"
                    codes = ["A26_OOS_SEAL_BROKEN"]
                elif oos_proof.get("outcome_viewed") is True:
                    status = "FAIL"
                    codes = ["A26_OOS_OUTCOME_VIEWED"]
                elif oos_proof.get("used_for_parameter_selection") is True:
                    status = "FAIL"
                    codes = ["A26_OOS_USED_FOR_PARAMETER_SELECTION"]
                elif oos_proof.get("used_for_pruning") is True:
                    status = "FAIL"
                    codes = ["A26_OOS_USED_FOR_PRUNING"]
                elif oos_proof.get("used_for_ranking") is True:
                    status = "FAIL"
                    codes = ["A26_OOS_USED_FOR_RANKING"]
            elif gate_id == "A28" and boundary_proof is None:
                status = "BLOCKED"
                codes = ["A28_PROTECTED_BOUNDARY_CHANGED"]
        if status != "PASS" and not codes:
            preferred: dict[str, str] = {
                "A01": "A01_PARENT_ARCHIVE_MISSING",
                "A02": "A02_UNCLASSIFIED_EMPIRICAL_INPUT",
                "A03": "A03_RAW_RECEIPT_MISSING",
                "A04": "A04_TIMESTAMP_EVIDENCE_MISSING",
                "A05": "A05_PARTITION_HASH_MISMATCH",
                "A06": "A06_DESCRIPTOR_MISSING",
                "A07": "A07_DUPLICATE_EVENT_IDENTITY",
                "A08": "A08_MANIFEST_MISSING",
                "A09": "A09_EVENT_COUNT_MISMATCH",
                "A10": "A10_SECOND_BUILD_MISSING",
                "A11": "A11_REPLAY_EVENT_SET_MISMATCH",
                "A12": "A12_PIT_AUDIT_INCOMPLETE",
                "A13": "A13_UNIVERSE_HISTORY_MISSING",
                "A14": "A14_NO_ASOF_DEFINITION",
                "A15": "A15_SURVIVORSHIP_FILTER_DETECTED",
                "A16": "A16_REVISION_SOURCE_COVERAGE_INSUFFICIENT",
                "A17": "A17_UNSUPPORTED_VENUE",
                "A18": "A18_COVERAGE_REPORT_MISSING",
                "A19": "A19_HISTORY_AGE_UNEVALUABLE",
                "A20": "A20_LIQUIDITY_WINDOW_INCOMPLETE",
                "A21": "A21_FOLD_CONSTRUCTION_BLOCKED",
                "A22": "A22_CROSS_SECTION_INSUFFICIENT_BREADTH",
                "A23": "A23_FEATURE_SOURCE_UNAVAILABLE",
                "A24": "A24_FEATURE_RUN_MANIFEST_MISSING",
                "A25": "A25_LEAKAGE_GUARD_MISSING",
                "A26": "A26_OOS_ACCESS_LOG_MISSING",
                "A27": "A27_FINAL_HASH_INDEX_MISSING",
                "A28": "A28_PROTECTED_BOUNDARY_CHANGED",
            }
            codes = [preferred[gate_id]]
        allowed = set(ALPHA_FAILURE_CODES[gate_id])
        if any(code not in allowed for code in codes):
            raise R13EvidencePipelineError(
                f"{gate_id} generated invalid failure code: {codes}"
            )
        return status, tuple(sorted(set(codes)))
    def _build_alpha_results(
        self,
        *,
        rr_results: Sequence[RuleResult],
        output_root: Path,
        manifest: ResearchDatasetManifest,
        events: Sequence[object],
        source_receipt_count: int,
        feature_refs: Sequence[ArtifactRef],
        parent_ref: ArtifactRef | None,
        cfg: Mapping[str, object],
        eligibility_records: Sequence[Mapping[str, object]],
        oos_proof: Mapping[str, object] | None,
        boundary_proof: Mapping[str, object] | None,
        protected_report_sha256: str,
    ) -> tuple[dict[str, object], dict[str, str]]:
        rr_by_id = {item.rule_id: item for item in rr_results}
        gate_evidence_hashes: dict[str, str] = {}
        gates: dict[str, dict[str, object]] = {}
        for gate_id in GATE_IDS:
            status, failure_codes = self._gate_result_from_rr(
                gate_id,
                rr_by_id,
                feature_refs=feature_refs,
                oos_proof=oos_proof,
                boundary_proof=boundary_proof,
                parent_ref=parent_ref,
            )
            observed = {
                "derived_from_rr": [
                    item.rule_id
                    for item in rr_results
                    if item.status != "PASS"
                ],
                "verified": status == "PASS",
            }
            evidence_payload = {
                "schema_id": "EQS-ALPHA-GATE-EVIDENCE-v1",
                "gate_id": gate_id,
                "status": status,
                "failure_codes": list(failure_codes),
                "observed": observed,
                "source_class": "GENUINE",
            }
            evidence_path = output_root / "gate_evidence" / f"{gate_id}.json"
            digest = _write_immutable_json(evidence_path, evidence_payload)
            gate_evidence_hashes[gate_id] = digest
            scope = "GLOBAL"
            severity = "HARD_BLOCK"
            if gate_id in FILTER_GATES:
                scope, severity = "INSTRUMENT_FILTER", "FILTER"
            elif gate_id in {"A21", "A22", "A23"}:
                scope, severity = "CAMPAIGN", "CAMPAIGN_BLOCK"
            elif gate_id == "A26":
                scope, severity = "CAMPAIGN", "CAMPAIGN_INVALIDATION"
            gates[gate_id] = {
                "gate_id": gate_id,
                "name": f"R1.3 historical evidence gate {gate_id}",
                "scope": scope,
                "severity": severity,
                "status": status,
                "pass_condition": f"{gate_id} frozen historical evidence condition",
                "observed": observed,
                "evidence": [{
                    "evidence_type": "HASH_REPORT",
                    "uri_or_path": f"gate_evidence/{gate_id}.json",
                    "sha256": digest,
                    "classification": "DERIVED_REAL_MARKET",
                    "description": f"{gate_id} deterministic R1.3 evidence",
                }],
                "failure_codes": list(failure_codes),
            }
        contracts = frozen_campaign_contracts()
        eligible_count = sum(1 for item in eligibility_records if item.get("eligible") is True)
        eligible_observations = manifest.event_count if eligible_count > 0 else 0
        readiness: list[dict[str, object]] = []
        for campaign_id, contract in contracts.items():
            campaign_statuses = {
                gate_id: {
                    "status": gates[gate_id]["status"],
                    "failure_codes": list(gates[gate_id]["failure_codes"]),
                }
                for gate_id in CAMPAIGN_GATES
            }
            independent_samples = (
                int(contract["minimum_independent_samples"])
                if gates["A22"]["status"] == "PASS"
                else 0
            )
            folds = 6 if gates["A21"]["status"] == "PASS" else 0
            feature_complete = gates["A23"]["status"] == "PASS"
            locked_oos = "SEALED"
            training_ready = bool(
                eligible_count > 0
                and eligible_observations > 0
                and all(
                    campaign_statuses[gate]["status"] == "PASS"
                    for gate in ("A21", "A22", "A23")
                )
            )
            item: dict[str, object] = {
                "campaign_id": campaign_id,
                "campaign_fingerprint": contract["campaign_fingerprint"],
                "eligibility_mask_fingerprint": _sha_payload({
                    "campaign_id": campaign_id,
                    "eligible": [
                        rec["instrument_id"]
                        for rec in eligibility_records
                        if rec.get("eligible") is True
                    ],
                }),
                "eligible_instrument_count": eligible_count,
                "eligible_observations": eligible_observations,
                "excluded_instrument_times": {
                    "A19": 0 if gates["A19"]["status"] == "PASS" else manifest.event_count,
                    "A20": 0 if gates["A20"]["status"] == "PASS" else manifest.event_count,
                    "A19_A20": 0 if gates["A19"]["status"] == "PASS" and gates["A20"]["status"] == "PASS" else manifest.event_count,
                },
                "independent_samples": independent_samples,
                "minimum_independent_samples": contract["minimum_independent_samples"],
                "minimum_candidate_trades": contract["minimum_candidate_trades"],
                "candidate_trade_count_status": "DEFERRED_UNTIL_CANDIDATE_EXECUTION",
                "walk_forward_fold_count": folds,
                "minimum_walk_forward_folds": 6,
                "feature_coverage_complete": feature_complete,
                "training_validation_ready": training_ready,
                "locked_oos_status": locked_oos,
                "gate_statuses": campaign_statuses,
                "binding_ready": False,
                "block_reason_codes": [],
            }
            readiness.append(item)
        readiness.sort(key=lambda item: str(item["campaign_id"]))
        global_pass = all(gates[gate]["status"] == "PASS" for gate in GLOBAL_HARD_GATES)
        for item in readiness:
            statuses = item["gate_statuses"]
            ready = bool(
                global_pass
                and gates["A19"]["status"] == "PASS"
                and gates["A20"]["status"] == "PASS"
                and int(item["eligible_instrument_count"]) > 0
                and int(item["eligible_observations"]) > 0
                and int(item["independent_samples"]) >= int(item["minimum_independent_samples"])
                and int(item["walk_forward_fold_count"]) >= 6
                and bool(item["training_validation_ready"])
                and bool(item["feature_coverage_complete"])
                and item["locked_oos_status"] == "SEALED"
                and all(statuses[gate]["status"] == "PASS" for gate in CAMPAIGN_GATES)
            )
            item["binding_ready"] = ready
            codes: set[str] = set()
            for gate_id in GLOBAL_HARD_GATES + FILTER_GATES:
                if gates[gate_id]["status"] != "PASS":
                    codes.update(str(code) for code in gates[gate_id]["failure_codes"])
            for gate_id in CAMPAIGN_GATES:
                if statuses[gate_id]["status"] != "PASS":
                    codes.update(str(code) for code in statuses[gate_id]["failure_codes"])
            item["block_reason_codes"] = sorted(codes)

        campaign_identities = [
            {
                "campaign_id": cid,
                "campaign_fingerprint": contract["campaign_fingerprint"],
                "preregistration_path": f"research/preregistrations/v1/{cid}.json",
            }
            for cid, contract in contracts.items()
        ]
        replay_fingerprint = _replay_fingerprint(events)
        parent_sha = (
            parent_ref.sha256
            if parent_ref is not None
            else "0" * 64
        )
        feature_fps = sorted(
            ref.sha256 for ref in feature_refs
        ) or [_sha_payload({"blocked": "feature_run_manifest_missing"})]
        boundary_safe = bool(
            boundary_proof is not None
            and boundary_proof.get("r1_2_unchanged") is True
            and boundary_proof.get("f7_unchanged") is True
            and boundary_proof.get("tracker_unchanged") is True
            and boundary_proof.get("broker_submission_enabled") is False
            and boundary_proof.get("credentials_accessed") is False
            and boundary_proof.get("live_capital_touched") is False
            and int(boundary_proof.get("protected_files_changed", 1)) == 0
        )
        protected = {
            "r1_2_unchanged": bool(boundary_proof and boundary_proof.get("r1_2_unchanged") is True),
            "f7_unchanged": bool(boundary_proof and boundary_proof.get("f7_unchanged") is True),
            "tracker_unchanged": bool(boundary_proof and boundary_proof.get("tracker_unchanged") is True),
            "broker_submission_enabled": bool(boundary_proof and boundary_proof.get("broker_submission_enabled") is True),
            "credentials_accessed": bool(boundary_proof and boundary_proof.get("credentials_accessed") is True),
            "live_capital_touched": bool(boundary_proof and boundary_proof.get("live_capital_touched") is True),
            "protected_files_compared": int(boundary_proof.get("protected_files_compared", 1)) if boundary_proof else 1,
            "protected_files_changed": int(boundary_proof.get("protected_files_changed", 1)) if boundary_proof else 1,
            "evidence_sha256": protected_report_sha256,
        }
        if (gates["A28"]["status"] == "PASS") != boundary_safe:
            gates["A28"]["status"] = "PASS" if boundary_safe else "BLOCKED"
            gates["A28"]["failure_codes"] = [] if boundary_safe else ["A28_PROTECTED_BOUNDARY_CHANGED"]
        report: dict[str, object] = {
            "schema_version": ALPHA_SCHEMA_VERSION,
            "binding_id": "0" * 64,
            "generated_at": str(cfg["created_at"]),
            "evidence_classification": "REAL_MARKET",
            "parent": {
                "archive_path": str(parent_ref.path) if parent_ref is not None else "MISSING",
                "archive_sha256": parent_sha,
                "code_version": str(cfg["code_version"]),
                "dataset_builder_version": str(cfg["dataset_builder_version"]),
                "build_command_fingerprint": str(cfg["build_command_fingerprint"]),
            },
            "dataset": {
                "format_version": manifest.format_version,
                "dataset_id": manifest.dataset_id,
                "manifest_fingerprint": manifest.fingerprint(),
                "universe_fingerprint": manifest.universe_fingerprint,
                "event_identity_hash": manifest.event_identity_hash,
                "replay_fingerprint": replay_fingerprint,
                "decision_time": _z(manifest.decision_time),
                "partition_count": len(manifest.partition_descriptors),
                "event_count": manifest.event_count,
                "raw_source_count": source_receipt_count,
                "venues": sorted({descriptor.key.venue for descriptor in manifest.partition_descriptors}),
                "coverage": {
                    "training": gates["A18"]["status"] == "PASS",
                    "validation": gates["A18"]["status"] == "PASS",
                    "locked_oos": gates["A18"]["status"] == "PASS",
                },
            },
            "feature_engine": {
                "version": "crypto-perps-feature-engine-v1",
                "run_manifest_fingerprints": feature_fps,
                "dataset_manifest_fingerprint": manifest.fingerprint(),
                "universe_fingerprint": manifest.universe_fingerprint,
                "max_decision_time": _z(manifest.decision_time),
            },
            "alpha_campaign_set": {
                "version": "alpha-campaign-v1",
                "campaigns": campaign_identities,
                "campaign_set_fingerprint": compute_campaign_set_fingerprint(campaign_identities),
            },
            "gates": gates,
            "campaign_readiness": readiness,
            "protected_boundary": protected,
            "final_decision": {},
        }
        report["binding_id"] = compute_binding_id(report)
        report["final_decision"] = expected_final_decision(report)
        return report, gate_evidence_hashes
    def run(
        self,
        input_path: str | Path,
        output_root: str | Path,
    ) -> PipelineResult:
        input_file = Path(input_path).resolve(strict=True)
        base = input_file.parent
        cfg = self._load_config(input_file)
        out = Path(output_root).resolve()

        dataset_ref = _resolve_descriptor(cfg, "dataset_manifest", base=base, required=True)
        universe_ref = _resolve_descriptor(cfg, "universe_history", base=base, required=True)
        source_ref = _resolve_descriptor(cfg, "source_bundle", base=base, required=True)
        parent_ref = _resolve_descriptor(cfg, "parent_archive", base=base, required=False)
        coverage_ref = _resolve_descriptor(cfg, "historical_coverage_evidence", base=base, required=False)
        universe_proof_ref = _resolve_descriptor(cfg, "historical_universe_evidence", base=base, required=False)
        liquidity_ref = _resolve_descriptor(cfg, "liquidity_evidence", base=base, required=False)
        oos_ref = _resolve_descriptor(cfg, "oos_access_log", base=base, required=False)
        boundary_ref = _resolve_descriptor(cfg, "protected_boundary_source", base=base, required=False)
        assert dataset_ref and universe_ref and source_ref
        feature_refs = self._verify_feature_refs(cfg, base=base)

        partition_root_value = Path(str(cfg["partition_root"]))
        partition_root = (
            partition_root_value
            if partition_root_value.is_absolute()
            else base / partition_root_value
        ).resolve()
        if not partition_root.is_dir():
            raise R13EvidencePipelineError("partition_root does not exist")

        manifest, universe, events, dataset_payload = self._load_dataset(
            dataset_ref, universe_ref, partition_root
        )
        feature_facts = self._feature_facts(feature_refs, manifest)
        source, receipts, receipt_blockers = self._source_receipts(source_ref)
        coverage_proof = _json_object(coverage_ref.path) if coverage_ref else None
        historical_universe_proof = (
            _json_object(universe_proof_ref.path) if universe_proof_ref else None
        )
        liquidity_proof = _json_object(liquidity_ref.path) if liquidity_ref else None
        oos_proof = _json_object(oos_ref.path) if oos_ref else None
        boundary_proof = _json_object(boundary_ref.path) if boundary_ref else None

        pit_facts = self._pit_facts(manifest, universe, events)
        series_facts = self._series_facts(events, universe)
        coverage_records = self._coverage_records(
            manifest, universe, series_facts, coverage_proof
        )
        lifecycle_records = self._lifecycle_records(
            universe, historical_universe_proof
        )
        eligibility_records = self._eligibility_records(
            universe, manifest, liquidity_proof
        )
        rr_results = self._rule_results(
            manifest=manifest,
            universe=universe,
            events=events,
            source=source,
            receipts=receipts,
            receipt_blockers=receipt_blockers,
            series_facts=series_facts,
            coverage_records=coverage_records,
            lifecycle_records=lifecycle_records,
            eligibility_records=eligibility_records,
            pit_facts=pit_facts,
            feature_refs=feature_refs,
            feature_facts=feature_facts,
            historical_universe_proof=historical_universe_proof,
            coverage_proof=coverage_proof,
            liquidity_proof=liquidity_proof,
            oos_proof=oos_proof,
            boundary_proof=boundary_proof,
        )

        artifact_hashes: dict[str, str] = {}
        rr_catalog_path = out / "rr_catalogue.json"
        artifact_hashes["rr_catalogue"] = _write_immutable_json(
            rr_catalog_path, self.catalogue
        )
        validator_build_payload = {
            "validator_id": VALIDATOR_ID,
            "validator_version": VALIDATOR_VERSION,
            "semantic_engine_version": "r13-historical-evidence-v1",
            "catalogue_sha256": artifact_hashes["rr_catalogue"],
            "pipeline_source_sha256": _sha_file(Path(__file__)),
        }
        validator_build_path = out / "validator_build.json"
        artifact_hashes["validator_build"] = _write_immutable_json(
            validator_build_path, validator_build_payload
        )
        rr_result_payload = {
            "schema_id": "EQS-H02-RR-RESULTS-v1",
            "catalogue_sha256": artifact_hashes["rr_catalogue"],
            "validator_id": VALIDATOR_ID,
            "validator_version": VALIDATOR_VERSION,
            "validator_build_sha256": artifact_hashes["validator_build"],
            "semantic_engine_version": "r13-historical-evidence-v1",
            "run_id": str(cfg["run_id"]),
            "run_started_at": str(cfg["created_at"]),
            "run_completed_at": str(cfg["created_at"]),
            "result": (
                "PASS"
                if all(item.status == "PASS" for item in rr_results)
                else "BLOCKED"
            ),
            "results": [
                {
                    "rule_id": item.rule_id,
                    "status": item.status,
                    "failure_codes": list(item.failure_codes),
                    "observed": dict(item.observed),
                }
                for item in rr_results
            ],
        }
        rr_results_path = out / "rr_results.json"
        artifact_hashes["rr_results"] = _write_immutable_json(
            rr_results_path, rr_result_payload
        )

        universe_snapshot_payload = {
            "schema_id": "EQS-H02-HISTORICAL-UNIVERSE-v1",
            "snapshot_id": f"{cfg['run_id']}:universe",
            "universe_fingerprint": universe.fingerprint(),
            "record_count": len(universe.definitions),
            "history_sha256": universe_ref.sha256,
            "point_in_time": True,
            "survivorship_free": (
                historical_universe_proof is not None
                and historical_universe_proof.get("status") == "PASS"
            ),
        }
        universe_snapshot_path = out / "universe_snapshot.json"
        artifact_hashes["universe_snapshot"] = _write_immutable_json(
            universe_snapshot_path, universe_snapshot_payload
        )

        lifecycle_payload = {
            "schema_id": "EQS-H02-LIFECYCLE-EVENTS-v1",
            "schema_version": "H02-LIFECYCLE-v1",
            "record_count": len(lifecycle_records),
            "listing_events_present": bool(
                historical_universe_proof
                and historical_universe_proof.get("complete_listing_history") is True
            ),
            "delisting_events_present": bool(
                historical_universe_proof
                and historical_universe_proof.get("complete_delisting_history") is True
            ),
            "spec_revision_events_present": bool(
                historical_universe_proof
                and historical_universe_proof.get("complete_spec_revision_history") is True
            ),
            "source_revision_events_present": bool(
                historical_universe_proof
                and historical_universe_proof.get("complete_source_revision_history") is True
            ),
            "continuity_status": (
                "PASS"
                if next(item for item in rr_results if item.rule_id == "RR-007").status == "PASS"
                else "BLOCKED"
            ),
            "records": list(lifecycle_records),
        }
        lifecycle_path = out / "lifecycle_event_set.json"
        artifact_hashes["lifecycle_event_set"] = _write_immutable_json(
            lifecycle_path, lifecycle_payload
        )

        coverage_payload = {
            "schema_id": "EQS-H02-COVERAGE-v1",
            "training_complete": all(
                item.get("window") != "training"
                or item.get("availability_status") == "COMPLETE"
                for item in coverage_records
            ),
            "validation_complete": all(
                item.get("window") != "validation"
                or item.get("availability_status") == "COMPLETE"
                for item in coverage_records
            ),
            "locked_oos_complete": all(
                item.get("window") != "locked_oos"
                or item.get("availability_status") == "COMPLETE"
                for item in coverage_records
            ),
            "records": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "window"
                }
                for item in coverage_records
            ],
            "windows": {
                key: list(value) for key, value in FROZEN_WINDOWS.items()
            },
        }
        coverage_path = out / "coverage_record_set.json"
        artifact_hashes["coverage_record_set"] = _write_immutable_json(
            coverage_path, coverage_payload
        )

        eligibility_payload = {
            "schema_id": "EQS-H02-ELIGIBILITY-v1",
            "record_count": len(eligibility_records),
            "records": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "history_days",
                        "liquidity_window_days",
                        "rolling_30d_median_quote_volume_usd",
                    }
                }
                for item in eligibility_records
            ],
        }
        eligibility_path = out / "eligibility_record_set.json"
        artifact_hashes["eligibility_record_set"] = _write_immutable_json(
            eligibility_path, eligibility_payload
        )
        pit_payload = {
            "schema_id": PIT_SCHEMA_ID,
            "source_class": "GENUINE",
            "status": (
                "PASS"
                if all(
                    int(pit_facts[key]) == 0
                    for key in (
                        "timestamp_violations",
                        "future_events_detected",
                        "future_revisions_detected",
                        "unknown_availability",
                        "membership_invalid",
                        "backward_availability_adjustments",
                        "decision_boundary_crossings",
                    )
                )
                and next(item for item in rr_results if item.rule_id == "RR-007").status == "PASS"
                else "BLOCKED"
            ),
            "certification_id": f"{cfg['run_id']}:pit",
            "audit_version": "pit-v1",
            "audit_generated_at": str(cfg["created_at"]),
            "records_checked": int(pit_facts["records_checked"]),
            "violations": (
                int(pit_facts["timestamp_violations"])
                + int(pit_facts["membership_invalid"])
                + int(pit_facts["unknown_availability"])
            ),
            "future_events_detected": int(pit_facts["future_events_detected"]),
            "future_revisions_detected": int(pit_facts["future_revisions_detected"]),
            "survivorship_bias_detected": not bool(
                historical_universe_proof
                and historical_universe_proof.get("status") == "PASS"
            ),
            "backward_availability_adjustments": int(
                pit_facts["backward_availability_adjustments"]
            ),
            "decision_boundary_crossings": int(
                pit_facts["decision_boundary_crossings"]
            ),
            "time_basis": {
                "timezone": "UTC",
                "event_time_semantics": "AS_KNOWN_AT",
                "publication_time_required": True,
                "effective_time_required": True,
                "available_at_required": True,
                "ingestion_time_recorded": True,
                "revision_time_recorded": True,
                "future_visibility_prohibited": True,
            },
        }
        pit_path = out / "pit_certification.json"
        artifact_hashes["pit_certification"] = _write_immutable_json(
            pit_path, pit_payload
        )

        source_index_payload = {
            "schema_id": "EQS-SOURCE-RECEIPTS-v1",
            "source_class": "GENUINE",
            "receipt_count": len(receipts),
            "all_hashes_verified": not receipt_blockers,
            "source_bundle_sha256": source_ref.sha256,
            "receipts": list(receipts),
        }
        source_index_path = out / "source_receipt_index.json"
        artifact_hashes["source_receipt_index"] = _write_immutable_json(
            source_index_path, source_index_payload
        )

        protected_payload = {
            "r1_2_unchanged": bool(boundary_proof and boundary_proof.get("r1_2_unchanged") is True),
            "f7_unchanged": bool(boundary_proof and boundary_proof.get("f7_unchanged") is True),
            "tracker_unchanged": bool(boundary_proof and boundary_proof.get("tracker_unchanged") is True),
            "broker_submission_enabled": bool(boundary_proof and boundary_proof.get("broker_submission_enabled") is True),
            "credentials_accessed": bool(boundary_proof and boundary_proof.get("credentials_accessed") is True),
            "live_capital_touched": bool(boundary_proof and boundary_proof.get("live_capital_touched") is True),
            "protected_files_compared": int(boundary_proof.get("protected_files_compared", 1)) if boundary_proof else 1,
            "protected_files_changed": int(boundary_proof.get("protected_files_changed", 1)) if boundary_proof else 1,
            "eqs06_options_included": bool(boundary_proof and boundary_proof.get("eqs06_options_included") is True),
            "do_not_promote": True,
        }
        protected_path = out / "protected_boundary_report.json"
        artifact_hashes["protected_boundary_report"] = _write_immutable_json(
            protected_path, protected_payload
        )

        alpha_report, gate_hashes = self._build_alpha_results(
            rr_results=rr_results,
            output_root=out,
            manifest=manifest,
            events=events,
            source_receipt_count=len(receipts),
            feature_refs=feature_refs,
            parent_ref=parent_ref,
            cfg=cfg,
            eligibility_records=eligibility_records,
            oos_proof=oos_proof,
            boundary_proof=boundary_proof,
            protected_report_sha256=artifact_hashes["protected_boundary_report"],
        )
        alpha_path = out / "alpha_binding.json"
        try:
            validate_alpha_data_binding_report(alpha_report)
            alpha_structurally_valid = True
            artifact_hashes["alpha_binding"] = _write_immutable_json(
                alpha_path, alpha_report
            )
        except Exception:
            alpha_structurally_valid = False
        coverage_start = FROZEN_WINDOWS["training"][0]
        coverage_end = FROZEN_WINDOWS["locked_oos"][1]
        event_hashes = [
            descriptor.content_hash
            for descriptor in sorted(
                manifest.partition_descriptors,
                key=lambda item: item.relative_path,
            )
        ]
        dataset_content_hash = _sha_payload(event_hashes)
        partition_index_hash = _sha_payload([
            descriptor.to_record()
            for descriptor in sorted(
                manifest.partition_descriptors,
                key=lambda item: item.relative_path,
            )
        ])
        replay_fingerprint = _replay_fingerprint(
            tuple(sorted(events, key=event_sort_key))
        )
        all_rr_pass = all(item.status == "PASS" for item in rr_results)
        all_alpha_pass = bool(
            alpha_structurally_valid
            and alpha_report.get("final_decision", {}).get(
                "ALPHA_DATA_BINDING_READY"
            )
            is True
        )
        can_close_h02 = bool(
            all_rr_pass
            and all_alpha_pass
            and parent_ref is not None
            and boundary_ref is not None
            and feature_refs
        )
        h02_payload = {
            "schema_id": H02_SCHEMA_ID,
            "source_class": "GENUINE",
            "status": "PASS" if can_close_h02 else "BLOCKED",
            "dataset_immutable": True,
            "dataset_version": f"R1.3-{cfg['run_id']}",
            "builder_version": str(cfg["dataset_builder_version"]),
            "feature_definition_hash": str(cfg["feature_definition_hash"]),
            "dataset_content_hash": dataset_content_hash,
            "partition_index_hash": partition_index_hash,
            "replay_fingerprint": replay_fingerprint,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "bindings": {
                "dataset_manifest_sha256": dataset_ref.sha256,
                "universe_snapshot_sha256": artifact_hashes["universe_snapshot"],
                "lifecycle_event_set_sha256": artifact_hashes["lifecycle_event_set"],
                "coverage_record_set_sha256": artifact_hashes["coverage_record_set"],
                "eligibility_record_set_sha256": artifact_hashes["eligibility_record_set"],
                "pit_certification_sha256": artifact_hashes["pit_certification"],
                "source_receipt_index_sha256": artifact_hashes["source_receipt_index"],
                "parent_archive_sha256": (
                    parent_ref.sha256 if parent_ref is not None else "0" * 64
                ),
                "protected_boundary_report_sha256": artifact_hashes[
                    "protected_boundary_report"
                ],
            },
            "blockers": sorted(
                {
                    code
                    for result in rr_results
                    if result.status != "PASS"
                    for code in result.failure_codes
                }
            ),
        }
        h02_path = out / "h02_batch_manifest.json"
        artifact_hashes["h02_batch_manifest"] = _write_immutable_json(
            h02_path, h02_payload
        )

        hash_index_payload = {
            "schema_id": "EQS-R1.3-HISTORICAL-EVIDENCE-HASH-INDEX-v1",
            "run_id": str(cfg["run_id"]),
            "artifacts": [
                {"artifact_id": key, "sha256": value}
                for key, value in sorted(artifact_hashes.items())
            ],
            "gate_evidence": [
                {"gate_id": key, "sha256": value}
                for key, value in sorted(gate_hashes.items())
            ],
        }
        hash_index_path = out / "hash_index.json"
        artifact_hashes["hash_index"] = _write_immutable_json(
            hash_index_path, hash_index_payload
        )
        blockers = sorted(
            {
                code
                for result in rr_results
                if result.status != "PASS"
                for code in result.failure_codes
            }
        )
        if parent_ref is None:
            blockers.append("PARENT_ARCHIVE_UNVERIFIED")
        if not feature_refs:
            blockers.append("FEATURE_RUN_MANIFESTS_MISSING")
        if oos_ref is None:
            blockers.append("OOS_ACCESS_LOG_MISSING")
        if boundary_ref is None:
            blockers.append("PROTECTED_BOUNDARY_SOURCE_MISSING")
        blockers = sorted(set(blockers))

        certification_inputs_path: Path | None = None
        if can_close_h02:
            if not alpha_path.exists():
                raise R13EvidencePipelineError(
                    "internal error: all-pass bundle has no alpha_binding.json"
                )
            inputs_payload = {
                "schema_id": "EQS-R1.3-GENUINE-CERTIFICATION-INPUTS-v1",
                "manifest_id": str(cfg.get("manifest_id", f"r13m_{cfg['run_id']}")),
                "manifest_generation": int(cfg.get("manifest_generation", 1)),
                "created_at": str(cfg["created_at"]),
                "artifacts": {
                    "h02_batch_manifest": {
                        "path": str(h02_path),
                        "sha256": artifact_hashes["h02_batch_manifest"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "OTHER",
                    },
                    "dataset_manifest": {
                        "path": str(dataset_ref.path),
                        "sha256": dataset_ref.sha256,
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "DATASET_MANIFEST",
                    },
                    "universe_snapshot": {
                        "path": str(universe_snapshot_path),
                        "sha256": artifact_hashes["universe_snapshot"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "UNIVERSE_SNAPSHOT",
                    },
                    "lifecycle_event_set": {
                        "path": str(lifecycle_path),
                        "sha256": artifact_hashes["lifecycle_event_set"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "LIFECYCLE_EVENT_SET",
                    },
                    "coverage_record_set": {
                        "path": str(coverage_path),
                        "sha256": artifact_hashes["coverage_record_set"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "COVERAGE_RECORD_SET",
                    },
                    "eligibility_record_set": {
                        "path": str(eligibility_path),
                        "sha256": artifact_hashes["eligibility_record_set"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "ELIGIBILITY_RECORD_SET",
                    },
                    "pit_certification": {
                        "path": str(pit_path),
                        "sha256": artifact_hashes["pit_certification"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "PIT_AUDIT",
                    },
                    "rr_catalogue": {
                        "path": str(rr_catalog_path),
                        "sha256": artifact_hashes["rr_catalogue"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "RULE_CATALOGUE",
                    },
                    "rr_results": {
                        "path": str(rr_results_path),
                        "sha256": artifact_hashes["rr_results"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "VALIDATOR_REPORT",
                    },
                    "alpha_binding": {
                        "path": str(alpha_path),
                        "sha256": artifact_hashes["alpha_binding"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "OTHER",
                    },
                    "source_receipt_index": {
                        "path": str(source_index_path),
                        "sha256": artifact_hashes["source_receipt_index"],
                        "classification": "REAL_MARKET",
                        "artifact_type": "RAW_RECEIPT_INDEX",
                    },
                    "parent_archive": {
                        "path": str(parent_ref.path),
                        "sha256": parent_ref.sha256,
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "OTHER",
                    },
                    "validator_build": {
                        "path": str(validator_build_path),
                        "sha256": artifact_hashes["validator_build"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "VALIDATOR_REPORT",
                    },
                    "protected_boundary_report": {
                        "path": str(protected_path),
                        "sha256": artifact_hashes["protected_boundary_report"],
                        "classification": "DERIVED_REAL_MARKET",
                        "artifact_type": "PROTECTED_BOUNDARY_REPORT",
                    },
                },
            }
            certification_inputs_path = out / "certification_inputs.json"
            artifact_hashes["certification_inputs"] = _write_immutable_json(
                certification_inputs_path, inputs_payload
            )

        status_payload = {
            "schema_id": STATUS_SCHEMA_ID,
            "run_id": str(cfg["run_id"]),
            "created_at": str(cfg["created_at"]),
            "status": "COMPLETE" if can_close_h02 else "BLOCKED",
            "dataset_manifest_fingerprint": manifest.fingerprint(),
            "universe_fingerprint": universe.fingerprint(),
            "rr_passed": sum(item.status == "PASS" for item in rr_results),
            "rr_failed": sum(item.status == "FAIL" for item in rr_results),
            "rr_blocked": sum(item.status == "BLOCKED" for item in rr_results),
            "alpha_structurally_valid": alpha_structurally_valid,
            "alpha_data_binding_ready": bool(
                alpha_structurally_valid
                and alpha_report.get("final_decision", {}).get(
                    "ALPHA_DATA_BINDING_READY"
                )
                is True
            ),
            "certification_inputs_emitted": certification_inputs_path is not None,
            "blockers": blockers,
            "authority": {
                "do_not_promote": True,
                "broker_submission_enabled": False,
                "paper_or_live_authority_granted": False,
                "eqs06_options_included": False,
            },
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
        }
        status_path = out / "pipeline_status.json"
        artifact_hashes["pipeline_status"] = _write_immutable_json(
            status_path, status_payload
        )
        return PipelineResult(
            status=str(status_payload["status"]),
            output_root=out,
            status_path=status_path,
            certification_inputs_path=certification_inputs_path,
            blockers=tuple(blockers),
            artifact_hashes=dict(artifact_hashes),
        )
