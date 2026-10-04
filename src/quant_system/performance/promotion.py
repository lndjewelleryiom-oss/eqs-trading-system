from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from quant_system.evolution.evidence_runner import AlphaTrialEvidenceLedger, SourceClass, TrialState

from .lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleError,
    StrategyLifecycleState,
)


class ResearchPromotionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResearchPromotionResult:
    trial_id: str
    strategy_id: str
    strategy_version: str
    lifecycle_state: StrategyLifecycleState
    evidence_sha256: str
    manifest_sha256: str


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


_REQUIRED_GENUINE_PROVENANCE = (
    "admission_id",
    "admission_generation",
    "manifest_sha256",
    "dataset_manifest_fingerprint",
    "universe_fingerprint",
    "h02_batch_manifest_sha256",
    "universe_snapshot_sha256",
    "lifecycle_event_schema_version",
    "lifecycle_event_set_sha256",
    "coverage_record_set_sha256",
    "eligibility_record_set_sha256",
    "validator_version",
    "rule_catalog_version",
    "rule_catalog_sha256",
    "pit_certification_sha256",
)


def promote_passed_genuine_trial(
    *,
    alpha_ledger_path: str | Path,
    trial_id: str,
    lifecycle: PersistentStrategyLifecycle,
    strategy_version: str | None = None,
) -> ResearchPromotionResult:
    ledger = AlphaTrialEvidenceLedger(alpha_ledger_path)
    try:
        if not ledger.verify_hash_chain():
            raise ResearchPromotionError("ALPHA_EVIDENCE_HASH_CHAIN_INVALID")

        trial = ledger.trial(trial_id)
        if trial["source_class"] != SourceClass.GENUINE.value:
            raise ResearchPromotionError("SYNTHETIC_TRIAL_NOT_PROMOTABLE")
        if trial["state"] != TrialState.PASSED.value:
            raise ResearchPromotionError("TRIAL_NOT_PASSED")
        missing = [field for field in _REQUIRED_GENUINE_PROVENANCE if trial.get(field) in (None, "")]
        if missing:
            raise ResearchPromotionError("GENUINE_PROVENANCE_INCOMPLETE:" + ",".join(missing))

        evidence = ledger.db.execute(
            "SELECT evidence_json,evidence_sha256,decision_reasons_json FROM alpha_evidence WHERE trial_id=?",
            (trial_id,),
        ).fetchone()
        if evidence is None:
            raise ResearchPromotionError("ALPHA_EVIDENCE_MISSING")
        evidence_payload = json.loads(evidence["evidence_json"])
        if sha256(_canonical(evidence_payload)).hexdigest() != evidence["evidence_sha256"]:
            raise ResearchPromotionError("ALPHA_EVIDENCE_SHA256_INVALID")
        reasons = tuple(json.loads(evidence["decision_reasons_json"]))
        if "ALL_PRECOMMITTED_CHECKS_PASSED" not in reasons:
            raise ResearchPromotionError("PRECOMMITTED_RESEARCH_GATE_NOT_ATTESTED")

        strategy_id = str(trial["hypothesis_id"])
        strategy_version = strategy_version or f"alpha-trial-{trial_id}"
        blueprint = {}
        if trial.get("blueprint_json"):
            blueprint = json.loads(str(trial["blueprint_json"]))
        try:
            current = lifecycle.get(strategy_id, strategy_version)
        except StrategyLifecycleError:
            current = lifecycle.register(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                source_class="GENUINE",
                research_trial_id=trial_id,
                research_evidence_sha256=evidence["evidence_sha256"],
                research_manifest_sha256=trial["manifest_sha256"],
                asset_class=str(blueprint.get("asset_class") or "") or None,
                family=str(blueprint.get("family") or "") or None,
                horizon=str(blueprint.get("horizon") or "") or None,
                signal_description=str(blueprint.get("signal_description") or "") or None,
                reasons=("GENUINE_ALPHA_TRIAL_REGISTERED",),
            )

        if current.state == StrategyLifecycleState.RESEARCH:
            current = lifecycle.transition(
                strategy_id,
                strategy_version,
                StrategyLifecycleState.VALIDATED,
                reasons=("GENUINE_ALPHA_TRIAL_PASSED", "ALL_PRECOMMITTED_CHECKS_PASSED"),
                evidence_sha256=evidence["evidence_sha256"],
                genuine_research_gate_passed=True,
            )
        elif current.state != StrategyLifecycleState.VALIDATED:
            raise ResearchPromotionError(
                "LIFECYCLE_ALREADY_BEYOND_RESEARCH_PROMOTION:" + current.state.value
            )

        return ResearchPromotionResult(
            trial_id=trial_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            lifecycle_state=current.state,
            evidence_sha256=evidence["evidence_sha256"],
            manifest_sha256=trial["manifest_sha256"],
        )
    finally:
        ledger.close()
