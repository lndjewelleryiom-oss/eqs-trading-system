from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
import copy
import math

from .journal import ImmutableLifecycleJournal, JournalSeal
from .policy_resolver import AllocationPolicyResolver, PolicyResolutionError
from .policy_pinning import PolicyGenerationPinError, build_policy_generation_pin
from .reservation_policy_binding import validate_capital_risk_policy_binding
from .reservation_lease import ReservationLeaseError, ReservationLeaseRegistry
from .allocator import (
    AllocationContractError,
    AllocationRegistry,
    FIXED_RISK_BUDGET_METHOD_VERSION,
)
from .validator import (
    AUTHORIZED_QUALIFICATION_STATES,
    SemanticValidator,
    canonical_json,
    hash_without,
    parse_ts,
    sha256_obj,
    target_set_hash,
)

RUNTIME_VERSION = "1.0.0"


class RuntimeContractError(ValueError):
    pass


def _stable_id(prefix: str, obj: Any, length: int = 20) -> str:
    return f"{prefix}-{sha256_obj(obj)[:length]}"


def _direction_sign(direction: str) -> float:
    if direction == "LONG":
        return 1.0
    if direction == "SHORT":
        return -1.0
    if direction == "FLAT":
        return 0.0
    raise RuntimeContractError(f"unsupported direction: {direction}")


def _copy(obj: Any) -> Any:
    return copy.deepcopy(obj)


@dataclass(frozen=True)
class RuntimePreparation:
    runtime_version: str
    run_id: str
    status: str
    request_sha256: str
    intents: Tuple[Dict[str, Any], ...]
    targets: Tuple[Dict[str, Any], ...]
    reservation_request: Optional[Dict[str, Any]]
    certification_context: Dict[str, Any]
    blocking_reasons: Tuple[str, ...]
    journal: ImmutableLifecycleJournal
    preparation_digest_sha256: str
    policy_generation_pin: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime_version": self.runtime_version,
            "run_id": self.run_id,
            "status": self.status,
            "request_sha256": self.request_sha256,
            "intents": [_copy(x) for x in self.intents],
            "targets": [_copy(x) for x in self.targets],
            "reservation_request": _copy(self.reservation_request),
            "certification_context": _copy(self.certification_context),
            "blocking_reasons": list(self.blocking_reasons),
            "journal": self.journal.to_dict(),
            "preparation_digest_sha256": self.preparation_digest_sha256,
            "policy_generation_pin": _copy(self.policy_generation_pin),
        }


@dataclass(frozen=True)
class RuntimeResult:
    runtime_version: str
    run_id: str
    status: str
    request_sha256: str
    preparation_digest_sha256: str
    targets: Tuple[Dict[str, Any], ...]
    reservation_request: Optional[Dict[str, Any]]
    capital_risk_response: Optional[Dict[str, Any]]
    execution_handoff: Optional[Dict[str, Any]]
    validation_report: Optional[Dict[str, Any]]
    blocking_reasons: Tuple[str, ...]
    journal: ImmutableLifecycleJournal
    journal_seal: JournalSeal
    semantic_replay_digest_sha256: str
    policy_generation_pin: Optional[Dict[str, Any]] = None
    reservation_lease_lifecycle: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime_version": self.runtime_version,
            "run_id": self.run_id,
            "status": self.status,
            "request_sha256": self.request_sha256,
            "preparation_digest_sha256": self.preparation_digest_sha256,
            "targets": [_copy(x) for x in self.targets],
            "reservation_request": _copy(self.reservation_request),
            "capital_risk_response": _copy(self.capital_risk_response),
            "execution_handoff": _copy(self.execution_handoff),
            "validation_report": _copy(self.validation_report),
            "blocking_reasons": list(self.blocking_reasons),
            "journal": self.journal.to_dict(),
            "journal_seal": self.journal_seal.to_dict(),
            "semantic_replay_digest_sha256": self.semantic_replay_digest_sha256,
            "policy_generation_pin": _copy(self.policy_generation_pin),
            "reservation_lease_lifecycle": _copy(self.reservation_lease_lifecycle),
        }


class PortfolioConstructionRuntime:
    """Canonical deterministic EQS-PC-V1.0.0 orchestration runtime.

    The runtime constructs portfolio proposals but never manufactures Capital & Risk authority.
    A caller must supply the authoritative CapitalRiskResponse to `finalize`, or provide an
    external responder to `execute`.
    """

    def __init__(self, schema_dir: str, reservation_lease_registry: Optional[ReservationLeaseRegistry] = None):
        self.schema_dir = str(schema_dir)
        self.validator = SemanticValidator(schema_dir)
        self.allocators = AllocationRegistry()
        self.policy_resolver = AllocationPolicyResolver(schema_dir)
        self.reservation_leases = reservation_lease_registry or ReservationLeaseRegistry()

    def reset_reservation_leases(self) -> None:
        self.reservation_leases.reset()

    def reservation_lease_snapshot(self) -> Dict[str, Any]:
        return self.reservation_leases.snapshot()

    def cancel_reservation(self, reservation_id: str, cancelled_at: str, reason: str) -> Dict[str, Any]:
        return self.reservation_leases.cancel(reservation_id, cancelled_at, reason).to_dict()

    def expire_reservations(self, at: str) -> List[str]:
        return self.reservation_leases.sweep_expired(at)

    def register_reservation_lease(self, preparation: RuntimePreparation, capital_risk_response: Dict[str, Any]) -> Dict[str, Any]:
        """Register an approved Capital & Risk reservation before final execution.

        This creates the live lease authority without creating an ExecutionHandoff, allowing a
        separately re-approved bounded renewal to occur before finalize().
        """
        response = _copy(capital_risk_response)
        if preparation.status != "RISK_PENDING" or preparation.reservation_request is None:
            raise RuntimeContractError("reservation lease registration requires a RISK_PENDING preparation")
        schema_errors = self.validator.schemas.validate("capital_risk_response.schema.json", response)
        if schema_errors:
            raise RuntimeContractError("invalid Capital & Risk response: " + "; ".join(schema_errors))
        if response.get("decision") not in {"APPROVED", "APPROVED_WITH_REDUCTIONS"}:
            raise RuntimeContractError("only an approved Capital & Risk response can issue a reservation lease")
        if response.get("reservation_request_id") != preparation.reservation_request.get("reservation_request_id"):
            raise RuntimeContractError("Capital & Risk response request id mismatch")
        binding_errors = validate_capital_risk_policy_binding(preparation.reservation_request, response)
        if binding_errors:
            raise RuntimeContractError("Capital & Risk policy binding invalid: " + "; ".join(binding_errors))
        life = self.reservation_leases.issue(
            reservation_id=response["reservation_id"],
            reservation_request_id=response["reservation_request_id"],
            portfolio_run_id=preparation.run_id,
            issued_at=response["decided_at"],
            expires_at=response["reservation_expiry"],
            approved_target_set_hash=response["approved_target_set_hash"],
            policy_generation_pin_sha256=response.get("policy_generation_pin_sha256"),
        )
        return life.to_dict()

    def renew_reservation(self, reservation_id: str, renewal_approval: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one explicit sealed Capital & Risk lease re-approval."""
        schema_errors = self.validator.schemas.validate("reservation_lease_renewal_approval.schema.json", renewal_approval)
        if schema_errors:
            raise RuntimeContractError("reservation renewal approval schema invalid: " + "; ".join(schema_errors))
        return self.reservation_leases.renew(reservation_id, _copy(renewal_approval)).to_dict()

    def _validate_runtime_request(self, request: Dict[str, Any]) -> List[str]:
        errors = self.validator.schemas.validate("runtime_request.schema.json", request)
        if errors:
            return ["RUNTIME_REQUEST_SCHEMA_INVALID: " + e for e in errors]
        try:
            canonical_json(request)
        except Exception as exc:
            return [f"RUNTIME_REQUEST_NONCANONICAL: {exc}"]
        return []

    def _admission_errors(self, request: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        decision_time = parse_ts(request["decision_time"])
        evidence = request["qualification_evidence"]
        mappings = request["exposure_mappings"]
        timeline = request["timeline"]
        try:
            ordered_times = [
                parse_ts(timeline["run_started_at"]), parse_ts(request["decision_time"]),
                parse_ts(timeline["proposal_validated_at"]), parse_ts(timeline["reservation_requested_at"]),
                parse_ts(timeline["handoff_created_at"]), parse_ts(timeline["final_validated_at"]),
                parse_ts(timeline["sealed_at"]),
            ]
            if any(b < a for a,b in zip(ordered_times, ordered_times[1:])):
                errors.append("RUNTIME_TIMELINE_NON_MONOTONIC")
        except Exception as exc:
            errors.append(f"RUNTIME_TIMELINE_INVALID:{exc}")
        for key, mapping in mappings.items():
            if mapping.get("mapping_ref") != key:
                errors.append(f"MAPPING_IDENTITY_MISMATCH:{key}:{mapping.get('mapping_ref')}")
            primaries = [c for c in mapping.get("factor_components",[]) if c.get("factor_id") == mapping.get("primary_factor_id")]
            if len(primaries) != 1:
                errors.append(f"PRIMARY_FACTOR_COMPONENT_COUNT:{key}:{len(primaries)}")
            elif primaries[0].get("factor_class") != mapping.get("primary_factor_class"):
                errors.append(f"PRIMARY_FACTOR_CLASS_MISMATCH:{key}")
            netkey = mapping.get("economic_netting_key")
            if netkey not in request["cost_estimate_ids"]:
                errors.append(f"COST_ESTIMATE_ID_MISSING:{netkey}")
            if netkey not in request["liquidity_assessment_ids"]:
                errors.append(f"LIQUIDITY_ASSESSMENT_ID_MISSING:{netkey}")
        seen = set()
        for i, intent in enumerate(request["intents"]):
            schema_errors = self.validator.schemas.validate("strategy_intent.schema.json", intent)
            errors.extend(f"INTENT_SCHEMA[{i}]: {x}" for x in schema_errors)
            iid = intent.get("intent_id")
            if iid in seen:
                errors.append(f"DUPLICATE_INTENT_ID:{iid}")
            seen.add(iid)
            if intent.get("qualification_state") not in AUTHORIZED_QUALIFICATION_STATES:
                errors.append(f"QUALIFICATION_NOT_AUTHORISED:{iid}")
            evid = intent.get("qualification_evidence_id")
            if evid not in evidence:
                errors.append(f"QUALIFICATION_EVIDENCE_MISSING:{iid}:{evid}")
            else:
                if sha256_obj(evidence[evid]) != intent.get("qualification_snapshot_hash"):
                    errors.append(f"QUALIFICATION_HASH_MISMATCH:{iid}")
                if evidence[evid].get("strategy_id") != intent.get("strategy_id") or evidence[evid].get("strategy_version") != intent.get("strategy_version"):
                    errors.append(f"QUALIFICATION_VERSION_MISMATCH:{iid}")
            try:
                if parse_ts(intent["knowledge_time"]) > decision_time:
                    errors.append(f"PIT_VIOLATION:{iid}")
            except Exception:
                errors.append(f"PIT_TIMESTAMP_INVALID:{iid}")
            mref = intent.get("exposure_mapping_ref")
            if mref not in mappings:
                errors.append(f"EXPOSURE_MAPPING_MISSING:{iid}:{mref}")
            if intent.get("desired_quantity") is None:
                errors.append(f"UNSUPPORTED_INTENT_BASIS:{iid}:desired_quantity_required_v1")

        method = request.get("allocation_method_version")
        policy = request.get("allocation_policy")
        envelope = request.get("allocation_policy_envelope")
        authority_evidence = request.get("policy_authority_evidence")
        authority_snapshot = request.get("policy_authority_snapshot")
        lifecycle_events = request.get("policy_lifecycle_events")
        if method == "NETTING_ONLY@1.0.0":
            if any(x is not None for x in (policy, envelope, authority_evidence, authority_snapshot, lifecycle_events)):
                errors.append("NETTING_ONLY_ALLOCATION_POLICY_NOT_ALLOWED")
        elif method == FIXED_RISK_BUDGET_METHOD_VERSION:
            binding_present = any(x is not None for x in (envelope, authority_evidence, authority_snapshot, lifecycle_events))
            if binding_present:
                if policy is not None:
                    errors.append("AUTHORITATIVE_POLICY_BINDING_MUST_NOT_INCLUDE_DIRECT_POLICY")
                if envelope is None or authority_evidence is None or authority_snapshot is None or lifecycle_events is None:
                    errors.append("AUTHORITATIVE_POLICY_BINDING_INCOMPLETE")
                else:
                    try:
                        resolved = self.policy_resolver.resolve_with_lifecycle(
                            decision_time=request["decision_time"], envelope=envelope,
                            authority_evidence=authority_evidence, authority_snapshot=authority_snapshot,
                            lifecycle_events=lifecycle_events,
                        )
                        if request.get("policy_snapshot_hash") != resolved.policy_payload_sha256:
                            errors.append("AUTHORITATIVE_POLICY_PAYLOAD_HASH_MISMATCH")
                        expected_ref = f"{resolved.policy_id}@{resolved.policy_version}"
                        if request.get("policy_ref") != expected_ref:
                            errors.append("AUTHORITATIVE_POLICY_REF_MISMATCH")
                    except Exception as exc:
                        errors.append(f"POLICY_RESOLUTION_FAILED:{type(exc).__name__}:{exc}")
            elif policy is None:
                errors.append("FIXED_RISK_BUDGET_POLICY_MISSING")
            else:
                for e in self.validator.schemas.validate("fixed_risk_budget_policy.schema.json", policy):
                    errors.append(f"FIXED_RISK_BUDGET_POLICY_SCHEMA:{e}")
                if policy.get("policy_mode") == "PRODUCTION":
                    errors.append("PRODUCTION_POLICY_REQUIRES_AUTHORITY_BINDING")
                try:
                    policy_hash = sha256_obj(policy)
                    if request.get("policy_snapshot_hash") != policy_hash:
                        errors.append("FIXED_RISK_BUDGET_POLICY_HASH_MISMATCH")
                except Exception as exc:
                    errors.append(f"FIXED_RISK_BUDGET_POLICY_NONCANONICAL:{exc}")
        else:
            errors.append(f"UNSUPPORTED_ALLOCATION_METHOD:{method}")
        return sorted(set(errors))

    def _map_intents(self, request: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        mapped: List[Dict[str, Any]] = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for intent in request["intents"]:
            mapping = request["exposure_mappings"][intent["exposure_mapping_ref"]]
            if mapping["source_instrument_id"] != intent["instrument_id"]:
                raise RuntimeContractError(f"mapping source instrument mismatch for {intent['intent_id']}")
            sign = _direction_sign(intent["direction"])
            qty = float(intent["desired_quantity"] or 0.0)
            factor_values = []
            for component in mapping["factor_components"]:
                factor_values.append({
                    "factor_id": component["factor_id"],
                    "factor_class": component["factor_class"],
                    "signed_exposure": sign * qty * float(component["exposure_per_source_quantity"]),
                    "exposure_unit": component["exposure_unit"],
                    "reporting_currency": component.get("reporting_currency"),
                    "normalisation_method": component["normalisation_method"],
                    "normalisation_version": component["normalisation_version"],
                })
            primary = next((x for x in factor_values if x["factor_id"] == mapping["primary_factor_id"]), None)
            if primary is None:
                raise RuntimeContractError(f"mapping {mapping['mapping_ref']} lacks primary factor component")
            row = {
                "intent_id": intent["intent_id"],
                "strategy_id": intent["strategy_id"],
                "campaign_id": intent["campaign_id"],
                "strategy_version": intent["strategy_version"],
                "source_instrument_id": intent["instrument_id"],
                "source_underlying_id": intent.get("underlying_id"),
                "economic_netting_key": mapping["economic_netting_key"],
                "target_instrument_id": mapping["target_instrument_id"],
                "target_underlying_id": mapping.get("target_underlying_id"),
                "primary_factor_id": mapping["primary_factor_id"],
                "primary_factor_class": mapping["primary_factor_class"],
                "primary_signed_exposure": primary["signed_exposure"],
                "factor_values": factor_values,
                "source_quantity": sign * qty,
                "mapping_ref": mapping["mapping_ref"],
            }
            mapped.append(row)
            groups.setdefault(mapping["economic_netting_key"], []).append(row)
        mapped.sort(key=lambda x: (x["economic_netting_key"], x["strategy_id"], x["intent_id"]))
        return mapped, groups

    def _resolve_allocation_policy(self, request: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        method = request["allocation_method_version"]
        if method == "NETTING_ONLY@1.0.0":
            return None, None
        if method != FIXED_RISK_BUDGET_METHOD_VERSION:
            raise AllocationContractError(f"unsupported allocation method: {method}")
        envelope = request.get("allocation_policy_envelope")
        authority_evidence = request.get("policy_authority_evidence")
        authority_snapshot = request.get("policy_authority_snapshot")
        lifecycle_events = request.get("policy_lifecycle_events")
        if any(x is not None for x in (envelope, authority_evidence, authority_snapshot, lifecycle_events)):
            if request.get("allocation_policy") is not None:
                raise PolicyResolutionError("authoritative binding cannot be combined with direct allocation_policy")
            if envelope is None or authority_evidence is None or authority_snapshot is None or lifecycle_events is None:
                raise PolicyResolutionError("authoritative policy binding is incomplete")
            binding = self.policy_resolver.resolve_with_lifecycle(
                decision_time=request["decision_time"], envelope=envelope,
                authority_evidence=authority_evidence, authority_snapshot=authority_snapshot,
                lifecycle_events=lifecycle_events,
            )
            if binding.policy_payload_sha256 != request["policy_snapshot_hash"]:
                raise PolicyResolutionError("resolved policy payload hash does not match runtime policy_snapshot_hash")
            if request.get("policy_ref") != f"{binding.policy_id}@{binding.policy_version}":
                raise PolicyResolutionError("runtime policy_ref does not identify resolved authoritative policy version")
            return _copy(binding.resolved_policy), binding.to_dict()
        policy = request.get("allocation_policy")
        if policy is None:
            raise AllocationContractError(f"allocation policy missing for {method}")
        if policy.get("policy_mode") == "PRODUCTION":
            raise PolicyResolutionError("direct production allocation policy is prohibited; authoritative binding required")
        return _copy(policy), None

    def _allocate_mapped_rows(
        self, request: Dict[str, Any], mapped: List[Dict[str, Any]], resolved_policy: Optional[Dict[str, Any]]=None
    ) -> tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
        method = request["allocation_method_version"]
        if method == "NETTING_ONLY@1.0.0":
            rows = [_copy(x) for x in mapped]
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for row in rows:
                groups.setdefault(row["economic_netting_key"], []).append(row)
            return rows, groups, None

        allocator = self.allocators.resolve(method)
        policy = resolved_policy
        if policy is None:
            policy, _binding = self._resolve_allocation_policy(request)
        if policy is None:
            raise AllocationContractError(f"allocation policy missing for {method}")
        result = allocator.allocate(
            run_id=request["run_id"],
            decision_time=request["decision_time"],
            mapped_rows=mapped,
            policy=policy,
        )
        result_dict = result.to_dict()
        schema_errors = self.validator.schemas.validate("allocation_result.schema.json", result_dict)
        if schema_errors:
            raise AllocationContractError("allocation result schema invalid: " + "; ".join(schema_errors))
        if result_dict["policy_sha256"] != request["policy_snapshot_hash"]:
            raise AllocationContractError("allocation result policy hash does not match runtime policy snapshot")
        rows = [_copy(x) for x in result.allocated_rows]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["economic_netting_key"], []).append(row)
        return rows, groups, result_dict

    @staticmethod
    def _netted_allocations(contributions: List[float]) -> List[float]:
        positive = sum(x for x in contributions if x > 0)
        negative = -sum(x for x in contributions if x < 0)
        matched = min(positive, negative)
        out: List[float] = []
        for x in contributions:
            if x > 0 and positive > 0:
                out.append(matched * (x / positive))
            elif x < 0 and negative > 0:
                out.append(-matched * ((-x) / negative))
            else:
                out.append(0.0)
        return out

    def _build_targets(self, request: Dict[str, Any], mapped: List[Dict[str, Any]], groups: Dict[str, List[Dict[str, Any]]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        targets: List[Dict[str, Any]] = []
        assertions: List[Dict[str, Any]] = []
        mappings = request["exposure_mappings"]
        for key in sorted(groups):
            rows = groups[key]
            group_maps = [mappings[x["mapping_ref"]] for x in rows]
            signature = {
                (
                    m["target_instrument_id"], m.get("target_underlying_id"), m["target_instrument_type"],
                    m["primary_factor_id"], m["primary_factor_class"], float(m["factor_exposure_per_target_quantity"]),
                    m.get("target_notional_per_quantity"), m["reporting_currency"], m.get("base_currency"),
                    bool(m.get("requires_currency_factor", False)), tuple(m.get("required_greeks", []))
                )
                for m in group_maps
            }
            if len(signature) != 1:
                raise RuntimeContractError(f"incompatible target definitions in netting group {key}")
            target_instrument, target_underlying, target_instrument_type, primary_factor_id, primary_factor_class, per_target_qty, target_notional_per_quantity, reporting_currency, base_currency, requires_currency_factor, required_greeks = next(iter(signature))
            if math.isclose(per_target_qty, 0.0, abs_tol=0.0):
                raise RuntimeContractError(f"zero factor_exposure_per_target_quantity in group {key}")
            primary_contribs = [float(x["primary_signed_exposure"]) for x in rows]
            net_primary = sum(primary_contribs)
            gross_primary = sum(abs(x) for x in primary_contribs)
            internal_netted = self._netted_allocations(primary_contribs)

            factor_agg: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                for f in row["factor_values"]:
                    existing = factor_agg.get(f["factor_id"])
                    descriptor = {k: f[k] for k in ["factor_class","exposure_unit","reporting_currency","normalisation_method","normalisation_version"]}
                    if existing is None:
                        factor_agg[f["factor_id"]] = {**descriptor, "exposure_value": float(f["signed_exposure"])}
                    else:
                        if any(existing[k] != descriptor[k] for k in descriptor):
                            raise RuntimeContractError(f"factor descriptor mismatch for {f['factor_id']} in {key}")
                        existing["exposure_value"] += float(f["signed_exposure"])

            factor_vectors: List[Dict[str, Any]] = []
            max_knowledge = max(parse_ts(x["knowledge_time"]) for x in request["intents"] if x["intent_id"] in {r["intent_id"] for r in rows})
            max_knowledge_text = max_knowledge.isoformat().replace("+00:00","Z")
            for factor_id in sorted(factor_agg):
                f = factor_agg[factor_id]
                factor_vectors.append({
                    "factor_id": factor_id,
                    "factor_class": f["factor_class"],
                    "exposure_value": f["exposure_value"],
                    "exposure_unit": f["exposure_unit"],
                    "reporting_currency": f["reporting_currency"],
                    "normalisation_method": f["normalisation_method"],
                    "normalisation_version": f["normalisation_version"],
                    "effective_time": request["decision_time"],
                    "knowledge_time": max_knowledge_text,
                    "source_ref": f"NETTING:{request['run_id']}:{key}",
                    "quality_state": "PASS",
                })

            attribution: List[Dict[str, Any]] = []
            for row, internally_netted in zip(rows, internal_netted):
                contribution = float(row["primary_signed_exposure"])
                external = contribution - internally_netted
                attribution.append({
                    "attribution_id": _stable_id("ATTR", {"run_id":request["run_id"],"key":key,"intent_id":row["intent_id"]}),
                    "strategy_id": row["strategy_id"],
                    "campaign_id": row["campaign_id"],
                    "strategy_version": row["strategy_version"],
                    "instrument_id": row["source_instrument_id"],
                    "underlying_id": row["source_underlying_id"],
                    "gross_requested_exposure": abs(float(row.get("requested_primary_signed_exposure", contribution))),
                    "accepted_exposure": contribution,
                    "netted_internal_exposure": internally_netted,
                    "external_executed_exposure": external,
                    "risk_contribution": None,
                    "cost_contribution": None,
                    "pnl_allocation_key": row["strategy_id"],
                    "allocation_reason": (
                        "EQS_PC_V1_FIXED_RISK_BUDGET_AND_NETTING"
                        if "requested_primary_signed_exposure" in row
                        else "EQS_PC_V1_CROSS_ASSET_NETTING"
                    ),
                })
            attribution.sort(key=lambda a: (a["strategy_id"], a["attribution_id"]))

            target_qty = net_primary / per_target_qty
            current_qty = float(request["current_target_quantities"].get(key, 0.0))
            representative_map = group_maps[0]
            notional_per_qty = target_notional_per_quantity
            target_notional = None if notional_per_qty is None else target_qty * float(notional_per_qty)
            target = {
                "target_id": _stable_id("PT", {"run_id":request["run_id"],"economic_netting_key":key,"target_instrument_id":target_instrument}),
                "portfolio_run_id": request["run_id"],
                "instrument_id": target_instrument,
                "underlying_id": target_underlying,
                "economic_netting_key": key,
                "current_quantity": current_qty,
                "target_quantity": target_qty,
                "trade_delta_quantity": target_qty-current_qty,
                "target_notional": target_notional,
                "reporting_currency": reporting_currency,
                "gross_strategy_exposure": gross_primary,
                "net_external_exposure": net_primary,
                "factor_exposures": factor_vectors,
                "source_attribution": attribution,
                "cost_estimate_id": request["cost_estimate_ids"][key],
                "liquidity_assessment_id": request["liquidity_assessment_ids"][key],
                "allocation_method_version": request["allocation_method_version"],
                "policy_snapshot_hash": request["policy_snapshot_hash"],
                "market_state_hash": request["market_state_hash"],
                "decision_reason_codes": sorted(set(
                    ["DETERMINISTIC_CROSS_ASSET_NETTING"]
                    + (["FIXED_RISK_BUDGET"] if any("requested_primary_signed_exposure" in r for r in rows) else [])
                    + (["STRATEGY_BUDGET_CLIPPED"] if any(float(r.get("strategy_budget_scale", 1.0)) < 1.0 - 1e-15 for r in rows) else [])
                    + (["NETTING_KEY_BUDGET_CLIPPED"] if any(float(r.get("group_budget_scale", 1.0)) < 1.0 - 1e-15 for r in rows) else [])
                )),
                "status": "RISK_PENDING",
                "target_hash": "",
            }
            target["target_hash"] = hash_without(target, "target_hash")
            targets.append(target)
            assertions.append({
                "assertion_id": _stable_id("EA", {"run_id":request["run_id"],"key":key}),
                "factor_id": primary_factor_id,
                "components": [
                    {"component_id": row["intent_id"], "instrument_id": row["source_instrument_id"], "signed_exposure": row["primary_signed_exposure"]}
                    for row in rows
                ],
                "declared_net_exposure": net_primary,
            })
        targets.sort(key=lambda x: x["target_id"])
        return targets, assertions

    def _build_certification_context(self, request: Dict[str, Any], assertions: List[Dict[str, Any]], targets: List[Dict[str, Any]], policy_resolution: Optional[Dict[str, Any]]=None, policy_generation_pin: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
        target_instruments = {t["instrument_id"] for t in targets}
        metadata = []
        derivative_exposures = []
        seen_meta = set()
        for mapping in request["exposure_mappings"].values():
            inst = mapping["target_instrument_id"]
            if inst not in target_instruments or inst in seen_meta:
                continue
            seen_meta.add(inst)
            metadata.append({
                "instrument_id": inst,
                "instrument_type": mapping["target_instrument_type"],
                "base_currency": mapping.get("base_currency"),
                "reporting_currency": mapping["reporting_currency"],
                "requires_currency_factor": bool(mapping.get("requires_currency_factor", False)),
                "required_greeks": list(mapping.get("required_greeks", [])),
            })
        for t in targets:
            mapping = next(m for m in request["exposure_mappings"].values() if m["economic_netting_key"]==t["economic_netting_key"])
            if mapping["target_instrument_type"] in {"FUTURE","OPTION"} and mapping.get("margin_requirement") is not None:
                derivative_exposures.append({
                    "instrument_id": t["instrument_id"],
                    "economic_exposure": t["net_external_exposure"],
                    "margin_requirement": float(mapping["margin_requirement"]),
                    "factor_id": mapping["primary_factor_id"],
                })
        cap = request["capital_state"]
        context = {
            "economic_exposure_assertions": assertions,
            "instrument_metadata": metadata,
            "derivative_exposures": derivative_exposures,
            "capital_state": {
                "available_cash": cap["available_cash"],
                "reserved_cash": cap["reserved_cash"],
                "proposed_cash_use": cap["proposed_cash_use"],
                "settlement_cash": cap.get("settlement_cash", 0.0),
            },
            "risk_estimates": _copy(request.get("risk_estimates", [])),
            "capacities": _copy(request.get("capacities", [])),
            "strategy_states": _copy(request.get("strategy_states", [])),
            "consumed_reservation_ids": _copy(request.get("consumed_reservation_ids", [])),
            "policy_resolution": _copy(policy_resolution),
            "policy_generation_pin": _copy(policy_generation_pin),
        }
        return context

    def _proposal_errors(self, request: Dict[str, Any], targets: List[Dict[str, Any]], context: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        for i, target in enumerate(targets):
            for e in self.validator.schemas.validate("target_exposure.schema.json", target):
                errors.append(f"TARGET_SCHEMA[{i}]: {e}")
            if target["target_hash"] != hash_without(target, "target_hash"):
                errors.append(f"TARGET_HASH_INVALID:{target['target_id']}")
            if not math.isclose(sum(a["accepted_exposure"] for a in target["source_attribution"]), target["net_external_exposure"], rel_tol=0, abs_tol=1e-9):
                errors.append(f"TARGET_ATTRIBUTION_NET_MISMATCH:{target['target_id']}")
        # Reuse certification-context rules with a deterministic BLOCKED sentinel response,
        # but do not claim that this is a Capital & Risk decision.
        dummy_request = self._build_reservation_request(request, targets, context)
        sentinel = {
            "reservation_request_id": dummy_request["reservation_request_id"],
            "decision": "BLOCKED",
            "reservation_id": None,
            "approved_target_set_hash": None,
            "approved_targets": [],
            "approved_constraints": {},
            "reservation_expiry": None,
            "decision_reason_codes": ["RUNTIME_PROPOSAL_VALIDATION_SENTINEL"],
            "capital_risk_evidence_hash": "0"*64,
            "decided_at": request["timeline"]["proposal_validated_at"],
        }
        report = self.validator.validate_chain(
            decision_time=request["decision_time"], intents=request["intents"], targets=targets,
            reservation_request=dummy_request, capital_risk_response=sentinel,
            execution_handoff=None, qualification_evidence=request["qualification_evidence"],
            certification_context=context, validation_time=request["timeline"]["proposal_validated_at"],
        )
        for rule in report["rule_results"]:
            if rule["status"] in {"FAIL","UNKNOWN"} and rule["severity"] in {"ERROR","CRITICAL"}:
                # Capital-response-only rules are not relevant to proposal validation.
                base = rule["rule_id"].split("[")[0]
                if base in {"PCV-030","PCV-031","PCV-032","PCV-033","PCV-034","PCV-035","PCV-036","PCV-040","PCV-048"}:
                    continue
                errors.append(f"{rule['rule_id']}:{rule['message']}")
        return sorted(set(errors))

    def _build_reservation_request(self, request: Dict[str, Any], targets: List[Dict[str, Any]], context: Dict[str, Any]) -> Dict[str, Any]:
        factor_after: Dict[str, Dict[str, Any]] = {}
        for t in targets:
            for f in t["factor_exposures"]:
                if f["factor_id"] not in factor_after:
                    factor_after[f["factor_id"]] = _copy(f)
                else:
                    factor_after[f["factor_id"]]["exposure_value"] += f["exposure_value"]
        ordered_after = [factor_after[k] for k in sorted(factor_after)]
        tsh = target_set_hash(targets)
        request_id = _stable_id("RR", {"run_id":request["run_id"],"target_set_hash":tsh})
        return {
            "reservation_request_id": request_id,
            "portfolio_run_id": request["run_id"],
            "proposed_target_set_hash": tsh,
            "current_positions_hash": sha256_obj(request["current_target_quantities"]),
            "market_state_hash": request["market_state_hash"],
            "policy_snapshot_hash": request["policy_snapshot_hash"],
            "policy_generation_pin_sha256": (context.get("policy_generation_pin") or {}).get("pin_sha256"),
            "targets": _copy(targets),
            "factor_vector_before": _copy(request.get("factor_vector_before", [])),
            "factor_vector_after": ordered_after,
            "gross_exposure_before": request["capital_state"]["gross_exposure_before"],
            "gross_exposure_after": sum(abs(t["net_external_exposure"]) for t in targets),
            "net_exposure_before": request["capital_state"]["net_exposure_before"],
            "net_exposure_after": sum(t["net_external_exposure"] for t in targets),
            "cash_effect": request["capital_state"]["cash_effect"],
            "estimated_margin_effect": request["capital_state"]["estimated_margin_effect"],
            "transaction_cost_summary": _copy(request["transaction_cost_summary"]),
            "liquidity_summary": _copy(request["liquidity_summary"]),
            "scenario_summary": _copy(request["scenario_summary"]),
            "concentration_summary": _copy(request["concentration_summary"]),
            "qualification_evidence_refs": sorted({i["qualification_evidence_id"] for i in request["intents"]}),
            "decision_trace_hash": sha256_obj({
                "runtime_version":RUNTIME_VERSION,
                "run_id":request["run_id"],
                "intents":request["intents"],
                "targets":targets,
                "context":context,
            }),
            "created_at": request["timeline"]["reservation_requested_at"],
        }

    def prepare(self, request: Dict[str, Any]) -> RuntimePreparation:
        request = _copy(request)
        noncanonical_error = None
        try:
            request_hash = sha256_obj(request)
        except Exception as exc:
            noncanonical_error = f"RUNTIME_REQUEST_NONCANONICAL:{type(exc).__name__}:{exc}"
            request_hash = sha256_obj({"run_id":str(request.get("run_id","INVALID-RUN")),"noncanonical_error":noncanonical_error})
        journal = ImmutableLifecycleJournal(str(request.get("run_id", "INVALID-RUN")))
        started = request.get("timeline", {}).get("run_started_at", request.get("decision_time", "1970-01-01T00:00:00Z"))
        journal = journal.append("PC_RUN_STARTED", started, {"runtime_version":RUNTIME_VERSION,"request_sha256":request_hash})

        errors = [noncanonical_error] if noncanonical_error else self._validate_runtime_request(request)
        if not errors:
            errors.extend(self._admission_errors(request))
        if errors:
            at = request.get("timeline", {}).get("proposal_validated_at", started)
            for reason in errors:
                journal = journal.append("PC_INTENT_BLOCKED", at, {"reason":reason})
            digest = sha256_obj({"run_id":request.get("run_id"),"request_sha256":request_hash,"status":"BLOCKED","blocking_reasons":errors,"journal_head":journal.head_sha256})
            return RuntimePreparation(RUNTIME_VERSION, request.get("run_id","INVALID-RUN"), "BLOCKED", request_hash, tuple(request.get("intents",[])), (), None, {}, tuple(errors), journal, digest)

        for intent in request["intents"]:
            journal = journal.append("PC_INTENT_ACCEPTED", request["timeline"]["proposal_validated_at"], {"intent_id":intent["intent_id"],"strategy_id":intent["strategy_id"],"strategy_version":intent["strategy_version"]})
        try:
            mapped, _raw_groups = self._map_intents(request)
            for row in mapped:
                journal = journal.append("PC_EXPOSURE_NORMALISED", request["timeline"]["proposal_validated_at"], row)
            resolved_policy, policy_resolution = self._resolve_allocation_policy(request)
            if policy_resolution is not None:
                journal = journal.append(
                    "PC_POLICY_RESOLUTION_STARTED", request["timeline"]["proposal_validated_at"],
                    {
                        "resolver_version": policy_resolution["resolver_version"],
                        "policy_id": policy_resolution["policy_id"],
                        "policy_version": policy_resolution["policy_version"],
                        "authority": policy_resolution["authority"],
                        "policy_seal_sha256": policy_resolution["policy_seal_sha256"],
                    },
                )
                journal = journal.append(
                    "PC_POLICY_AUTHORITY_VERIFIED", request["timeline"]["proposal_validated_at"],
                    {
                        "authority_evidence_id": policy_resolution["authority_evidence_id"],
                        "authority_evidence_sha256": policy_resolution["authority_evidence_sha256"],
                        "authority_snapshot_id": policy_resolution["authority_snapshot_id"],
                        "authority_snapshot_sha256": policy_resolution["authority_snapshot_sha256"],
                    },
                )
                lifecycle_events = request.get("policy_lifecycle_events") or []
                if lifecycle_events:
                    journal = journal.append(
                        "PC_POLICY_LIFECYCLE_VERIFIED", request["timeline"]["proposal_validated_at"],
                        {
                            "lifecycle_id": lifecycle_events[0]["lifecycle_id"],
                            "event_count": len(lifecycle_events),
                            "lifecycle_head_sha256": lifecycle_events[-1]["event_sha256"],
                            "policy_generation": policy_resolution["policy_generation"],
                            "policy_seal_sha256": policy_resolution["policy_seal_sha256"],
                        },
                    )
                journal = journal.append(
                    "PC_POLICY_BOUND", request["timeline"]["proposal_validated_at"],
                    {
                        "policy_id": policy_resolution["policy_id"],
                        "policy_version": policy_resolution["policy_version"],
                        "policy_generation": policy_resolution["policy_generation"],
                        "policy_payload_sha256": policy_resolution["policy_payload_sha256"],
                        "resolution_digest_sha256": policy_resolution["resolution_digest_sha256"],
                    },
                )
                policy_pin = build_policy_generation_pin(
                    run_id=request["run_id"],
                    request_sha256=request_hash,
                    policy_resolution=policy_resolution,
                    authority_snapshot=request["policy_authority_snapshot"],
                    lifecycle_events=request["policy_lifecycle_events"],
                ).to_dict()
                for e in self.validator.schemas.validate("policy_generation_pin.schema.json", policy_pin):
                    raise PolicyGenerationPinError("policy generation pin schema invalid: " + e)
                journal = journal.append(
                    "PC_POLICY_GENERATION_PINNED", request["timeline"]["proposal_validated_at"],
                    {
                        "pin_sha256": policy_pin["pin_sha256"],
                        "policy_id": policy_pin["policy_id"],
                        "policy_version": policy_pin["policy_version"],
                        "policy_generation": policy_pin["policy_generation"],
                        "policy_seal_sha256": policy_pin["policy_seal_sha256"],
                        "lifecycle_head_sha256": policy_pin["lifecycle_head_sha256"],
                    },
                )
            else:
                policy_pin = None
            allocated_rows, groups, allocation_result = self._allocate_mapped_rows(request, mapped, resolved_policy=resolved_policy)
            if allocation_result is not None:
                journal = journal.append(
                    "PC_ALLOCATION_STARTED", request["timeline"]["proposal_validated_at"],
                    {
                        "interface_version": allocation_result["interface_version"],
                        "method_version": allocation_result["method_version"],
                        "policy_id": allocation_result["policy_id"],
                        "policy_sha256": allocation_result["policy_sha256"],
                    },
                )
                for trace in allocation_result["traces"]:
                    journal = journal.append(
                        "PC_ALLOCATION_BUDGET_APPLIED", request["timeline"]["proposal_validated_at"], trace
                    )
                journal = journal.append(
                    "PC_ALLOCATION_COMPLETED", request["timeline"]["proposal_validated_at"],
                    {
                        "allocation_digest_sha256": allocation_result["allocation_digest_sha256"],
                        "policy_sha256": allocation_result["policy_sha256"],
                        "method_version": allocation_result["method_version"],
                    },
                )
            targets, assertions = self._build_targets(request, allocated_rows, groups)
            context = self._build_certification_context(request, assertions, targets, policy_resolution=policy_resolution, policy_generation_pin=policy_pin)
            for assertion in assertions:
                journal = journal.append("PC_NETTING_COMPLETED", request["timeline"]["proposal_validated_at"], assertion)
            for target in targets:
                journal = journal.append("PC_TARGET_PROPOSED", request["timeline"]["proposal_validated_at"], {"target_id":target["target_id"],"target_hash":target["target_hash"],"economic_netting_key":target["economic_netting_key"],"net_external_exposure":target["net_external_exposure"]})
            errors = self._proposal_errors(request, targets, context)
        except Exception as exc:
            errors = [f"RUNTIME_CONSTRUCTION_ERROR:{type(exc).__name__}:{exc}"]
            targets = []
            context = {}

        if errors:
            for reason in errors:
                journal = journal.append("PC_PROPOSAL_BLOCKED", request["timeline"]["proposal_validated_at"], {"reason":reason})
            digest = sha256_obj({"run_id":request["run_id"],"request_sha256":request_hash,"status":"BLOCKED","blocking_reasons":errors,"targets":targets,"journal_head":journal.head_sha256})
            return RuntimePreparation(RUNTIME_VERSION, request["run_id"], "BLOCKED", request_hash, tuple(request["intents"]), tuple(targets), None, context, tuple(errors), journal, digest)

        reservation_request = self._build_reservation_request(request, targets, context)
        rr_schema_errors = self.validator.schemas.validate("risk_reservation_request.schema.json", reservation_request)
        if rr_schema_errors:
            errors = [f"RESERVATION_REQUEST_SCHEMA:{e}" for e in rr_schema_errors]
            for reason in errors:
                journal = journal.append("PC_PROPOSAL_BLOCKED", request["timeline"]["reservation_requested_at"], {"reason":reason})
            digest = sha256_obj({"run_id":request["run_id"],"request_sha256":request_hash,"status":"BLOCKED","blocking_reasons":errors,"journal_head":journal.head_sha256})
            return RuntimePreparation(RUNTIME_VERSION, request["run_id"], "BLOCKED", request_hash, tuple(request["intents"]), tuple(targets), None, context, tuple(errors), journal, digest)
        journal = journal.append("PC_PRE_RESERVATION_VALIDATION_PASSED", request["timeline"]["proposal_validated_at"], {"target_set_hash":reservation_request["proposed_target_set_hash"]})
        journal = journal.append("PC_RISK_RESERVATION_REQUESTED", request["timeline"]["reservation_requested_at"], {"reservation_request_id":reservation_request["reservation_request_id"],"proposed_target_set_hash":reservation_request["proposed_target_set_hash"]})
        digest = sha256_obj({
            "runtime_version":RUNTIME_VERSION,"run_id":request["run_id"],"request_sha256":request_hash,"status":"RISK_PENDING",
            "targets":targets,"reservation_request":reservation_request,"certification_context":context,"journal_head":journal.head_sha256,
        })
        return RuntimePreparation(RUNTIME_VERSION, request["run_id"], "RISK_PENDING", request_hash, tuple(request["intents"]), tuple(targets), reservation_request, context, (), journal, digest, policy_generation_pin=policy_pin)

    def _revalidate_policy_generation_pin(self, request: Dict[str, Any], preparation: RuntimePreparation) -> Dict[str, Any]:
        pinned = _copy(preparation.policy_generation_pin)
        if pinned is None:
            # Synthetic/direct policies and NETTING_ONLY have no authoritative generation pin.
            return {}
        for e in self.validator.schemas.validate("policy_generation_pin.schema.json", pinned):
            raise PolicyGenerationPinError("prepared policy generation pin schema invalid: " + e)
        _resolved_policy, policy_resolution = self._resolve_allocation_policy(request)
        if policy_resolution is None:
            raise PolicyGenerationPinError("prepared authoritative pin cannot resolve to a non-authoritative policy")
        recomputed = build_policy_generation_pin(
            run_id=request["run_id"],
            request_sha256=preparation.request_sha256,
            policy_resolution=policy_resolution,
            authority_snapshot=request["policy_authority_snapshot"],
            lifecycle_events=request["policy_lifecycle_events"],
        ).to_dict()
        if recomputed != pinned:
            raise PolicyGenerationPinError("policy generation changed or pin material drifted between prepare and finalize")
        rr_pin = (preparation.reservation_request or {}).get("policy_generation_pin_sha256")
        if rr_pin != pinned["pin_sha256"]:
            raise PolicyGenerationPinError("reservation request policy generation pin mismatch")
        ctx_pin = (preparation.certification_context or {}).get("policy_generation_pin")
        if ctx_pin != pinned:
            raise PolicyGenerationPinError("certification context policy generation pin mismatch")
        return pinned

    def _build_handoff(self, request: Dict[str, Any], preparation: RuntimePreparation, response: Dict[str, Any], lease_lifecycle=None) -> Dict[str, Any]:
        decision = response["decision"]
        approved_targets = list(preparation.targets) if decision == "APPROVED" else _copy(response.get("approved_targets") or [])
        attributions: List[Dict[str, Any]] = []
        for target in approved_targets:
            attributions.extend(_copy(target.get("source_attribution", [])))
        handoff = {
            "execution_handoff_id": _stable_id("EH", {"run_id":preparation.run_id,"reservation_id":response["reservation_id"],"approved_target_set_hash":response["approved_target_set_hash"]}),
            "portfolio_run_id": preparation.run_id,
            "reservation_request_id": response["reservation_request_id"],
            "capital_reservation_id": response["reservation_id"],
            "reservation_expiry": lease_lifecycle.state().expires_at if lease_lifecycle is not None else response["reservation_expiry"],
            "approved_target_set_hash": response["approved_target_set_hash"],
            "instrument_targets": approved_targets,
            "urgency": request["execution_urgency"],
            "execution_constraints": _copy(response.get("approved_constraints") or {}),
            "source_strategy_attribution": attributions,
            "market_state_ref": request["market_state_ref"],
            "policy_ref": request["policy_ref"],
            "policy_generation_pin_sha256": (preparation.policy_generation_pin or {}).get("pin_sha256"),
            "reservation_lease_id": lease_lifecycle.lease_id if lease_lifecycle is not None else None,
            "reservation_lease_issue_sha256": lease_lifecycle.events[0].event_sha256 if lease_lifecycle is not None else None,
            "reservation_lease_head_sha256": lease_lifecycle.head_sha256 if lease_lifecycle is not None else None,
            "reservation_lease_renewal_count": lease_lifecycle.state().renewal_count if lease_lifecycle is not None else None,
            "reservation_lease_last_renewal_approval_sha256": lease_lifecycle.state().last_renewal_approval_sha256 if lease_lifecycle is not None else None,
            "decision_trace_ref": preparation.reservation_request["decision_trace_hash"],
            "created_at": request["timeline"]["handoff_created_at"],
            "handoff_hash": "",
        }
        handoff["handoff_hash"] = hash_without(handoff, "handoff_hash")
        return handoff

    def finalize(self, request: Dict[str, Any], preparation: RuntimePreparation, capital_risk_response: Dict[str, Any]) -> RuntimeResult:
        request = _copy(request)
        response = _copy(capital_risk_response)
        if preparation.request_sha256 != sha256_obj(request):
            raise RuntimeContractError("request changed between prepare and finalize")
        journal = preparation.journal
        if preparation.status != "RISK_PENDING" or preparation.reservation_request is None:
            journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
            seal = journal.seal()
            semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","request_sha256":preparation.request_sha256,"preparation_digest":preparation.preparation_digest_sha256,"journal_seal":seal.to_dict()})
            return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, None, None, None, None, preparation.blocking_reasons, journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin)

        try:
            revalidated_pin = self._revalidate_policy_generation_pin(request, preparation)
            if revalidated_pin:
                journal = journal.append(
                    "PC_POLICY_GENERATION_PIN_REVALIDATED", request["timeline"]["final_validated_at"],
                    {
                        "pin_sha256": revalidated_pin["pin_sha256"],
                        "policy_generation": revalidated_pin["policy_generation"],
                        "policy_seal_sha256": revalidated_pin["policy_seal_sha256"],
                        "lifecycle_head_sha256": revalidated_pin["lifecycle_head_sha256"],
                    },
                )
        except Exception as exc:
            reason = f"POLICY_GENERATION_PIN_MISMATCH:{type(exc).__name__}:{exc}"
            journal = journal.append("PC_POLICY_GENERATION_PIN_MISMATCH", request["timeline"]["final_validated_at"], {"reason":reason})
            journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
            seal = journal.seal()
            semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","request_sha256":preparation.request_sha256,"preparation_digest":preparation.preparation_digest_sha256,"reason":reason,"journal_seal":seal.to_dict()})
            return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, None, None, (reason,), journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin)

        response_schema = self.validator.schemas.validate("capital_risk_response.schema.json", response)
        if response_schema:
            reasons = tuple(f"CAPITAL_RISK_RESPONSE_SCHEMA:{e}" for e in response_schema)
            for reason in reasons:
                journal = journal.append("PC_RISK_RESPONSE_INVALID", request["timeline"]["final_validated_at"], {"reason":reason})
            journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
            seal = journal.seal()
            semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","response":response,"reasons":reasons,"journal_seal":seal.to_dict()})
            return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, None, None, reasons, journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin)

        response_contract_errors: List[str] = []
        if response.get("reservation_request_id") != preparation.reservation_request.get("reservation_request_id"):
            response_contract_errors.append("CAPITAL_RISK_RESPONSE_REQUEST_ID_MISMATCH")
        policy_binding_errors = validate_capital_risk_policy_binding(preparation.reservation_request, response)
        response_contract_errors.extend(policy_binding_errors)
        try:
            requested_at = parse_ts(preparation.reservation_request["created_at"])
            decided_at = parse_ts(response["decided_at"])
            handoff_at = parse_ts(request["timeline"]["handoff_created_at"])
            final_at = parse_ts(request["timeline"]["final_validated_at"])
            sealed_at = parse_ts(request["timeline"]["sealed_at"])
            if not (requested_at <= decided_at <= handoff_at <= final_at <= sealed_at):
                response_contract_errors.append("RUNTIME_RESPONSE_TIMELINE_NON_MONOTONIC")
        except Exception as exc:
            response_contract_errors.append(f"RUNTIME_RESPONSE_TIMELINE_INVALID:{exc}")
        if response_contract_errors:
            reasons = tuple(response_contract_errors)
            for reason in reasons:
                journal = journal.append("PC_RISK_RESPONSE_INVALID", request["timeline"]["final_validated_at"], {"reason":reason})
            journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
            seal = journal.seal()
            semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","response":response,"reasons":reasons,"journal_seal":seal.to_dict()})
            return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, None, None, reasons, journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin)

        if preparation.reservation_request.get("policy_generation_pin_sha256"):
            att = response["reservation_policy_attestation"]
            journal = journal.append(
                "PC_RISK_POLICY_ATTESTATION_VERIFIED", request["timeline"]["final_validated_at"],
                {
                    "policy_generation_pin_sha256": response["policy_generation_pin_sha256"],
                    "attestation_sha256": att["attestation_sha256"],
                    "reservation_request_id": response["reservation_request_id"],
                    "decision": response["decision"],
                },
            )
        journal = journal.append("PC_RISK_RESPONSE_RECEIVED", response["decided_at"], {"decision":response["decision"],"reservation_request_id":response["reservation_request_id"],"reservation_id":response.get("reservation_id")})
        if response["decision"] in {"APPROVED","APPROVED_WITH_REDUCTIONS"}:
            journal = journal.append("PC_RISK_APPROVED", response["decided_at"], {"decision":response["decision"],"reservation_id":response["reservation_id"],"approved_target_set_hash":response["approved_target_set_hash"],"policy_generation_pin_sha256":response.get("policy_generation_pin_sha256")})
        handoff = None
        lease_lifecycle = None
        if response["decision"] in {"APPROVED","APPROVED_WITH_REDUCTIONS"}:
            try:
                existing_lease = self.reservation_leases.get(response["reservation_id"])
                if existing_lease is None:
                    lease_lifecycle = self.reservation_leases.issue(
                        reservation_id=response["reservation_id"],
                        reservation_request_id=response["reservation_request_id"],
                        portfolio_run_id=preparation.run_id,
                        issued_at=response["decided_at"],
                        expires_at=response["reservation_expiry"],
                        approved_target_set_hash=response["approved_target_set_hash"],
                        policy_generation_pin_sha256=response.get("policy_generation_pin_sha256"),
                    )
                    lease_state = lease_lifecycle.state()
                    journal = journal.append("PC_RESERVATION_LEASE_ISSUED", response["decided_at"], {
                        "lease_id": lease_state.lease_id, "reservation_id": lease_state.reservation_id,
                        "reservation_request_id": lease_state.reservation_request_id, "expires_at": lease_state.expires_at,
                        "issue_event_sha256": lease_lifecycle.events[0].event_sha256,
                    })
                else:
                    lease_lifecycle = existing_lease
                    lease_state = lease_lifecycle.state()
                    expected_identity = (
                        response["reservation_id"], response["reservation_request_id"], preparation.run_id,
                        response["approved_target_set_hash"], response.get("policy_generation_pin_sha256"), response["reservation_expiry"],
                    )
                    actual_identity = (
                        lease_state.reservation_id, lease_state.reservation_request_id, lease_state.portfolio_run_id,
                        lease_state.approved_target_set_hash, lease_state.policy_generation_pin_sha256, lease_state.initial_expires_at,
                    )
                    if lease_state.status != "ACTIVE" or actual_identity != expected_identity:
                        raise ReservationLeaseError("pre-issued reservation lease is not active or does not match Capital & Risk approval")
                    journal = journal.append("PC_RESERVATION_LEASE_PREISSUED_VERIFIED", request["timeline"]["final_validated_at"], {
                        "reservation_id": lease_state.reservation_id, "lease_id": lease_state.lease_id,
                        "initial_expires_at": lease_state.initial_expires_at, "expires_at": lease_state.expires_at,
                        "renewal_count": lease_state.renewal_count, "lifecycle_head_sha256": lease_lifecycle.head_sha256,
                    })
                    for ev in lease_lifecycle.events:
                        if ev.event_type == "RENEWED":
                            journal = journal.append("PC_RESERVATION_LEASE_RENEWAL_VERIFIED", request["timeline"]["final_validated_at"], {
                                "reservation_id": lease_state.reservation_id, "lease_id": lease_state.lease_id,
                                "reapproved_at": ev.occurred_at,
                                "renewal_approval_id": ev.renewal_approval["renewal_approval_id"],
                                "renewal_approval_sha256": ev.renewal_approval["approval_sha256"],
                                "renewal_sequence": ev.renewal_approval["renewal_sequence"],
                                "new_expires_at": ev.renewal_approval["new_expires_at"],
                            })
                if parse_ts(request["timeline"]["handoff_created_at"]) >= parse_ts(lease_lifecycle.state().expires_at):
                    lease_lifecycle = self.reservation_leases.expire(response["reservation_id"], request["timeline"]["handoff_created_at"], "HANDOFF_AT_OR_AFTER_EXPIRY")
                    reason = "RESERVATION_LEASE_EXPIRED"
                    journal = journal.append("PC_RESERVATION_LEASE_EXPIRED", request["timeline"]["handoff_created_at"], {"reservation_id":response["reservation_id"],"lease_id":lease_lifecycle.lease_id,"reason":reason})
                    journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
                    seal = journal.seal()
                    semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","response":response,"reason":reason,"reservation_lease_lifecycle":lease_lifecycle.to_dict(),"journal_seal":seal.to_dict()})
                    return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, None, None, (reason,), journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin, reservation_lease_lifecycle=lease_lifecycle.to_dict())
                handoff = self._build_handoff(request, preparation, response, lease_lifecycle)
            except Exception as exc:
                reason = f"RESERVATION_LEASE_REPLAY_OR_INVALID:{type(exc).__name__}:{exc}"
                journal = journal.append("PC_RESERVATION_LEASE_REJECTED", request["timeline"]["final_validated_at"], {"reservation_id":response.get("reservation_id"),"reason":reason})
                journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":"BLOCKED","preseal_head_sha256":journal.head_sha256})
                seal = journal.seal()
                semantic = sha256_obj({"run_id":preparation.run_id,"status":"BLOCKED","response":response,"reason":reason,"journal_seal":seal.to_dict()})
                return RuntimeResult(RUNTIME_VERSION, preparation.run_id, "BLOCKED", preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, None, None, (reason,), journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin)
        else:
            journal = journal.append("PC_RISK_DENIED" if response["decision"]=="DENIED" else "PC_RISK_BLOCKED", response["decided_at"], {"decision":response["decision"],"reason_codes":response["decision_reason_codes"]})

        validation_context = _copy(preparation.certification_context)
        validation_context["consumed_reservation_ids"] = sorted(set(validation_context.get("consumed_reservation_ids", [])) | set(self.reservation_leases.terminal_reservation_ids()))
        if lease_lifecycle is not None:
            validation_context["reservation_lease_state"] = lease_lifecycle.state().to_dict()
            validation_context["reservation_lease_lifecycle"] = lease_lifecycle.to_dict()
        validation_report = self.validator.validate_chain(
            decision_time=request["decision_time"],
            intents=list(preparation.intents),
            targets=list(preparation.targets),
            reservation_request=preparation.reservation_request,
            capital_risk_response=response,
            execution_handoff=handoff,
            qualification_evidence=request["qualification_evidence"],
            certification_context=validation_context,
            validation_time=request["timeline"]["final_validated_at"],
        )
        if validation_report["overall_status"] != "PASS":
            blocking = tuple(r["rule_id"] for r in validation_report["rule_results"] if r["severity"] in {"ERROR","CRITICAL"} and r["status"] in {"FAIL","UNKNOWN"})
            journal = journal.append("PC_FINAL_VALIDATION_FAILED", request["timeline"]["final_validated_at"], {"blocking_rule_ids":list(blocking)})
            if lease_lifecycle is not None and lease_lifecycle.state().status == "ACTIVE":
                try:
                    if parse_ts(request["timeline"]["final_validated_at"]) >= parse_ts(lease_lifecycle.state().expires_at):
                        lease_lifecycle = self.reservation_leases.expire(response["reservation_id"], request["timeline"]["final_validated_at"], "FINAL_VALIDATION_AFTER_EXPIRY")
                        journal = journal.append("PC_RESERVATION_LEASE_EXPIRED", request["timeline"]["final_validated_at"], {"reservation_id":response["reservation_id"],"lease_id":lease_lifecycle.lease_id,"reason":"FINAL_VALIDATION_AFTER_EXPIRY"})
                    else:
                        lease_lifecycle = self.reservation_leases.cancel(response["reservation_id"], request["timeline"]["final_validated_at"], "FINAL_VALIDATION_FAILED")
                        journal = journal.append("PC_RESERVATION_LEASE_CANCELLED", request["timeline"]["final_validated_at"], {"reservation_id":response["reservation_id"],"lease_id":lease_lifecycle.lease_id,"reason":"FINAL_VALIDATION_FAILED"})
                except Exception as exc:
                    blocking = tuple(list(blocking) + [f"RESERVATION_LEASE_TERMINATION_FAILED:{type(exc).__name__}:{exc}"])
            handoff = None
            status = "BLOCKED"
        elif response["decision"] in {"APPROVED","APPROVED_WITH_REDUCTIONS"}:
            blocking = ()
            journal = journal.append("PC_FINAL_VALIDATION_PASSED", request["timeline"]["final_validated_at"], {"validation_report_hash":validation_report["report_hash"]})
            try:
                lease_lifecycle = self.reservation_leases.consume(response["reservation_id"], request["timeline"]["handoff_created_at"], handoff["execution_handoff_id"])
            except Exception as exc:
                reason = f"RESERVATION_LEASE_CONSUME_FAILED:{type(exc).__name__}:{exc}"
                blocking = (reason,)
                journal = journal.append("PC_RESERVATION_LEASE_REJECTED", request["timeline"]["final_validated_at"], {"reservation_id":response["reservation_id"],"reason":reason})
                handoff = None
                status = "BLOCKED"
            else:
                journal = journal.append("PC_RESERVATION_LEASE_CONSUMED", request["timeline"]["handoff_created_at"], {"capital_reservation_id":response["reservation_id"],"lease_id":lease_lifecycle.lease_id,"execution_handoff_id":handoff["execution_handoff_id"],"lifecycle_head_sha256":lease_lifecycle.head_sha256})
                journal = journal.append("PC_EXECUTION_HANDOFF_CREATED", request["timeline"]["handoff_created_at"], {"execution_handoff_id":handoff["execution_handoff_id"],"handoff_hash":handoff["handoff_hash"],"capital_reservation_id":handoff["capital_reservation_id"],"reservation_lease_id":lease_lifecycle.lease_id})
                journal = journal.append("PC_RESERVATION_CONSUMED", request["timeline"]["handoff_created_at"], {"capital_reservation_id":response["reservation_id"],"execution_handoff_id":handoff["execution_handoff_id"]})
                status = "EXECUTION_READY"
        else:
            blocking = ()
            journal = journal.append("PC_FINAL_VALIDATION_PASSED", request["timeline"]["final_validated_at"], {"validation_report_hash":validation_report["report_hash"]})
            status = "RISK_DENIED" if response["decision"]=="DENIED" else "RISK_BLOCKED"

        journal = journal.append("PC_RUN_SEALED", request["timeline"]["sealed_at"], {"status":status,"preseal_head_sha256":journal.head_sha256})
        seal = journal.seal()
        semantic_core = {
            "runtime_version":RUNTIME_VERSION,
            "run_id":preparation.run_id,
            "status":status,
            "request_sha256":preparation.request_sha256,
            "preparation_digest_sha256":preparation.preparation_digest_sha256,
            "targets":list(preparation.targets),
            "reservation_request":preparation.reservation_request,
            "capital_risk_response":response,
            "execution_handoff":handoff,
            "validation_report":validation_report,
            "reservation_lease_lifecycle": lease_lifecycle.to_dict() if lease_lifecycle is not None else None,
            "journal_seal":seal.to_dict(),
        }
        semantic = sha256_obj(semantic_core)
        return RuntimeResult(RUNTIME_VERSION, preparation.run_id, status, preparation.request_sha256, preparation.preparation_digest_sha256, preparation.targets, preparation.reservation_request, response, handoff, validation_report, blocking, journal, seal, semantic, policy_generation_pin=preparation.policy_generation_pin, reservation_lease_lifecycle=lease_lifecycle.to_dict() if lease_lifecycle is not None else None)

    def execute(self, request: Dict[str, Any], capital_risk_responder: Callable[[Dict[str, Any]], Dict[str, Any]]) -> RuntimeResult:
        preparation = self.prepare(request)
        if preparation.status != "RISK_PENDING" or preparation.reservation_request is None:
            return self.finalize(request, preparation, {})
        response = capital_risk_responder(_copy(preparation.reservation_request))
        return self.finalize(request, preparation, response)

    def replay(self, request: Dict[str, Any], capital_risk_response: Dict[str, Any], expected_semantic_replay_digest_sha256: Optional[str]=None, expected_journal_seal_sha256: Optional[str]=None, reservation_lease_snapshot: Optional[Dict[str, Any]]=None) -> RuntimeResult:
        # Replay verifies deterministic construction in an isolated lease registry and never
        # consumes or alters the live single-use reservation authority state. For a historically
        # renewed reservation, provide the sealed ACTIVE pre-finalize lease snapshot so the exact
        # renewal chain can be reconstructed without touching live authority.
        replay_registry = ReservationLeaseRegistry.from_snapshot(reservation_lease_snapshot) if reservation_lease_snapshot is not None else None
        replay_runtime = PortfolioConstructionRuntime(self.schema_dir, reservation_lease_registry=replay_registry)
        preparation = replay_runtime.prepare(request)
        result = replay_runtime.finalize(request, preparation, capital_risk_response)
        if expected_semantic_replay_digest_sha256 is not None and result.semantic_replay_digest_sha256 != expected_semantic_replay_digest_sha256:
            raise RuntimeContractError("deterministic replay digest mismatch")
        if expected_journal_seal_sha256 is not None and result.journal_seal.seal_sha256 != expected_journal_seal_sha256:
            raise RuntimeContractError("deterministic journal seal mismatch")
        return result
