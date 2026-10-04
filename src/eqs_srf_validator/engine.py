from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator


# ---------------------------------------------------------------------------
# Deterministic canonicalization / hashing
# ---------------------------------------------------------------------------

MAX_SAFE_INTEGER = 9007199254740991


def _utf16_sort_key(value: str) -> bytes:
    # JCS property ordering uses UTF-16 code units.
    return value.encode("utf-16-be", "surrogatepass")


def _json_string(value: str) -> str:
    # Reject lone surrogates: the reference implementation is intentionally
    # I-JSON strict rather than allowing platform-dependent Unicode handling.
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError("lone UTF-16 surrogate is not permitted in canonical JSON")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _expand_decimal(mantissa: str, exponent: int) -> str:
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    if "." in mantissa:
        whole, frac = mantissa.split(".", 1)
    else:
        whole, frac = mantissa, ""
    digits = whole + frac
    point = len(whole) + exponent
    if point <= 0:
        out = "0." + ("0" * (-point)) + digits
    elif point >= len(digits):
        out = digits + ("0" * (point - len(digits)))
    else:
        out = digits[:point] + "." + digits[point:]
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out == "":
        out = "0"
    return sign + out


def _number_to_jcs(value: int | float) -> str:
    if isinstance(value, bool):
        raise TypeError("bool is not a number for canonicalization")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("integer exceeds IEEE-754 safe integer range required by JCS/I-JSON")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("NaN and Infinity are forbidden by JCS")
    if value == 0:
        return "0"

    # Python's repr is shortest-round-trip for IEEE-754 binary64.  We then
    # transform notation to ECMAScript JSON number thresholds used by JCS.
    s = repr(float(value)).lower()
    if "e" in s:
        mantissa, exp_text = s.split("e", 1)
        exponent = int(exp_text)
        abs_value = abs(value)
        if 1e-6 <= abs_value < 1e21:
            return _expand_decimal(mantissa, exponent)
        if mantissa.endswith(".0"):
            mantissa = mantissa[:-2]
        sign = "+" if exponent >= 0 else "-"
        return f"{mantissa}e{sign}{abs(exponent)}"
    if s.endswith(".0"):
        s = s[:-2]
    return s


def canonical_json_bytes(value: Any) -> bytes:
    def encode(v: Any) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return _number_to_jcs(v)
        if isinstance(v, str):
            return _json_string(v)
        if isinstance(v, list):
            return "[" + ",".join(encode(x) for x in v) + "]"
        if isinstance(v, dict):
            if not all(isinstance(k, str) for k in v):
                raise TypeError("canonical JSON object keys must be strings")
            parts = []
            for key in sorted(v.keys(), key=_utf16_sort_key):
                parts.append(_json_string(key) + ":" + encode(v[key]))
            return "{" + ",".join(parts) + "}"
        raise TypeError(f"unsupported canonical JSON type: {type(v).__name__}")

    return encode(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


OWN_HASH_FIELDS: dict[str, tuple[str, ...]] = {
    "EQSUniverseDefinition": ("definition_hash",),
    "EQSStrategyDefinition": ("definition_hash",),
    "EQSResearchCampaign": ("campaign_hash",),
    "EQSPreregistration": ("canonical_payload_hash",),
    "EQSDataBinding": ("binding_hash",),
    "EQSCodeBinding": ("binding_hash",),
    "EQSEnvironmentBinding": ("binding_hash",),
    "EQSFeatureDefinition": ("definition_hash",),
    "EQSLabelDefinition": ("definition_hash",),
    "EQSCrossValidationSpec": ("definition_hash",),
    "EQSExecutionModelDefinition": ("definition_hash",),
    "EQSCostModel": ("definition_hash",),
    "EQSMetricDefinition": ("definition_hash",),
    "EQSExperiment": ("result_hash",),
    "EQSResearchSearchLedger": ("ledger_hash",),
    "EQSOOSAuthorization": ("authorization_hash",),
    "EQSOOSLock": ("lock_hash",),
    "EQSOOSAccessEvent": ("event_hash",),
    "EQSResearchEvidenceManifest": ("manifest_hash",),
    "EQSCandidateSeal": ("candidate_hash",),
    "EQSStressResult": ("result_hash",),
    "EQSReproductionRecord": ("record_hash",),
    "EQSResearchPaperHandoff": ("handoff_hash",),
    "EQSValidationReport": ("report_hash",),
}


def hash_payload(obj: Mapping[str, Any], hash_field: str | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(dict(obj))
    if hash_field is None:
        fields = OWN_HASH_FIELDS.get(str(obj.get("object_type")), ())
        if len(fields) != 1:
            raise ValueError(f"cannot infer unique own hash field for {obj.get('object_type')}")
        hash_field = fields[0]
    payload.pop(hash_field, None)
    return payload


def seal_object(obj: Mapping[str, Any], hash_field: str | None = None) -> dict[str, Any]:
    out = copy.deepcopy(dict(obj))
    if hash_field is None:
        fields = OWN_HASH_FIELDS.get(str(out.get("object_type")), ())
        if len(fields) != 1:
            raise ValueError(f"cannot infer unique own hash field for {out.get('object_type')}")
        hash_field = fields[0]
    out[hash_field] = canonical_sha256(hash_payload(out, hash_field))
    return out


# ---------------------------------------------------------------------------
# Identity, reference and object store
# ---------------------------------------------------------------------------

ID_FIELDS: dict[str, str] = {
    "EQSUniverseDefinition": "universe_definition_id",
    "EQSStrategyDefinition": "strategy_definition_id",
    "EQSResearchCampaign": "campaign_id",
    "EQSPreregistration": "preregistration_id",
    "EQSDataBinding": "data_binding_id",
    "EQSCodeBinding": "code_binding_id",
    "EQSEnvironmentBinding": "environment_binding_id",
    "EQSFeatureDefinition": "feature_id",
    "EQSLabelDefinition": "label_id",
    "EQSCrossValidationSpec": "cross_validation_spec_id",
    "EQSExecutionModelDefinition": "execution_model_id",
    "EQSCostModel": "cost_model_id",
    "EQSMetricDefinition": "metric_id",
    "EQSMetricObservation": "metric_observation_id",
    "EQSExperiment": "experiment_id",
    "EQSResearchSearchLedger": "search_ledger_id",
    "EQSOOSAuthorization": "authorization_id",
    "EQSOOSLock": "oos_lock_id",
    "EQSOOSAccessEvent": "access_event_id",
    "EQSResearchEvidenceManifest": "evidence_manifest_id",
    "EQSCandidateSeal": "candidate_id",
    "EQSMarketEvent": "event_id",
    "EQSDecision": "decision_id",
    "EQSOrder": "order_id",
    "EQSFill": "fill_id",
    "EQSStressResult": "stress_run_id",
    "EQSReproductionRecord": "reproduction_id",
    "EQSResearchPaperHandoff": "handoff_id",
    "EQSValidationResult": "validation_result_id",
    "EQSValidationReport": "validation_report_id",
}

VERSION_FIELDS: dict[str, str] = {
    "EQSUniverseDefinition": "version",
    "EQSStrategyDefinition": "version",
    "EQSResearchCampaign": "campaign_version",
    "EQSFeatureDefinition": "feature_version",
    "EQSLabelDefinition": "label_version",
    "EQSExecutionModelDefinition": "version",
    "EQSCostModel": "version",
    "EQSMetricDefinition": "metric_version",
    "EQSCandidateSeal": "candidate_version",
}

TYPE_HASH_FIELDS: dict[str, str] = {
    k: v[0] for k, v in OWN_HASH_FIELDS.items() if len(v) == 1
}


@dataclass(frozen=True)
class ReferenceSpec:
    owner_type: str
    owner_id: str
    path: str
    expected_type: str | None
    object_id: str
    version: str | int | None = None
    object_hash: str | None = None

    @property
    def synthetic_id(self) -> str:
        raw = f"{self.owner_type}|{self.owner_id}|{self.path}|{self.expected_type}|{self.object_id}|{self.version}|{self.object_hash}"
        return "REF-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


class ObjectStore:
    def __init__(self, objects: Iterable[Mapping[str, Any]] = ()) -> None:
        self.objects: list[dict[str, Any]] = []
        self.by_type: dict[str, list[dict[str, Any]]] = {}
        for obj in objects:
            self.add(obj)

    def add(self, obj: Mapping[str, Any]) -> None:
        item = copy.deepcopy(dict(obj))
        typ = str(item.get("object_type", ""))
        self.objects.append(item)
        self.by_type.setdefault(typ, []).append(item)

    def id_of(self, obj: Mapping[str, Any]) -> str:
        typ = str(obj.get("object_type", ""))
        fld = ID_FIELDS.get(typ)
        if fld and fld in obj:
            return str(obj[fld])
        return "OBJ-" + canonical_sha256(obj)[:24]

    def type_objects(self, object_type: str) -> list[dict[str, Any]]:
        return list(self.by_type.get(object_type, []))

    def resolve_all(
        self,
        object_type: str | None,
        object_id: str,
        version: str | int | None = None,
        object_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        candidates = self.objects if object_type is None else self.by_type.get(object_type, [])
        out: list[dict[str, Any]] = []
        for obj in candidates:
            typ = str(obj.get("object_type", ""))
            fld = ID_FIELDS.get(typ)
            if not fld or str(obj.get(fld)) != str(object_id):
                continue
            if version is not None:
                vf = VERSION_FIELDS.get(typ)
                if not vf or str(obj.get(vf)) != str(version):
                    continue
            if object_hash is not None:
                hf = TYPE_HASH_FIELDS.get(typ)
                if not hf or obj.get(hf) != object_hash:
                    continue
            out.append(obj)
        return out

    def resolve_one(self, object_type: str | None, object_id: str, version: str | int | None = None, object_hash: str | None = None) -> dict[str, Any] | None:
        xs = self.resolve_all(object_type, object_id, version, object_hash)
        return xs[0] if len(xs) == 1 else None


# ---------------------------------------------------------------------------
# Result model and evidence
# ---------------------------------------------------------------------------

PRECEDENCE = {"PASS": 0, "NOT_APPLICABLE": 0, "BLOCK": 1, "INVALID": 2, "COMPROMISED": 3}


@dataclass
class Evaluation:
    outcome: str
    message: str
    error_code: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ValidationResult:
    data: dict[str, Any]


@dataclass
class ValidationBundle:
    results: list[dict[str, Any]]
    report: dict[str, Any]
    executed_rule_ids: list[str]
    visited_catalog_rule_count: int


class EvidenceView:
    """Thin deterministic accessor over external runtime evidence.

    The validator never invents missing evidence.  Missing required evidence
    produces BLOCK rather than an inferred PASS or inferred violation.
    """

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def by_id(self, key: str, object_id: str) -> Any:
        value = self.data.get(key, {})
        if isinstance(value, Mapping):
            return value.get(object_id)
        return None


# ---------------------------------------------------------------------------
# Validator engine
# ---------------------------------------------------------------------------

class EQSSRFValidator:
    def __init__(
        self,
        schema_bundle: Mapping[str, Any],
        validator_catalog: Mapping[str, Any],
        error_catalog: Mapping[str, Any],
    ) -> None:
        self.schema_bundle = dict(schema_bundle)
        self.catalog = dict(validator_catalog)
        self.error_catalog = dict(error_catalog)
        Draft202012Validator.check_schema(self.schema_bundle)
        self.schema_validator = Draft202012Validator(self.schema_bundle)
        self.rules = sorted(self.catalog["rules"], key=lambda r: r["rule_id"])
        if len(self.rules) != 89:
            raise ValueError(f"EQS-SRF-V1 reference engine requires exactly 89 rules; got {len(self.rules)}")
        known_errors = {e["code"] for e in self.error_catalog["codes"]}
        unknown = {r["error_code"] for r in self.rules} - known_errors
        if unknown:
            raise ValueError(f"validator catalogue contains undefined error codes: {sorted(unknown)}")

    @classmethod
    def from_contract_dir(cls, contract_dir: str | Path | None = None) -> "EQSSRFValidator":
        if contract_dir is None:
            contract_dir = Path(__file__).with_name("contracts")
        base = Path(contract_dir)
        return cls(
            json.loads((base / "eqs_srf_v1_schema_bundle.json").read_text()),
            json.loads((base / "eqs_srf_v1_validator_catalog.json").read_text()),
            json.loads((base / "eqs_srf_v1_error_codes.json").read_text()),
        )

    def validate(
        self,
        objects: Sequence[Mapping[str, Any]],
        evidence: Mapping[str, Any] | None = None,
        *,
        root_subject_type: str = "EQS-SRF-V1",
        root_subject_id: str = "EQS-SRF-V1-VALIDATION",
        evaluated_at: str | None = None,
    ) -> ValidationBundle:
        if not objects:
            raise ValueError("at least one canonical object is required")
        timestamp = evaluated_at or _now()
        ev = EvidenceView(evidence)
        store = ObjectStore(objects)
        schema_ok = {id(obj): self._schema_ok(obj) for obj in store.objects}
        references = self._extract_references(store)
        required_states = self._extract_required_states(store, ev)

        results: list[dict[str, Any]] = []
        visited: list[str] = []

        for rule in self.rules:
            visited.append(rule["rule_id"])
            subjects = self._subjects_for_rule(rule, store, references, required_states, root_subject_type, root_subject_id)
            if not subjects:
                subjects = [(root_subject_type, root_subject_id, None)]
                evaluation = Evaluation("NOT_APPLICABLE", "No object/reference/state in this validation graph is in scope for the rule.")
                results.append(self._make_result(rule, subjects[0], evaluation, timestamp))
                continue

            for subject in subjects:
                typ, sid, value = subject
                # Structural failure is already represented by GEN-001.  Avoid
                # cascading semantic claims from malformed objects.
                if isinstance(value, dict) and value in store.objects and rule["rule_id"] not in {"SRF-GEN-001", "SRF-GEN-002"}:
                    if not schema_ok.get(id(value), False):
                        evaluation = Evaluation(
                            "NOT_APPLICABLE",
                            "Semantic rule not evaluated because the subject failed structural schema validation; SRF-GEN-001 controls fail-closed aggregation.",
                        )
                        results.append(self._make_result(rule, subject, evaluation, timestamp))
                        continue
                try:
                    evaluation = self._evaluate_rule(rule, subject, store, ev, references, required_states)
                except Exception as exc:  # deterministic fail-closed fallback
                    requested = rule.get("on_fail", "BLOCK")
                    outcome = requested if PRECEDENCE.get(requested, 1) >= PRECEDENCE["BLOCK"] else "BLOCK"
                    evaluation = Evaluation(
                        outcome,
                        f"Rule execution error failed closed: {type(exc).__name__}: {exc}",
                        rule.get("error_code"),
                    )
                results.append(self._make_result(rule, subject, evaluation, timestamp))

        results.sort(key=lambda x: (x["rule_id"], x["subject_type"], x["subject_id"], x["validation_result_id"]))
        aggregate = self._aggregate(results)
        report = {
            "object_type": "EQSValidationReport",
            "schema_version": "EQS-VALIDATION-REPORT-1.0",
            "validation_report_id": "VRPT-" + hashlib.sha256((root_subject_type + "|" + root_subject_id + "|" + timestamp).encode()).hexdigest()[:24],
            "subject_type": root_subject_type,
            "subject_id": root_subject_id,
            "catalog_version": "EQS-SRF-VALIDATOR-CATALOG-1.0",
            "result_ids": [r["validation_result_id"] for r in results],
            "aggregate_outcome": aggregate,
            "generated_at": timestamp,
            "report_hash": "0" * 64,
        }
        report = seal_object(report, "report_hash")
        self._assert_generated_schema(report)
        for r in results:
            self._assert_generated_schema(r)
        return ValidationBundle(results=results, report=report, executed_rule_ids=sorted(set(visited)), visited_catalog_rule_count=len(visited))

    # --------------------------- dispatch ---------------------------------

    def _evaluate_rule(self, rule: Mapping[str, Any], subject: tuple[str, str, Any], store: ObjectStore, ev: EvidenceView, references: list[ReferenceSpec], required_states: list[tuple[str,str,Any]]) -> Evaluation:
        rid = rule["rule_id"]
        typ, sid, obj = subject
        fail = lambda msg, code=None, outcome=None: Evaluation(outcome or rule["on_fail"], msg, code or rule["error_code"])
        passed = lambda msg="PASS": Evaluation("PASS", msg)
        blocked_unknown = lambda what: Evaluation("BLOCK", f"Required evidence unresolved: {what}", "EQS-SRF-UNKNOWN-REQUIRED-STATE")

        # Generic rules -----------------------------------------------------
        if rid == "SRF-GEN-001":
            errors = sorted(self.schema_validator.iter_errors(obj), key=lambda e: list(e.path))
            if errors:
                msg = "; ".join(f"/{'/'.join(map(str,e.path))}: {e.message}" for e in errors[:8])
                return fail(f"JSON Schema validation failed: {msg}")
            return passed("Object validates against EQS-SRF-V1 Draft 2020-12 schema bundle.")
        if rid == "SRF-GEN-002":
            versions = self._supported_versions()
            return passed() if obj.get("schema_version") in versions else fail("Unsupported schema_version.")
        if rid == "SRF-GEN-003":
            fields = OWN_HASH_FIELDS.get(typ, ())
            if not fields:
                return Evaluation("NOT_APPLICABLE", "Object has no framework-owned canonical hash field.")
            for hf in fields:
                if obj.get(hf) is None:
                    return blocked_unknown(f"{typ}.{hf}")
                expected = self._canonical_candidate_hash(obj, store) if typ == "EQSCandidateSeal" and hf == "candidate_hash" else canonical_sha256(hash_payload(obj, hf))
                if expected is None:
                    return blocked_unknown("candidate dependencies needed for canonical hash")
                if obj.get(hf) != expected:
                    return fail(f"Canonical hash mismatch for {hf}: stored={obj.get(hf)} recomputed={expected}")
            return passed("All framework-owned canonical hashes recompute exactly.")
        if rid == "SRF-GEN-004":
            ref: ReferenceSpec = obj
            matches = store.resolve_all(ref.expected_type, ref.object_id, ref.version, ref.object_hash)
            return passed() if len(matches) == 1 else fail(f"Reference resolves to {len(matches)} objects; exactly one required.")
        if rid == "SRF-GEN-005":
            ref = obj
            any_matches = store.resolve_all(None, ref.object_id, ref.version, ref.object_hash)
            if not any_matches:
                return blocked_unknown(f"reference {ref.path} -> {ref.object_id}")
            if ref.expected_type is None:
                return passed("Reference has no stricter expected type contract.")
            return passed() if all(x.get("object_type") == ref.expected_type for x in any_matches) else fail("Resolved reference type does not match expected canonical object type.")
        if rid == "SRF-GEN-006":
            state = obj
            return fail(f"Required state is unresolved/non-passable: {state!r}") if state in {None,"UNKNOWN","UNVERIFIED","PARTIAL"} else passed()
        if rid == "SRF-GEN-007":
            alg = obj.get("hash_algorithm")
            return passed() if alg in {None, "SHA-256"} else fail("Unsupported canonical hash algorithm.")
        if rid == "SRF-GEN-008":
            ref = obj
            if ref.version is None:
                return Evaluation("NOT_APPLICABLE", "Reference is not version-qualified.")
            matches = store.resolve_all(ref.expected_type, ref.object_id, ref.version, ref.object_hash)
            return passed() if len(matches) == 1 else fail(f"Version-qualified reference resolves to {len(matches)} objects; exactly one required.")

        # Campaign ----------------------------------------------------------
        if rid == "SRF-CAM-001":
            expected = canonical_sha256(hash_payload(obj, "campaign_hash"))
            return passed() if obj["campaign_hash"] == expected else fail("Campaign hash mismatch.")
        if rid == "SRF-CAM-002":
            d = obj["date_ranges"]
            rs,re = _dt(d["research"]["start"]),_dt(d["research"]["end"])
            vs,ve = _dt(d["validation"]["start"]),_dt(d["validation"]["end"])
            os_,oe = _dt(d["oos"]["start"]),_dt(d["oos"]["end"])
            ok = rs < re <= vs < ve <= os_ < oe
            return passed() if ok else fail("Research, validation and OOS partitions are not strictly ordered/non-overlapping.")
        if rid == "SRF-CAM-003":
            prior = ev.by_id("prior_objects", sid)
            if obj.get("status") in {"DRAFT","PREREGISTRATION_PENDING"}:
                return passed("Campaign remains mutable in pre-registration state.")
            if prior is None:
                return blocked_unknown(f"prior immutable snapshot for campaign {sid}")
            return passed() if _semantic_equal(prior, obj, ignored={"status"}) else fail("Registered campaign semantics changed in place.")
        if rid == "SRF-CAM-004":
            exe = store.resolve_one("EQSExecutionModelDefinition", obj["execution_model_id"])
            cost = store.resolve_one("EQSCostModel", obj["cost_model_id"])
            if exe is None or cost is None:
                return blocked_unknown("campaign execution/cost model")
            assets=set(obj["asset_classes"])
            return passed() if assets <= set(exe["asset_classes"]) and assets <= set(cost["asset_classes"]) else fail("Campaign asset classes are not covered by both execution and cost models.")

        # Preregistration ---------------------------------------------------
        if rid == "SRF-PRE-001":
            camp = store.resolve_one("EQSResearchCampaign", obj["campaign_id"])
            if camp is None: return blocked_unknown("canonical campaign")
            return passed() if obj["campaign_hash"] == camp["campaign_hash"] else fail("Preregistration campaign hash does not match canonical campaign.")
        if rid == "SRF-PRE-002":
            expected=canonical_sha256(hash_payload(obj,"canonical_payload_hash"))
            return passed() if obj["canonical_payload_hash"]==expected else fail("Preregistration canonical hash mismatch.")
        if rid == "SRF-PRE-003":
            prior=ev.by_id("prior_objects",sid)
            if prior is None: return blocked_unknown(f"prior immutable snapshot for preregistration {sid}")
            return passed() if _semantic_equal(prior,obj) else fail("Registered preregistration changed in place.")
        if rid == "SRF-PRE-004":
            if obj.get("experiment_class") in {"EXPLORATORY","DIAGNOSTIC"}: return passed()
            return passed() if store.resolve_one("EQSPreregistration",obj["preregistration_id"]) is not None else fail("Canonical experiment has no resolvable preregistration.")

        # Data/code/environment --------------------------------------------
        if rid == "SRF-DAT-001": return passed() if obj["pit_certification_status"]=="CERTIFIED" else fail("PIT certification is not CERTIFIED.")
        if rid == "SRF-DAT-002":
            certs=ev.get("market_data_certifications")
            if certs is None: return blocked_unknown("market_data_certifications evidence")
            missing=[x for x in obj["market_data_certification_ids"] if x not in certs]
            return passed() if not missing else fail(f"Missing market-data certifications: {missing}")
        if rid == "SRF-DAT-003":
            manifest=ev.by_id("dataset_manifests",obj["data_binding_id"])
            if manifest is None: return blocked_unknown("dataset manifest bytes/object")
            expected = manifest if isinstance(manifest,str) and re.fullmatch(r"[a-f0-9]{64}",manifest) else canonical_sha256(manifest)
            return passed() if obj["dataset_manifest_hash"]==expected else fail("Dataset manifest hash mismatch.")
        if rid == "SRF-DAT-004":
            snaps=ev.by_id("source_snapshot_hashes",obj["data_binding_id"])
            if snaps is None: return blocked_unknown("source snapshot hash evidence")
            return passed() if set(obj["source_snapshot_hashes"])==set(snaps) else fail("Source snapshot hashes differ from bound data.")
        if rid == "SRF-COD-001": return passed() if obj["dirty_worktree"] is False else fail("Qualification code binding has dirty_worktree=true.")
        if rid == "SRF-COD-002":
            actual=ev.by_id("runtime_code_identity",obj["code_binding_id"])
            if actual is None: return blocked_unknown("runtime code identity")
            expected={k:obj[k] for k in ["commit_sha","tree_hash","dependency_lock_hash","build_hash"]}
            return passed() if all(actual.get(k)==v for k,v in expected.items()) else fail("Runtime code identity differs from bound code identity.")
        if rid == "SRF-ENV-001":
            actual=ev.by_id("runtime_environment_identity",obj["environment_binding_id"])
            if actual is None: return blocked_unknown("runtime environment identity")
            expected={k:obj[k] for k in ["runtime_versions","os_image_id","container_digest","dependency_manifest_hash","hardware_metadata","determinism_profile_id"]}
            return passed() if all(actual.get(k)==v for k,v in expected.items()) else fail("Runtime environment differs from bound environment.")
        if rid == "SRF-ENV-002":
            ranks={"D0_UNCONTROLLED":0,"D1_SEEDED":1,"D2_ENVIRONMENT_LOCKED":2,"D3_BIT_REPRODUCIBLE":3,"D4_TOLERANCE_REPRODUCIBLE":4}
            required=int(ev.get("required_determinism_rank",2))
            return passed() if ranks.get(obj["determinism_profile_id"],-1)>=required else fail(f"Determinism profile below required rank {required}.")

        # Features / labels -------------------------------------------------
        if rid in {"SRF-FEA-001","SRF-FEA-004"}:
            for b in obj["feature_bindings"]:
                x=store.resolve_one("EQSFeatureDefinition",b["definition_id"],b["version"])
                if x is None: return fail(f"Feature binding cannot be uniquely resolved: {b}") if rid=="SRF-FEA-001" else blocked_unknown(f"feature {b}")
                if rid=="SRF-FEA-004" and x["definition_hash"]!=b["definition_hash"]: return fail("Feature definition hash mismatch.")
            return passed()
        if rid == "SRF-FEA-002":
            xs=store.resolve_all("EQSFeatureDefinition",obj["feature_id"],obj["feature_version"])
            hashes={x["definition_hash"] for x in xs}
            return passed() if len(hashes)<=1 else fail("Same feature ID/version maps to multiple semantic hashes.")
        if rid == "SRF-FEA-003":
            lineage=ev.by_id("feature_snapshot_lineage",obj["decision_id"])
            if lineage is None: return blocked_unknown("feature snapshot known_from lineage")
            cutoff=_dt(obj["information_cutoff"])
            bad=[x for x in lineage if _dt(x["known_from"])>cutoff]
            return passed() if not bad else fail(f"{len(bad)} feature inputs were not knowable by the information cutoff.")
        if rid in {"SRF-LAB-001","SRF-LAB-003"}:
            for b in obj["label_bindings"]:
                x=store.resolve_one("EQSLabelDefinition",b["definition_id"],b["version"])
                if x is None: return fail(f"Label binding cannot be uniquely resolved: {b}") if rid=="SRF-LAB-001" else blocked_unknown(f"label {b}")
                if rid=="SRF-LAB-003" and x["definition_hash"]!=b["definition_hash"]: return fail("Label definition hash mismatch.")
            return passed()
        if rid == "SRF-LAB-002":
            leakage=ev.by_id("target_leakage",obj["experiment_id"])
            if leakage is None: return blocked_unknown("target-leakage analysis")
            return passed() if leakage is False or leakage==[] else fail("Target leakage analysis detected forward outcome information in model inputs.")

        # Cross validation --------------------------------------------------
        if rid == "SRF-CV-001":
            ok=all(_dt(f["train"]["start"]) < _dt(f["train"]["end"]) <= _dt(f["validation"]["start"]) < _dt(f["validation"]["end"]) for f in obj["folds"])
            return passed() if ok else fail("At least one CV fold has invalid temporal ordering.")
        if rid == "SRF-CV-002":
            for f in obj["folds"]:
                if _ranges_overlap(f["train"],f["validation"]): return fail("Train and validation intervals overlap.")
            return passed()
        if rid == "SRF-CV-003":
            if obj["method"] not in {"PURGED","PURGED_EMBARGOED"}: return Evaluation("NOT_APPLICABLE","CV method does not require purging.")
            req=ev.by_id("cv_required_purge_seconds",obj["cross_validation_spec_id"])
            if req is None: return blocked_unknown("required purge interval derived from label horizons")
            return passed() if _duration_seconds(obj["purge_interval"]) >= float(req) else fail("Configured purge interval is below required purge duration.")
        if rid == "SRF-CV-004":
            if obj["method"] != "PURGED_EMBARGOED": return Evaluation("NOT_APPLICABLE","CV method does not require embargo.")
            req=ev.by_id("cv_required_embargo_seconds",obj["cross_validation_spec_id"])
            if req is None: return blocked_unknown("required embargo interval")
            return passed() if _duration_seconds(obj["embargo_interval"]) >= float(req) else fail("Configured embargo interval is below required duration.")
        if rid == "SRF-CV-005": return passed() if obj["shuffle"] is False else fail("Temporal CV shuffle must be false.")

        # Experiment --------------------------------------------------------
        if rid == "SRF-EXP-001":
            camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if camp is None:return blocked_unknown("canonical campaign")
            return passed() if obj["campaign_hash"]==camp["campaign_hash"] else fail("Experiment campaign hash mismatch.")
        if rid == "SRF-EXP-002":
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if pre is None:return blocked_unknown("canonical preregistration")
            return passed() if obj["preregistration_hash"]==pre["canonical_payload_hash"] else fail("Experiment preregistration hash mismatch.")
        if rid == "SRF-EXP-003":
            strat=store.resolve_one("EQSStrategyDefinition",obj["strategy_definition_id"],obj["strategy_definition_version"])
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if strat is None or pre is None:return blocked_unknown("strategy/preregistration")
            allowed={x["strategy_family_id"] for x in pre["strategy_families"]}
            return passed() if strat["strategy_family_id"] in allowed else fail("Experiment strategy family was not preregistered.")
        if rid == "SRF-EXP-004":
            if obj["experiment_class"] in {"EXPLORATORY","DIAGNOSTIC"}: return passed()
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if pre is None:return blocked_unknown("preregistration")
            allowed={(x["feature_id"],str(x["feature_version"]),x["definition_hash"]) for x in pre["feature_sets"]}
            actual={(x["definition_id"],str(x["version"]),x["definition_hash"]) for x in obj["feature_bindings"]}
            return passed() if actual<=allowed else fail("Experiment uses feature binding not present in preregistration.")
        if rid == "SRF-EXP-005":
            if obj["experiment_class"] in {"EXPLORATORY","DIAGNOSTIC"}: return passed()
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if pre is None:return blocked_unknown("preregistration")
            allowed={(x["label_id"],str(x["label_version"]),x["definition_hash"]) for x in pre["labels"]}
            actual={(x["definition_id"],str(x["version"]),x["definition_hash"]) for x in obj["label_bindings"]}
            return passed() if actual<=allowed else fail("Experiment uses label binding not present in preregistration.")
        if rid == "SRF-EXP-006":
            if obj["experiment_class"] in {"EXPLORATORY","DIAGNOSTIC"}: return passed()
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            strat=store.resolve_one("EQSStrategyDefinition",obj["strategy_definition_id"],obj["strategy_definition_version"])
            if pre is None or strat is None:return blocked_unknown("preregistration/strategy")
            fam=next((x for x in pre["strategy_families"] if x["strategy_family_id"]==strat["strategy_family_id"]),None)
            if fam is None:return fail("Strategy family is not preregistered.")
            return passed() if _parameters_within(obj["parameter_set"],fam["permitted_parameters"],fam["parameter_ranges"]) else fail("Experiment parameters violate preregistered domain.")
        if rid == "SRF-EXP-007":
            lineage=ev.by_id("consumed_data_times",obj["experiment_id"])
            camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if lineage is None:return blocked_unknown("consumed data-time lineage")
            if camp is None:return blocked_unknown("campaign")
            phase=obj["research_phase"]
            key={"TRAIN":"research","VALIDATION":"validation","DIAGNOSTIC":"research","STRESS":"validation","OOS":"oos"}[phase]
            rg=camp["date_ranges"][key]; start,end=_dt(rg["start"]),_dt(rg["end"])
            ok=all(start <= _dt(t) < end for t in lineage)
            return passed() if ok else fail("Experiment consumed data outside its authorized phase partition.")
        if rid == "SRF-EXP-008":
            xs=[x for x in store.type_objects("EQSExperiment") if x["campaign_id"]==obj["campaign_id"] and x["experiment_sequence"]==obj["experiment_sequence"]]
            return passed() if len(xs)==1 else fail(f"Experiment sequence appears {len(xs)} times in campaign.")
        if rid == "SRF-EXP-009":
            deps=[
                store.resolve_one("EQSResearchCampaign",obj["campaign_id"]),
                store.resolve_one("EQSPreregistration",obj["preregistration_id"]),
                store.resolve_one("EQSStrategyDefinition",obj["strategy_definition_id"],obj["strategy_definition_version"]),
                store.resolve_one("EQSCodeBinding",obj["code_binding_id"]),
                store.resolve_one("EQSDataBinding",obj["data_binding_id"]),
                store.resolve_one("EQSEnvironmentBinding",obj["environment_binding_id"]),
            ]
            if any(x is None for x in deps):return fail("Core experiment provenance cannot be resolved.")
            if canonical_sha256(obj["parameter_set"]) != obj["parameter_hash"]:return fail("Parameter hash does not match parameter_set.")
            return passed()

        # Search ledger -----------------------------------------------------
        if rid in {"SRF-SRC-001","SRF-SRC-002","SRF-SRC-003"}:
            exps=[x for x in store.type_objects("EQSExperiment") if x["campaign_id"]==obj["campaign_id"]]
            ids={x["experiment_id"] for x in exps}; listed=set(obj["experiment_ids"])
            if rid=="SRF-SRC-001": return passed() if ids<=listed else fail(f"Search ledger omits experiments: {sorted(ids-listed)}")
            if rid=="SRF-SRC-003": return passed() if listed<=ids else fail(f"Search ledger references unknown experiments: {sorted(listed-ids)}")
            counts=_recompute_search_counts(exps,ev,obj["campaign_id"])
            return passed() if obj["counts"]==counts[0] and obj["classification_counts"]==counts[1] else fail("Search-ledger counts do not reconcile to experiment registry.")

        # Execution and costs ----------------------------------------------
        if rid == "SRF-EXE-001":
            camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if camp is None:return blocked_unknown("campaign")
            return passed() if store.resolve_one("EQSExecutionModelDefinition",camp["execution_model_id"]) is not None else fail("Campaign execution model cannot be resolved.")
        if rid == "SRF-EXE-002":
            instruments=ev.by_id("execution_model_instruments",obj["execution_model_id"])
            classes=ev.get("asset_class_by_instrument")
            if instruments is None or classes is None:return blocked_unknown("execution-model instrument/asset mapping")
            bad=[i for i in instruments if classes.get(i) not in obj["asset_classes"]]
            return passed() if not bad else fail(f"Execution model incompatible with instruments: {bad}")
        if rid == "SRF-EXE-003":
            required=ev.by_id("required_lifecycle_handlers",obj["execution_model_id"])
            if required is None:return blocked_unknown("asset-adapter lifecycle requirements")
            return passed() if set(required)<=set(obj["lifecycle_handlers"]) else fail(f"Missing lifecycle handlers: {sorted(set(required)-set(obj['lifecycle_handlers']))}")
        if rid == "SRF-CST-001":
            return passed() if store.resolve_one("EQSCostModel",obj["cost_model_id"]) is not None else fail("Campaign cost model cannot be resolved.")
        if rid == "SRF-CST-002":
            fills=ev.by_id("fills_by_cost_model",obj["cost_model_id"])
            if fills is None:return blocked_unknown("fills associated with cost model")
            start=_dt(obj["effective_from"]); end=_dt(obj["effective_to"]) if obj["effective_to"] else None
            bad=[f for f in fills if not (start <= _dt(f["execution_time"]) and (end is None or _dt(f["execution_time"]) < end))]
            return passed() if not bad else fail(f"{len(bad)} fills fall outside cost-model effective interval.")
        if rid == "SRF-CST-003":
            instruments=ev.by_id("cost_model_instruments",obj["cost_model_id"]); classes=ev.get("asset_class_by_instrument")
            if instruments is None or classes is None:return blocked_unknown("cost-model instrument/asset mapping")
            bad=[i for i in instruments if classes.get(i) not in obj["asset_classes"]]
            return passed() if not bad else fail(f"Cost model incompatible with instruments: {bad}")
        if rid == "SRF-CST-004":
            state=ev.by_id("cost_application",obj["experiment_id"])
            if state is None:return blocked_unknown("cost application evidence")
            return passed() if state.get("model_bound") and not state.get("implicit_zero_default",False) else fail("Experiment used absent cost model or implicit zero-cost default.")

        # Timing ------------------------------------------------------------
        if rid == "SRF-TIM-001": return passed() if _dt(obj["signal_time"])<=_dt(obj["decision_time"]) else fail("signal_time is later than decision_time.")
        if rid == "SRF-TIM-002":
            dec=store.resolve_one("EQSDecision",obj["decision_id"])
            if dec is None:return blocked_unknown("order decision")
            return passed() if _dt(dec["decision_time"])<=_dt(obj["order_time"]) else fail("order_time precedes decision_time.")
        if rid == "SRF-TIM-003":
            order=store.resolve_one("EQSOrder",obj["order_id"])
            if order is None:return blocked_unknown("fill order")
            return passed() if _dt(order["order_time"])<=_dt(obj["execution_time"]) else fail("fill execution_time precedes order_time.")
        if rid == "SRF-TIM-004":
            lineage=ev.by_id("decision_information_lineage",obj["decision_id"])
            if lineage is None:return blocked_unknown("decision information lineage")
            cutoff=_dt(obj["information_cutoff"]); decision=_dt(obj["decision_time"])
            ok=cutoff<=decision and all(_dt(x["known_from"])<=cutoff for x in lineage)
            return passed() if ok else fail("Decision consumed information not known by its information cutoff or cutoff exceeds decision_time.")
        if rid == "SRF-TIM-005":
            order=store.resolve_one("EQSOrder",obj["order_id"])
            if order is None:return blocked_unknown("fill order")
            fills=[x for x in store.type_objects("EQSFill") if x["order_id"]==obj["order_id"]]
            return passed() if sum(abs(float(x["quantity"])) for x in fills)<=float(order["quantity"])+1e-12 else fail("Aggregate fill quantity exceeds order quantity.")
        if rid == "SRF-TIM-006":
            states=ev.get("market_state_hashes")
            if states is None:return blocked_unknown("market-state hash store")
            return passed() if obj["market_state_hash"] in set(states) else fail("Fill market_state_hash cannot be resolved.")
        if rid == "SRF-TIM-007":
            policy=ev.by_id("source_timing_models",obj["source_id"])
            if policy is None:return blocked_unknown("source timing model")
            return passed() if policy.get("allows_receipt_before_known_from",False) or _dt(obj["receipt_time"])>=_dt(obj["known_from"]) else fail("receipt_time precedes known_from under source timing policy.")

        # OOS ---------------------------------------------------------------
        if rid == "SRF-OOS-001":
            camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if camp is None:return blocked_unknown("campaign")
            rg=camp["date_ranges"]["oos"]
            return passed() if obj["oos_start"]==rg["start"] and obj["oos_end"]==rg["end"] else fail("OOS lock interval does not exactly cover campaign OOS interval.")
        if rid == "SRF-OOS-002":
            if obj["access_class"]!="OUTCOME" or obj["decision"]=="DENY":return passed()
            lock=store.resolve_one("EQSOOSLock",obj["oos_lock_id"])
            auth=store.resolve_one("EQSOOSAuthorization",obj["authorization_id"]) if obj.get("authorization_id") else None
            if lock is None or auth is None:return fail("Allowed OOS outcome access lacks lock/authorization.")
            valid=lock["status"] in {"AUTHORIZED","OPEN","CONSUMED"} and auth["authority"]=="EQS-00" and auth["campaign_id"]==obj["campaign_id"]
            return passed() if valid else fail("OOS outcome access occurred without valid EQS-00 authorization.")
        if rid == "SRF-OOS-003":
            cand=store.resolve_one("EQSCandidateSeal",obj["candidate_id"]); camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if cand is None or camp is None:return blocked_unknown("authorized candidate/campaign")
            ok=obj["authority"]=="EQS-00" and obj["candidate_hash"]==cand["candidate_hash"] and cand["campaign_id"]==camp["campaign_id"]
            return passed() if ok else fail("OOS authorization does not match sealed candidate/campaign.")
        if rid == "SRF-OOS-004":
            events=[x for x in store.type_objects("EQSOOSAccessEvent") if x["oos_lock_id"]==obj["oos_lock_id"]]
            events.sort(key=lambda x:(x["accessed_at"],x["access_event_id"]))
            prev=None
            for e in events:
                expected_prev=prev["event_hash"] if prev else None
                if e["previous_event_hash"]!=expected_prev:return fail("OOS access-log hash chain has a gap or fork.")
                if e["event_hash"]!=canonical_sha256(hash_payload(e,"event_hash")):return fail("OOS access event hash does not recompute.")
                prev=e
            return passed()
        if rid == "SRF-OOS-005":
            if obj["status"]!="CONSUMED":return Evaluation("NOT_APPLICABLE","OOS lock is not CONSUMED.")
            auth=store.resolve_one("EQSOOSAuthorization",obj["authorization_id"]) if obj.get("authorization_id") else None
            if auth is None:return blocked_unknown("OOS authorization")
            cand=store.resolve_one("EQSCandidateSeal",auth["candidate_id"])
            if cand is None:return blocked_unknown("sealed candidate")
            return passed() if obj["consumed_candidate_hash"]==auth["candidate_hash"]==cand["candidate_hash"] else fail("Consumed OOS candidate hash differs from authorized sealed candidate.")
        if rid == "SRF-OOS-006":
            hist=ev.by_id("oos_lock_history",obj["oos_lock_id"])
            if hist is None:return blocked_unknown("OOS lock state history")
            seen=False
            for s in hist:
                if s=="COMPROMISED":seen=True
                elif seen and s in {"LOCKED","AUTHORIZED","OPEN","CONSUMED"}:return fail("Compromised OOS lock was relocked/reused as clean.")
            return passed()
        if rid == "SRF-OOS-007":
            if obj["access_class"]!="OUTCOME" or obj["decision"]=="DENY":return passed()
            if not obj.get("candidate_id"):return fail("Candidate-specific OOS outcome access has no candidate binding.")
            cand=store.resolve_one("EQSCandidateSeal",obj["candidate_id"])
            if cand is None:return blocked_unknown("candidate")
            ok=cand["status"] in {"SEALED","OOS_CONSUMED"} and _dt(cand["frozen_at"])<=_dt(obj["accessed_at"])
            return passed() if ok else fail("OOS outcome access occurred before candidate freeze/seal.")

        # Candidate ---------------------------------------------------------
        if rid == "SRF-CAN-001": return passed() if obj["status"]=="SEALED" else fail("Candidate is not SEALED.")
        if rid == "SRF-CAN-002":
            expected=self._canonical_candidate_hash(obj,store)
            if expected is None:return blocked_unknown("candidate dependencies needed for canonical candidate hash")
            return passed() if obj["candidate_hash"]==expected else fail(f"Candidate hash mismatch: expected {expected}")
        if rid == "SRF-CAN-003":
            if obj["status"] not in {"SEALED","OOS_CONSUMED"}:return passed()
            prior=ev.by_id("prior_objects",sid)
            if prior is None:return blocked_unknown("prior sealed candidate snapshot")
            return passed() if _semantic_equal(prior,obj,ignored={"status"}) else fail("Sealed candidate semantics changed after freeze.")
        if rid == "SRF-CAN-004":
            pair=ev.by_id("candidate_open_completion",sid)
            if pair is None:return blocked_unknown("candidate snapshots at OOS open/completion")
            return passed() if pair.get("at_open_hash")==pair.get("at_completion_hash")==obj["candidate_hash"] else fail("Candidate changed after OOS was opened.")
        if rid == "SRF-CAN-005":
            matches=[x for x in store.type_objects("EQSResearchEvidenceManifest") if x["manifest_hash"]==obj["research_evidence_manifest_hash"]]
            return passed() if len(matches)==1 else fail(f"Candidate evidence manifest hash resolves to {len(matches)} manifests.")

        # Stress/reproduction/metrics --------------------------------------
        if rid == "SRF-STR-001":
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if pre is None:return blocked_unknown("preregistration")
            results=[x for x in store.type_objects("EQSStressResult") if x["candidate_id"]==obj["candidate_id"]]
            have={x["stress_type"] for x in results}
            required=set(pre["stress_requirements"])
            return passed() if required<=have else fail(f"Missing required stress evidence: {sorted(required-have)}")
        if rid == "SRF-STR-002":
            cand=store.resolve_one("EQSCandidateSeal",obj["candidate_id"])
            if cand is None:return blocked_unknown("candidate")
            expected=ev.by_id("stress_baseline_hash",obj["candidate_id"])
            if expected is None: expected=cand["candidate_hash"]
            return passed() if obj["baseline_hash"]==expected else fail("Stress baseline hash does not match sealed candidate baseline.")
        if rid == "SRF-STR-003":
            required=ev.by_id("stress_required",obj["stress_run_id"])
            if required is False:return passed()
            return passed() if obj["result"]!="INCONCLUSIVE" else fail("Required stress result is INCONCLUSIVE.")
        if rid == "SRF-REP-001":
            required=ev.by_id("reproduction_required",obj["candidate_id"])
            if required is False:return passed()
            exps=ev.by_id("candidate_qualification_experiments",obj["candidate_id"])
            if exps is None:return blocked_unknown("candidate qualification experiment set")
            records=store.type_objects("EQSReproductionRecord")
            reproduced={x["original_experiment_id"] for x in records}
            return passed() if set(exps)<=reproduced else fail(f"Missing reproduction records: {sorted(set(exps)-reproduced)}")
        if rid == "SRF-REP-002": return passed() if obj["result"]=="PASS" else fail("Reproduction result is not PASS.")
        if rid == "SRF-MET-001":
            md=store.resolve_one("EQSMetricDefinition",obj["metric_id"],obj["metric_version"])
            if md is None:return blocked_unknown("metric definition")
            ok=obj["units"]==md["units"] and obj["calculation_definition_hash"]==md["definition_hash"]
            return passed() if ok else fail("Metric observation definition/version/units do not match registry.")
        if rid == "SRF-MET-002":
            req=ev.by_id("required_metric_observations",obj["metric_observation_id"])
            if req is False:return passed()
            return passed() if obj["status"]=="VALID" else fail("Required metric observation is not VALID.")

        # Handoff -----------------------------------------------------------
        if rid == "SRF-HOF-001":
            cand=store.resolve_one("EQSCandidateSeal",obj["candidate_id"]); lock=store.resolve_one("EQSOOSLock",obj["oos_lock_id"])
            if cand is None or lock is None:return blocked_unknown("candidate/OOS lock")
            ok=obj["candidate_hash"]==cand["candidate_hash"]==lock.get("consumed_candidate_hash")
            return passed() if ok else fail("Handoff candidate differs from OOS-consumed candidate.")
        if rid == "SRF-HOF-002":
            camp=store.resolve_one("EQSResearchCampaign",obj["campaign_id"])
            if camp is None:return blocked_unknown("campaign")
            return passed() if obj["campaign_hash"]==camp["campaign_hash"] else fail("Handoff campaign hash mismatch.")
        if rid == "SRF-HOF-003":
            pre=store.resolve_one("EQSPreregistration",obj["preregistration_id"])
            if pre is None:return blocked_unknown("preregistration")
            return passed() if obj["preregistration_hash"]==pre["canonical_payload_hash"] else fail("Handoff preregistration hash mismatch.")
        if rid == "SRF-HOF-004":
            evidence_map=ev.get("artifact_hashes")
            if evidence_map is None:return blocked_unknown("sealed artifact hash store")
            return passed() if evidence_map.get(obj["oos_evidence_id"])==obj["oos_evidence_hash"] else fail("Sealed OOS evidence cannot be resolved by exact hash.")
        if rid == "SRF-HOF-005":
            ledgers=[x for x in store.type_objects("EQSResearchSearchLedger") if x["campaign_id"]==obj["campaign_id"]]
            return passed() if len(ledgers)==1 and ledgers[0]["ledger_hash"]==obj["research_search_ledger_hash"] else fail("Handoff search-ledger hash mismatch.")
        if rid == "SRF-HOF-006":
            expected=ev.by_id("canonical_stress_evidence_hash",obj["candidate_id"])
            if expected is None:return blocked_unknown("canonical stress evidence hash")
            return passed() if obj["stress_evidence_hash"]==expected else fail("Handoff stress evidence hash mismatch.")
        if rid == "SRF-HOF-007":
            if obj["qualification_status"]!="VALID_RESEARCH_HANDOFF":return passed()
            pre_report=ev.by_id("pre_handoff_aggregate",obj["handoff_id"])
            if pre_report is None:return blocked_unknown("pre-handoff qualification aggregate")
            return passed() if pre_report=="PASS" else fail("Handoff claims VALID_RESEARCH_HANDOFF while qualification aggregate is not PASS.")
        if rid == "SRF-HOF-008":
            forbidden={"paper_authorized","live_authorized","promotion_authority","programme_gate_decision"}
            return fail("Research handoff contains programme-promotion authority fields.") if _contains_any_key(obj,forbidden) else passed()

        raise NotImplementedError(f"No evaluator registered for {rid}")

    # --------------------------- internals --------------------------------

    def _subjects_for_rule(self, rule, store, references, required_states, root_type, root_id):
        scope=rule["scope"]
        if scope=="ANY": return [(o.get("object_type","UNKNOWN"),store.id_of(o),o) for o in store.objects]
        if scope=="HASHED_OBJECT": return [(o.get("object_type","UNKNOWN"),store.id_of(o),o) for o in store.objects if o.get("object_type") in OWN_HASH_FIELDS]
        if scope=="REFERENCE": return [("REFERENCE",r.synthetic_id,r) for r in references]
        if scope=="REQUIRED_STATE": return required_states
        return [(scope,store.id_of(o),o) for o in store.type_objects(scope)]

    def _schema_ok(self,obj):
        return not any(self.schema_validator.iter_errors(obj))

    def _supported_versions(self):
        versions=set()
        for v in self.schema_bundle.get("$defs",{}).values():
            if isinstance(v,dict):
                p=v.get("properties",{}).get("schema_version",{})
                if "const" in p:versions.add(p["const"])
        return versions

    def _assert_generated_schema(self,obj):
        errors=list(self.schema_validator.iter_errors(obj))
        if errors:
            raise ValueError("Generated validator output violates EQS schema: " + "; ".join(e.message for e in errors[:5]))

    def _make_result(self,rule,subject,evaluation,timestamp):
        typ,sid,_=subject
        raw=f"{rule['rule_id']}|{typ}|{sid}|{evaluation.outcome}|{timestamp}"
        rid="VR-"+hashlib.sha256(raw.encode()).hexdigest()[:24]
        return {
            "object_type":"EQSValidationResult",
            "schema_version":"EQS-VALIDATION-RESULT-1.0",
            "validation_result_id":rid,
            "rule_id":rule["rule_id"],
            "subject_type":typ,
            "subject_id":sid,
            "evaluated_at":timestamp,
            "outcome":evaluation.outcome,
            "error_code":evaluation.error_code if evaluation.outcome not in {"PASS","NOT_APPLICABLE"} else None,
            "message":evaluation.message,
            "evidence":evaluation.evidence,
        }

    def _aggregate(self,results):
        worst="PASS"
        for r in results:
            out=r["outcome"]
            if PRECEDENCE.get(out,0)>PRECEDENCE[worst]:worst=out
        return worst

    def _extract_references(self,store:ObjectStore)->list[ReferenceSpec]:
        refs=[]
        def add(o,path,t,oid,version=None,h=None):
            if oid is not None:refs.append(ReferenceSpec(o["object_type"],store.id_of(o),path,t,str(oid),version,h))
        for o in store.objects:
            t=o.get("object_type")
            if t=="EQSResearchCampaign":
                add(o,"universe.universe_definition_id","EQSUniverseDefinition",o["universe"]["universe_definition_id"],None,o["universe"]["universe_definition_hash"])
                add(o,"execution_model_id","EQSExecutionModelDefinition",o["execution_model_id"]); add(o,"cost_model_id","EQSCostModel",o["cost_model_id"]); add(o,"cross_validation_spec_id","EQSCrossValidationSpec",o["cross_validation_spec_id"])
            elif t=="EQSPreregistration":
                add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"]); add(o,"cross_validation_spec_id","EQSCrossValidationSpec",o["cross_validation_spec_id"])
                for i,x in enumerate(o["feature_sets"]):add(o,f"feature_sets[{i}]","EQSFeatureDefinition",x["feature_id"],x["feature_version"],x["definition_hash"])
                for i,x in enumerate(o["labels"]):add(o,f"labels[{i}]","EQSLabelDefinition",x["label_id"],x["label_version"],x["definition_hash"])
            elif t=="EQSExperiment":
                add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"]); add(o,"preregistration_id","EQSPreregistration",o["preregistration_id"]); add(o,"strategy_definition_id","EQSStrategyDefinition",o["strategy_definition_id"],o["strategy_definition_version"]); add(o,"code_binding_id","EQSCodeBinding",o["code_binding_id"]); add(o,"data_binding_id","EQSDataBinding",o["data_binding_id"]); add(o,"environment_binding_id","EQSEnvironmentBinding",o["environment_binding_id"])
                for i,x in enumerate(o["feature_bindings"]):add(o,f"feature_bindings[{i}]","EQSFeatureDefinition",x["definition_id"],x["version"],x["definition_hash"])
                for i,x in enumerate(o["label_bindings"]):add(o,f"label_bindings[{i}]","EQSLabelDefinition",x["definition_id"],x["version"],x["definition_hash"])
            elif t=="EQSCandidateSeal":
                add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"]); add(o,"preregistration_id","EQSPreregistration",o["preregistration_id"]); add(o,"strategy_definition_id","EQSStrategyDefinition",o["strategy_definition_id"]); add(o,"code_binding_id","EQSCodeBinding",o["code_binding_id"]); add(o,"data_binding_id","EQSDataBinding",o["data_binding_id"]); add(o,"environment_binding_id","EQSEnvironmentBinding",o["environment_binding_id"])
                for i,x in enumerate(o["feature_bindings"]):add(o,f"feature_bindings[{i}]","EQSFeatureDefinition",x["definition_id"],x["version"],x["definition_hash"])
                for i,x in enumerate(o["label_bindings"]):add(o,f"label_bindings[{i}]","EQSLabelDefinition",x["definition_id"],x["version"],x["definition_hash"])
            elif t=="EQSCrossValidationSpec": add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"])
            elif t=="EQSOOSAuthorization": add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"]); add(o,"candidate_id","EQSCandidateSeal",o["candidate_id"],None,o["candidate_hash"])
            elif t=="EQSOOSLock":
                add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"])
                if o.get("authorization_id"):add(o,"authorization_id","EQSOOSAuthorization",o["authorization_id"])
            elif t=="EQSOOSAccessEvent":
                add(o,"oos_lock_id","EQSOOSLock",o["oos_lock_id"]); add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"])
                if o.get("candidate_id"):add(o,"candidate_id","EQSCandidateSeal",o["candidate_id"],None,o.get("candidate_hash"))
                if o.get("authorization_id"):add(o,"authorization_id","EQSOOSAuthorization",o["authorization_id"])
            elif t=="EQSDecision": add(o,"experiment_id","EQSExperiment",o["experiment_id"])
            elif t=="EQSOrder": add(o,"decision_id","EQSDecision",o["decision_id"]); add(o,"execution_model_id","EQSExecutionModelDefinition",o["execution_model_id"])
            elif t=="EQSFill": add(o,"order_id","EQSOrder",o["order_id"])
            elif t=="EQSStressResult": add(o,"candidate_id","EQSCandidateSeal",o["candidate_id"])
            elif t=="EQSReproductionRecord": add(o,"original_experiment_id","EQSExperiment",o["original_experiment_id"]); add(o,"reproduction_experiment_id","EQSExperiment",o["reproduction_experiment_id"])
            elif t=="EQSMetricObservation": add(o,"metric_id","EQSMetricDefinition",o["metric_id"],o["metric_version"])
            elif t=="EQSResearchPaperHandoff":
                add(o,"candidate_id","EQSCandidateSeal",o["candidate_id"],None,o["candidate_hash"]); add(o,"campaign_id","EQSResearchCampaign",o["campaign_id"],None,o["campaign_hash"]); add(o,"preregistration_id","EQSPreregistration",o["preregistration_id"],None,o["preregistration_hash"]); add(o,"data_binding_id","EQSDataBinding",o["data_binding_id"]); add(o,"code_binding_id","EQSCodeBinding",o["code_binding_id"]); add(o,"environment_binding_id","EQSEnvironmentBinding",o["environment_binding_id"]); add(o,"oos_lock_id","EQSOOSLock",o["oos_lock_id"]); add(o,"execution_model_id","EQSExecutionModelDefinition",o["execution_model_id"]); add(o,"cost_model_id","EQSCostModel",o["cost_model_id"])
        return refs

    def _extract_required_states(self,store,ev):
        out=[]
        for o in store.objects:
            t=o.get("object_type"); sid=store.id_of(o)
            if t=="EQSDataBinding":
                out.append(("REQUIRED_STATE",f"STATE-{sid}-PIT",o.get("pit_certification_status")))
            elif t=="EQSMetricObservation":
                out.append(("REQUIRED_STATE",f"STATE-{sid}-METRIC",o.get("status")))
            elif t=="EQSStressResult":
                out.append(("REQUIRED_STATE",f"STATE-{sid}-STRESS",o.get("result")))
            elif t=="EQSUniverseDefinition":
                # V1's sealed 89-rule catalogue does not assign a dedicated
                # survivor-bias rule ID. Preserve the catalogue unchanged but
                # fail closed through SRF-GEN-006 unless an external universe
                # certification explicitly proves point-in-time membership.
                state=ev.by_id("universe_survivor_bias_status",sid)
                if state is None:
                    state="UNVERIFIED"
                out.append(("REQUIRED_STATE",f"STATE-{sid}-SURVIVOR-BIAS",state))
        return out

    def _canonical_candidate_hash(self,cand,store):
        dep_types_fields=[("EQSResearchCampaign","campaign_id"),("EQSPreregistration","preregistration_id"),("EQSStrategyDefinition","strategy_definition_id"),("EQSCodeBinding","code_binding_id"),("EQSDataBinding","data_binding_id"),("EQSEnvironmentBinding","environment_binding_id")]
        deps=[]
        for typ,f in dep_types_fields:
            x=store.resolve_one(typ,cand[f])
            if x is None:return None
            deps.append([typ,store.id_of(x),x.get(TYPE_HASH_FIELDS.get(typ,""))])
        payload={
            "campaign_id":cand["campaign_id"],"preregistration_id":cand["preregistration_id"],"strategy_definition_id":cand["strategy_definition_id"],
            "code_binding_id":cand["code_binding_id"],"data_binding_id":cand["data_binding_id"],"environment_binding_id":cand["environment_binding_id"],
            "feature_bindings":cand["feature_bindings"],"label_bindings":cand["label_bindings"],"parameter_hash":cand["parameter_hash"],
            "universe_definition_hash":cand["universe_definition_hash"],"allocation_logic_hash":cand["allocation_logic_hash"],"execution_model_hash":cand["execution_model_hash"],"cost_model_hash":cand["cost_model_hash"],"research_evidence_manifest_hash":cand["research_evidence_manifest_hash"],
            "resolved_dependency_hashes":deps,
        }
        return canonical_sha256(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def _dt(value):
    if isinstance(value,datetime):return value
    if value is None:return None
    return datetime.fromisoformat(str(value).replace("Z","+00:00"))

def _semantic_equal(a,b,ignored:set[str]|None=None):
    ignored=ignored or set()
    aa={k:v for k,v in a.items() if k not in ignored and k not in OWN_HASH_FIELDS.get(a.get("object_type"),())}
    bb={k:v for k,v in b.items() if k not in ignored and k not in OWN_HASH_FIELDS.get(b.get("object_type"),())}
    return canonical_json_bytes(aa)==canonical_json_bytes(bb)

def _ranges_overlap(a,b):
    return max(_dt(a["start"]),_dt(b["start"])) < min(_dt(a["end"]),_dt(b["end"]))

def _duration_seconds(value):
    if value is None:return 0.0
    m=re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",value)
    if not m:raise ValueError(f"unsupported ISO-8601 duration: {value}")
    y,mo,d,h,mi,s=m.groups()
    # Research purge/embargo durations should normally use D/H/M/S. Years/months
    # are deliberately rejected because they are calendar-dependent.
    if y or mo:raise ValueError("calendar years/months are not deterministic duration seconds")
    return int(d or 0)*86400+int(h or 0)*3600+int(mi or 0)*60+float(s or 0)

def _parameters_within(params,permitted,ranges):
    if permitted and any(k not in permitted for k in params):return False
    for k,v in params.items():
        spec=ranges.get(k)
        if spec is None:continue
        if isinstance(spec,dict):
            if "min" in spec and v<spec["min"]:return False
            if "max" in spec and v>spec["max"]:return False
            if "enum" in spec and v not in spec["enum"]:return False
        elif isinstance(spec,list) and v not in spec:return False
    return True

def _recompute_search_counts(exps,ev,campaign_id):
    statuses=[x["result_status"] for x in exps]
    counts={
        "experiments":len(exps),"passed":statuses.count("PASS"),"failed":statuses.count("FAIL"),"invalid":statuses.count("INVALID"),"blocked":statuses.count("BLOCKED"),
        "parameter_combinations":len({x["parameter_hash"] for x in exps}),
        "distinct_feature_sets":len({canonical_sha256(x["feature_bindings"]) for x in exps}),
        "distinct_labels":len({b["definition_id"] for x in exps for b in x["label_bindings"]}),
        "strategy_variants":len({(x["strategy_definition_id"],x["strategy_definition_version"]) for x in exps}),
        "manual_interventions":int(ev.by_id("manual_interventions",campaign_id) or 0),
        "post_hoc_analyses":int(ev.by_id("post_hoc_analyses",campaign_id) or 0),
    }
    classes={k:0 for k in ["PREREGISTERED","EXPLORATORY","DIAGNOSTIC","CONFIRMATORY","OOS"]}
    for x in exps:classes[x["experiment_class"]]+=1
    return counts,classes

def _contains_any_key(value,keys):
    if isinstance(value,dict):
        if set(value)&keys:return True
        return any(_contains_any_key(v,keys) for v in value.values())
    if isinstance(value,list):return any(_contains_any_key(v,keys) for v in value)
    return False
