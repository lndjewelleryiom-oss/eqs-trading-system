from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple
import copy
import math

from .validator import canonical_json, sha256_obj

ALLOCATION_INTERFACE_VERSION = "EQS-PC-ALLOCATE-v1"
FIXED_RISK_BUDGET_METHOD_VERSION = "FIXED_RISK_BUDGET@1.0.0"


class AllocationContractError(ValueError):
    """Fail-closed allocation contract violation."""


class AllocationEngine(Protocol):
    """Frozen allocation interface for EQS-PC-V1.0.0.

    Implementations receive already validated/normalised strategy rows and an explicit
    policy object. They may reduce exposure but must never create exposure absent from
    the input rows. Production limits remain external policy inputs.
    """

    interface_version: str
    method_version: str

    def allocate(
        self,
        *,
        run_id: str,
        decision_time: str,
        mapped_rows: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> "AllocationResult": ...


@dataclass(frozen=True)
class AllocationTrace:
    strategy_id: str
    intent_id: str
    economic_netting_key: str
    exposure_unit: str
    requested_primary_exposure: float
    strategy_budget: float
    strategy_scale: float
    post_strategy_budget_exposure: float
    group_budget: float
    group_scale: float
    allocated_primary_exposure: float
    clipped: bool
    reason_codes: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "intent_id": self.intent_id,
            "economic_netting_key": self.economic_netting_key,
            "exposure_unit": self.exposure_unit,
            "requested_primary_exposure": self.requested_primary_exposure,
            "strategy_budget": self.strategy_budget,
            "strategy_scale": self.strategy_scale,
            "post_strategy_budget_exposure": self.post_strategy_budget_exposure,
            "group_budget": self.group_budget,
            "group_scale": self.group_scale,
            "allocated_primary_exposure": self.allocated_primary_exposure,
            "clipped": self.clipped,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class AllocationResult:
    interface_version: str
    method_version: str
    policy_id: str
    policy_version: str
    policy_mode: str
    policy_sha256: str
    allocated_rows: Tuple[Dict[str, Any], ...]
    traces: Tuple[AllocationTrace, ...]
    allocation_digest_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interface_version": self.interface_version,
            "method_version": self.method_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_mode": self.policy_mode,
            "policy_sha256": self.policy_sha256,
            "allocated_rows": [copy.deepcopy(x) for x in self.allocated_rows],
            "traces": [x.to_dict() for x in self.traces],
            "allocation_digest_sha256": self.allocation_digest_sha256,
        }


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise AllocationContractError(f"{field} must be numeric") from exc
    if not math.isfinite(out) or out < 0:
        raise AllocationContractError(f"{field} must be finite and >= 0")
    return out


def _primary_component(row: Mapping[str, Any]) -> Mapping[str, Any]:
    matches = [
        f for f in row.get("factor_values", [])
        if f.get("factor_id") == row.get("primary_factor_id")
    ]
    if len(matches) != 1:
        raise AllocationContractError(
            f"intent {row.get('intent_id')} must contain exactly one primary factor component"
        )
    return matches[0]


class FixedRiskBudgetAllocator:
    """Deterministic fixed risk-budget allocator.

    V1 budgets are explicit absolute limits in the primary economic-factor units of an
    economic netting key. There are two independent caps:

      1) per-strategy/per-netting-key cap; then
      2) per-netting-key portfolio cap applied pro-rata to all surviving contributions.

    The allocator never supplies defaults for missing budgets and never treats margin or
    notional as a substitute for the primary economic factor. It is therefore reduction-
    only: abs(allocated contribution) <= abs(requested contribution) for every row.
    """

    interface_version = ALLOCATION_INTERFACE_VERSION
    method_version = FIXED_RISK_BUDGET_METHOD_VERSION

    def _validate_policy_authority(self, policy: Mapping[str, Any]) -> None:
        mode = policy.get("policy_mode")
        authority = policy.get("authority")
        if mode == "SYNTHETIC":
            if authority != "SYNTHETIC_CERTIFICATION_ONLY":
                raise AllocationContractError(
                    "synthetic fixed-risk-budget policy must use authority SYNTHETIC_CERTIFICATION_ONLY"
                )
            if policy.get("production_limits_authority") != "EQS-00/CAPITAL_RISK":
                raise AllocationContractError(
                    "synthetic policy must preserve EQS-00/CAPITAL_RISK production ownership"
                )
        elif mode == "PRODUCTION":
            # Runtime may consume a production policy only when an explicit immutable
            # authority reference/hash is supplied. It never creates these values.
            if authority not in {"EQS-00", "CAPITAL_RISK", "EQS-00/CAPITAL_RISK"}:
                raise AllocationContractError("production fixed-risk-budget policy lacks authorised issuer")
            if not policy.get("authority_ref") or not policy.get("authority_sha256"):
                raise AllocationContractError("production fixed-risk-budget policy lacks immutable authority evidence")
        else:
            raise AllocationContractError("policy_mode must be SYNTHETIC or PRODUCTION")

    def allocate(
        self,
        *,
        run_id: str,
        decision_time: str,
        mapped_rows: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> AllocationResult:
        # canonical_json rejects NaN/Infinity and non-canonical objects.
        canonical_json(policy)
        canonical_json(list(mapped_rows))
        self._validate_policy_authority(policy)

        if policy.get("policy_type") != "FIXED_RISK_BUDGET":
            raise AllocationContractError("wrong policy_type for FixedRiskBudgetAllocator")
        if policy.get("method_version") != self.method_version:
            raise AllocationContractError("fixed-risk-budget method_version mismatch")
        if policy.get("interface_version") != self.interface_version:
            raise AllocationContractError("allocation interface version mismatch")

        strategy_budget_index: Dict[tuple[str, str], Mapping[str, Any]] = {}
        for b in policy.get("strategy_budgets", []):
            key = (b.get("strategy_id"), b.get("economic_netting_key"))
            if key in strategy_budget_index:
                raise AllocationContractError(f"duplicate strategy budget: {key}")
            _finite_nonnegative(b.get("max_abs_primary_exposure"), f"strategy budget {key}")
            strategy_budget_index[key] = b

        group_budget_index: Dict[str, Mapping[str, Any]] = {}
        for b in policy.get("netting_key_budgets", []):
            key = b.get("economic_netting_key")
            if key in group_budget_index:
                raise AllocationContractError(f"duplicate netting-key budget: {key}")
            _finite_nonnegative(b.get("max_abs_net_exposure"), f"netting-key budget {key}")
            group_budget_index[key] = b

        if policy.get("unbudgeted_strategy_action") != "BLOCK":
            raise AllocationContractError("V1 requires unbudgeted_strategy_action=BLOCK")
        if policy.get("unbudgeted_netting_key_action") != "BLOCK":
            raise AllocationContractError("V1 requires unbudgeted_netting_key_action=BLOCK")

        ordered = sorted(
            [copy.deepcopy(dict(r)) for r in mapped_rows],
            key=lambda x: (x["economic_netting_key"], x["strategy_id"], x["intent_id"]),
        )

        # Stage 1: strategy budgets.
        stage1: List[Dict[str, Any]] = []
        meta: Dict[str, Dict[str, Any]] = {}
        for row in ordered:
            key = (row["strategy_id"], row["economic_netting_key"])
            budget = strategy_budget_index.get(key)
            if budget is None:
                raise AllocationContractError(
                    f"missing strategy budget for {row['strategy_id']} / {row['economic_netting_key']}"
                )
            primary = _primary_component(row)
            unit = str(primary["exposure_unit"])
            if budget.get("exposure_unit") != unit:
                raise AllocationContractError(
                    f"strategy budget unit mismatch for {row['strategy_id']} / {row['economic_netting_key']}"
                )
            requested = float(row["primary_signed_exposure"])
            if not math.isfinite(requested):
                raise AllocationContractError("requested primary exposure must be finite")
            limit = _finite_nonnegative(budget["max_abs_primary_exposure"], "max_abs_primary_exposure")
            scale = 1.0 if abs(requested) <= limit or math.isclose(abs(requested), limit, rel_tol=0, abs_tol=1e-12) else (0.0 if requested == 0 else limit / abs(requested))
            if scale < 0 or scale > 1 + 1e-15:
                raise AllocationContractError("strategy budget attempted risk amplification")
            scale = min(1.0, max(0.0, scale))
            row["requested_primary_signed_exposure"] = requested
            row["strategy_budget_scale"] = scale
            row["primary_signed_exposure"] = requested * scale
            for f in row["factor_values"]:
                f["requested_signed_exposure"] = float(f["signed_exposure"])
                f["signed_exposure"] = float(f["signed_exposure"]) * scale
            stage1.append(row)
            meta[row["intent_id"]] = {
                "unit": unit,
                "strategy_budget": limit,
                "strategy_scale": scale,
                "requested": requested,
                "post_strategy": row["primary_signed_exposure"],
            }

        # Stage 2: netting-key budget, pro-rata reduction across already-budgeted rows.
        group_scales: Dict[str, float] = {}
        group_limits: Dict[str, float] = {}
        for netkey in sorted({r["economic_netting_key"] for r in stage1}):
            rows = [r for r in stage1 if r["economic_netting_key"] == netkey]
            budget = group_budget_index.get(netkey)
            if budget is None:
                raise AllocationContractError(f"missing netting-key budget for {netkey}")
            units = {_primary_component(r)["exposure_unit"] for r in rows}
            if len(units) != 1:
                raise AllocationContractError(f"incompatible primary exposure units inside {netkey}")
            unit = next(iter(units))
            if budget.get("exposure_unit") != unit:
                raise AllocationContractError(f"netting-key budget unit mismatch for {netkey}")
            limit = _finite_nonnegative(budget["max_abs_net_exposure"], "max_abs_net_exposure")
            net = sum(float(r["primary_signed_exposure"]) for r in rows)
            scale = 1.0 if abs(net) <= limit or math.isclose(abs(net), limit, rel_tol=0, abs_tol=1e-12) else (0.0 if net == 0 else limit / abs(net))
            if scale < 0 or scale > 1 + 1e-15:
                raise AllocationContractError("netting-key budget attempted risk amplification")
            group_scales[netkey] = min(1.0, max(0.0, scale))
            group_limits[netkey] = limit

        traces: List[AllocationTrace] = []
        allocated: List[Dict[str, Any]] = []
        for row in stage1:
            netkey = row["economic_netting_key"]
            gscale = group_scales[netkey]
            post_strategy = float(row["primary_signed_exposure"])
            row["group_budget_scale"] = gscale
            row["primary_signed_exposure"] = post_strategy * gscale
            for f in row["factor_values"]:
                f["signed_exposure"] = float(f["signed_exposure"]) * gscale
            m = meta[row["intent_id"]]
            clipped_strategy = m["strategy_scale"] < 1.0 - 1e-15
            clipped_group = gscale < 1.0 - 1e-15
            reasons = ["FIXED_RISK_BUDGET"]
            if clipped_strategy:
                reasons.append("STRATEGY_BUDGET_CLIPPED")
            if clipped_group:
                reasons.append("NETTING_KEY_BUDGET_CLIPPED")
            if not clipped_strategy and not clipped_group:
                reasons.append("WITHIN_FIXED_RISK_BUDGET")
            traces.append(AllocationTrace(
                strategy_id=row["strategy_id"],
                intent_id=row["intent_id"],
                economic_netting_key=netkey,
                exposure_unit=m["unit"],
                requested_primary_exposure=m["requested"],
                strategy_budget=m["strategy_budget"],
                strategy_scale=m["strategy_scale"],
                post_strategy_budget_exposure=post_strategy,
                group_budget=group_limits[netkey],
                group_scale=gscale,
                allocated_primary_exposure=float(row["primary_signed_exposure"]),
                clipped=clipped_strategy or clipped_group,
                reason_codes=tuple(reasons),
            ))
            allocated.append(row)

        # Reduction-only invariant.
        for row in allocated:
            requested = float(row["requested_primary_signed_exposure"])
            accepted = float(row["primary_signed_exposure"])
            if abs(accepted) > abs(requested) + 1e-12:
                raise AllocationContractError("fixed risk budget amplified strategy exposure")
            if requested != 0 and accepted != 0 and math.copysign(1.0, requested) != math.copysign(1.0, accepted):
                raise AllocationContractError("fixed risk budget reversed strategy direction")

        policy_sha = sha256_obj(policy)
        core = {
            "interface_version": self.interface_version,
            "method_version": self.method_version,
            "run_id": run_id,
            "decision_time": decision_time,
            "policy_sha256": policy_sha,
            "allocated_rows": allocated,
            "traces": [x.to_dict() for x in traces],
        }
        digest = sha256_obj(core)
        return AllocationResult(
            interface_version=self.interface_version,
            method_version=self.method_version,
            policy_id=str(policy["policy_id"]),
            policy_version=str(policy["policy_version"]),
            policy_mode=str(policy["policy_mode"]),
            policy_sha256=policy_sha,
            allocated_rows=tuple(allocated),
            traces=tuple(traces),
            allocation_digest_sha256=digest,
        )


class AllocationRegistry:
    """Frozen method-version -> allocator registry.

    Unknown methods fail closed. NETTING_ONLY is retained by the runtime as its legacy
    no-allocation compatibility path and is intentionally not an allocator implementation.
    """

    def __init__(self) -> None:
        self._allocators: Dict[str, AllocationEngine] = {
            FIXED_RISK_BUDGET_METHOD_VERSION: FixedRiskBudgetAllocator(),
        }

    def resolve(self, method_version: str) -> AllocationEngine:
        try:
            return self._allocators[method_version]
        except KeyError as exc:
            raise AllocationContractError(f"unsupported allocation method: {method_version}") from exc
