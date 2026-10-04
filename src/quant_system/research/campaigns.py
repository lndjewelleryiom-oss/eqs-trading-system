from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid5

from quant_system.data.crypto_perps.research_datasets import ResearchDatasetManifest
from quant_system.evolution.budget import ResearchTrialBudget
from quant_system.evolution.factory import StrategyBlueprint
from quant_system.evolution.models import AcceptanceCriteria, Hypothesis, ResearchEvidence
from quant_system.features.models import FeatureRunManifest
from quant_system.validation.gates import ValidationThresholds, assess_candidate

from .manifest import ExperimentManifest

_NAMESPACE = UUID("c3cf2c94-7ec8-4d89-8b3c-04a5c67cc8ec")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(payload: object) -> str:
    return sha256(_canonical_json(payload)).hexdigest()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SearchDimension:
    name: str
    kind: str
    low: float | int | None = None
    high: float | int | None = None
    step: float | int | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("search dimension name is required")
        if self.kind not in {"int", "float", "choice"}:
            raise ValueError("search dimension kind must be int, float, or choice")
        if self.kind == "choice":
            if not self.choices or len(set(self.choices)) != len(self.choices):
                raise ValueError("choice dimensions require unique choices")
            if any(value is not None for value in (self.low, self.high, self.step)):
                raise ValueError("choice dimensions cannot define numeric bounds")
        else:
            if self.low is None or self.high is None or self.step is None:
                raise ValueError("numeric dimensions require low/high/step")
            if not all(math.isfinite(float(v)) for v in (self.low, self.high, self.step)):
                raise ValueError("search bounds must be finite")
            if float(self.low) > float(self.high) or float(self.step) <= 0:
                raise ValueError("invalid search bounds")

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "low": self.low,
            "high": self.high,
            "step": self.step,
            "choices": self.choices,
        }


@dataclass(frozen=True, slots=True)
class ResearchPeriods:
    training_start: datetime
    training_end: datetime
    validation_start: datetime
    validation_end: datetime
    oos_start: datetime
    oos_end: datetime

    def __post_init__(self) -> None:
        for name in (
            "training_start", "training_end", "validation_start", "validation_end", "oos_start", "oos_end"
        ):
            _aware(getattr(self, name), name)
        if not (
            self.training_start < self.training_end < self.validation_start < self.validation_end
            < self.oos_start < self.oos_end
        ):
            raise ValueError("training, validation, and OOS periods must be strictly ordered and disjoint")

    def to_payload(self) -> dict[str, str]:
        return {name: getattr(self, name).isoformat() for name in (
            "training_start", "training_end", "validation_start", "validation_end", "oos_start", "oos_end"
        )}


@dataclass(frozen=True, slots=True)
class StatisticalGate:
    min_oos_mean_after_costs: float = 0.0
    min_walk_forward_positive_fraction: float = 2 / 3
    min_parameter_robust_fraction: float = 2 / 3
    min_dsr_probability: float = 0.95
    max_pbo: float = 0.25
    max_reality_check_p: float = 0.05
    bootstrap_confidence: float = 0.95
    bootstrap_lower_mean_gt: float = 0.0
    alpha: float = 0.05

    def __post_init__(self) -> None:
        probabilities = (
            self.min_walk_forward_positive_fraction,
            self.min_parameter_robust_fraction,
            self.min_dsr_probability,
            self.max_pbo,
            self.max_reality_check_p,
            self.bootstrap_confidence,
            self.alpha,
        )
        if any(not 0 < value <= 1 for value in probabilities):
            raise ValueError("statistical gate probabilities must lie in (0, 1]")

    def to_validation_thresholds(self) -> ValidationThresholds:
        return ValidationThresholds(
            min_dsr_probability=self.min_dsr_probability,
            max_pbo=self.max_pbo,
            max_reality_check_p=self.max_reality_check_p,
            min_walk_forward_positive_fraction=self.min_walk_forward_positive_fraction,
            min_parameter_profitable_fraction=self.min_parameter_robust_fraction,
        )

    def to_acceptance_criteria(self) -> AcceptanceCriteria:
        return AcceptanceCriteria(
            min_oos_mean=self.min_oos_mean_after_costs,
            min_walk_forward_positive_fraction=self.min_walk_forward_positive_fraction,
            min_parameter_robust_fraction=self.min_parameter_robust_fraction,
            min_dsr_probability=self.min_dsr_probability,
            max_pbo=self.max_pbo,
            max_reality_check_p=self.max_reality_check_p,
        )


@dataclass(frozen=True, slots=True)
class PreregisteredCampaign:
    campaign_id: str
    version: str
    family: str
    economic_hypothesis: str
    feature_inputs: tuple[str, ...]
    parameter_space: tuple[SearchDimension, ...]
    transaction_cost_assumptions: tuple[tuple[str, str], ...]
    funding_financing_treatment: tuple[str, ...]
    universe_rules: tuple[str, ...]
    periods: ResearchPeriods
    validation_design: tuple[str, ...]
    minimum_requirements: tuple[tuple[str, int], ...]
    rejection_conditions: tuple[str, ...]
    sensitivity_tests: tuple[str, ...]
    bootstrap_monte_carlo: tuple[str, ...]
    multiple_testing: tuple[str, ...]
    statistical_gate: StatisticalGate
    capacity_liquidity_constraints: tuple[str, ...]
    promotion_requirements: tuple[str, ...]
    max_parameter_combinations: int
    max_family_trials: int
    random_seed: int
    preregistered_at: datetime
    empirical_status: str = "UNVIEWED"
    required_dataset_format: str = "crypto-perps-research-v1"
    required_feature_engine: str = "crypto-perps-feature-engine-v1"

    def __post_init__(self) -> None:
        _aware(self.preregistered_at, "preregistered_at")
        if self.empirical_status != "UNVIEWED":
            raise ValueError("new preregistrations must start UNVIEWED")
        for name in ("campaign_id", "version", "family", "economic_hypothesis"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not self.feature_inputs or not self.parameter_space:
            raise ValueError("feature inputs and bounded parameter space are required")
        if len({dimension.name for dimension in self.parameter_space}) != len(self.parameter_space):
            raise ValueError("parameter dimension names must be unique")
        if self.max_parameter_combinations <= 0 or self.max_family_trials <= 0:
            raise ValueError("research budgets must be positive")
        if tuple(sorted(self.minimum_requirements)) != self.minimum_requirements:
            raise ValueError("minimum requirements must be canonically sorted")
        if tuple(sorted(self.transaction_cost_assumptions)) != self.transaction_cost_assumptions:
            raise ValueError("cost assumptions must be canonically sorted")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "version": self.version,
            "family": self.family,
            "economic_hypothesis": self.economic_hypothesis,
            "feature_inputs": self.feature_inputs,
            "parameter_space": [item.to_payload() for item in self.parameter_space],
            "transaction_cost_assumptions": self.transaction_cost_assumptions,
            "funding_financing_treatment": self.funding_financing_treatment,
            "universe_rules": self.universe_rules,
            "periods": self.periods.to_payload(),
            "validation_design": self.validation_design,
            "minimum_requirements": self.minimum_requirements,
            "rejection_conditions": self.rejection_conditions,
            "sensitivity_tests": self.sensitivity_tests,
            "bootstrap_monte_carlo": self.bootstrap_monte_carlo,
            "multiple_testing": self.multiple_testing,
            "statistical_gate": {
                "min_oos_mean_after_costs": self.statistical_gate.min_oos_mean_after_costs,
                "min_walk_forward_positive_fraction": self.statistical_gate.min_walk_forward_positive_fraction,
                "min_parameter_robust_fraction": self.statistical_gate.min_parameter_robust_fraction,
                "min_dsr_probability": self.statistical_gate.min_dsr_probability,
                "max_pbo": self.statistical_gate.max_pbo,
                "max_reality_check_p": self.statistical_gate.max_reality_check_p,
                "bootstrap_confidence": self.statistical_gate.bootstrap_confidence,
                "bootstrap_lower_mean_gt": self.statistical_gate.bootstrap_lower_mean_gt,
                "alpha": self.statistical_gate.alpha,
            },
            "capacity_liquidity_constraints": self.capacity_liquidity_constraints,
            "promotion_requirements": self.promotion_requirements,
            "max_parameter_combinations": self.max_parameter_combinations,
            "max_family_trials": self.max_family_trials,
            "random_seed": self.random_seed,
            "preregistered_at": self.preregistered_at.isoformat(),
            "empirical_status": self.empirical_status,
            "required_dataset_format": self.required_dataset_format,
            "required_feature_engine": self.required_feature_engine,
        }

    @property
    def fingerprint(self) -> str:
        return _sha(self.canonical_payload())

    def to_record(self) -> dict[str, object]:
        return {"campaign_fingerprint": self.fingerprint, "campaign": self.canonical_payload()}

    def write_immutable(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_record(), sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if destination.read_bytes() != payload:
                raise RuntimeError("preregistered campaign is immutable and existing content differs")

    def to_hypothesis(self) -> Hypothesis:
        return Hypothesis(
            statement=self.economic_hypothesis,
            economic_rationale=f"Preregistered institutional campaign {self.campaign_id}; results remain falsification-led.",
            family=self.family,
            asset_class="crypto-linear-perpetuals",
            horizon="intraday-to-multiday",
            source_anomaly=f"PREREGISTERED:{self.fingerprint}",
            criteria=self.statistical_gate.to_acceptance_criteria(),
            hypothesis_id=uuid5(_NAMESPACE, self.fingerprint),
        )

    def to_strategy_blueprint(self) -> StrategyBlueprint:
        hypothesis = self.to_hypothesis()
        return StrategyBlueprint(
            hypothesis_id=str(hypothesis.hypothesis_id),
            family=self.family,
            asset_class=hypothesis.asset_class,
            horizon=hypothesis.horizon,
            signal_description=self.economic_hypothesis,
            falsification_tests=tuple(dict.fromkeys((
                *self.validation_design,
                *self.rejection_conditions,
                *self.sensitivity_tests,
                *self.multiple_testing,
                *self.promotion_requirements,
            ))),
        )

    def research_budget(self) -> ResearchTrialBudget:
        return ResearchTrialBudget(
            max_hypotheses=1,
            max_trials_per_hypothesis=self.max_family_trials,
            max_parameter_combinations=self.max_parameter_combinations,
        )


@dataclass(frozen=True, slots=True)
class CampaignLineageBinding:
    campaign_fingerprint: str
    dataset_manifest_fingerprint: str
    dataset_id: str
    universe_fingerprint: str
    feature_run_fingerprints: tuple[str, ...]
    feature_registry_fingerprints: tuple[str, ...]
    feature_config_fingerprints: tuple[str, ...]
    decision_times: tuple[str, ...]
    alpha_data_binding_id: str | None = None
    alpha_data_binding_decision_fingerprint: str | None = None

    @classmethod
    def _bind_lineage_only(
        cls,
        campaign: PreregisteredCampaign,
        dataset: ResearchDatasetManifest,
        feature_runs: Iterable[FeatureRunManifest],
    ) -> "CampaignLineageBinding":
        if dataset.format_version != campaign.required_dataset_format:
            raise ValueError("R1.3 dataset format does not satisfy campaign preregistration")
        dataset_fp = dataset.fingerprint()
        runs = tuple(feature_runs)
        if not runs:
            raise ValueError("at least one FeatureRunManifest is required")
        for run in runs:
            if run.engine_version != campaign.required_feature_engine:
                raise ValueError("feature engine version does not satisfy campaign preregistration")
            if dataset_fp not in run.input_dataset_fingerprints:
                raise ValueError("feature run is not lineage-bound to the R1.3 dataset manifest")
            if run.universe_version != dataset.universe_fingerprint:
                raise ValueError("feature run universe lineage differs from R1.3 manifest")
            if run.decision_time > dataset.decision_time:
                raise ValueError("feature decision exceeds R1.3 manifest decision time")
        return cls(
            campaign_fingerprint=campaign.fingerprint,
            dataset_manifest_fingerprint=dataset_fp,
            dataset_id=dataset.dataset_id,
            universe_fingerprint=dataset.universe_fingerprint,
            feature_run_fingerprints=tuple(sorted(run.fingerprint for run in runs)),
            feature_registry_fingerprints=tuple(sorted({run.registry_fingerprint for run in runs})),
            feature_config_fingerprints=tuple(sorted({run.config_fingerprint for run in runs})),
            decision_times=tuple(sorted(run.decision_time.isoformat() for run in runs)),
        )

    @classmethod
    def bind(
        cls,
        campaign: PreregisteredCampaign,
        dataset: ResearchDatasetManifest,
        feature_runs: Iterable[FeatureRunManifest],
        *,
        binding_report: Mapping[str, object] | None = None,
    ) -> "CampaignLineageBinding":
        from .alpha_binding import AlphaDataBindingError, validate_alpha_data_binding_report

        if binding_report is None:
            raise AlphaDataBindingError("validated Alpha-v1 data-binding report is required")
        validate_alpha_data_binding_report(binding_report)
        final = binding_report["final_decision"]
        assert isinstance(final, Mapping)
        if final.get("ALPHA_DATA_BINDING_READY") is not True:
            raise AlphaDataBindingError("ALPHA_DATA_BINDING_READY is false")

        campaign_rows = binding_report["campaign_readiness"]
        assert isinstance(campaign_rows, list)
        matching = [row for row in campaign_rows if isinstance(row, Mapping) and row.get("campaign_id") == campaign.campaign_id]
        if len(matching) != 1 or matching[0].get("campaign_fingerprint") != campaign.fingerprint:
            raise AlphaDataBindingError("binding report does not contain the exact frozen campaign fingerprint")
        if matching[0].get("binding_ready") is not True:
            raise AlphaDataBindingError("campaign is not binding-ready")

        dataset_record = binding_report["dataset"]
        feature_record = binding_report["feature_engine"]
        assert isinstance(dataset_record, Mapping) and isinstance(feature_record, Mapping)
        dataset_fp = dataset.fingerprint()
        if dataset_record.get("manifest_fingerprint") != dataset_fp:
            raise AlphaDataBindingError("binding report dataset manifest differs from requested R1.3 manifest")
        if dataset_record.get("dataset_id") != dataset.dataset_id:
            raise AlphaDataBindingError("binding report dataset_id differs from requested R1.3 manifest")
        if dataset_record.get("universe_fingerprint") != dataset.universe_fingerprint:
            raise AlphaDataBindingError("binding report universe differs from requested R1.3 manifest")

        runs = tuple(feature_runs)
        if not runs:
            raise AlphaDataBindingError("at least one FeatureRunManifest is required")
        allowed_runs = set(feature_record.get("run_manifest_fingerprints", ()))
        missing_runs = sorted(run.fingerprint for run in runs if run.fingerprint not in allowed_runs)
        if missing_runs:
            raise AlphaDataBindingError("requested FeatureRunManifest is not covered by the binding report")

        lineage = cls._bind_lineage_only(campaign, dataset, runs)
        return cls(
            campaign_fingerprint=lineage.campaign_fingerprint,
            dataset_manifest_fingerprint=lineage.dataset_manifest_fingerprint,
            dataset_id=lineage.dataset_id,
            universe_fingerprint=lineage.universe_fingerprint,
            feature_run_fingerprints=lineage.feature_run_fingerprints,
            feature_registry_fingerprints=lineage.feature_registry_fingerprints,
            feature_config_fingerprints=lineage.feature_config_fingerprints,
            decision_times=lineage.decision_times,
            alpha_data_binding_id=str(binding_report["binding_id"]),
            alpha_data_binding_decision_fingerprint=str(final["decision_fingerprint"]),
        )

    @property
    def fingerprint(self) -> str:
        return _sha({
            "campaign_fingerprint": self.campaign_fingerprint,
            "dataset_manifest_fingerprint": self.dataset_manifest_fingerprint,
            "dataset_id": self.dataset_id,
            "universe_fingerprint": self.universe_fingerprint,
            "feature_run_fingerprints": self.feature_run_fingerprints,
            "feature_registry_fingerprints": self.feature_registry_fingerprints,
            "feature_config_fingerprints": self.feature_config_fingerprints,
            "decision_times": self.decision_times,
            "alpha_data_binding_id": self.alpha_data_binding_id,
            "alpha_data_binding_decision_fingerprint": self.alpha_data_binding_decision_fingerprint,
        })

    def to_experiment_manifest(
        self,
        campaign: PreregisteredCampaign,
        *,
        code_version: str,
        parameters: Mapping[str, Any],
        software_versions: Mapping[str, str] | None = None,
    ) -> ExperimentManifest:
        from .alpha_binding import AlphaDataBindingError

        if self.campaign_fingerprint != campaign.fingerprint:
            raise ValueError("lineage binding belongs to a different campaign")
        if self.alpha_data_binding_id is None or self.alpha_data_binding_decision_fingerprint is None:
            raise AlphaDataBindingError("unverified lineage cannot create an empirical ExperimentManifest")
        return ExperimentManifest(
            strategy_id=campaign.campaign_id,
            hypothesis=campaign.economic_hypothesis,
            data_fingerprints={
                "r1.3_manifest": self.dataset_manifest_fingerprint,
                "universe": self.universe_fingerprint,
                "campaign": campaign.fingerprint,
                "campaign_lineage": self.fingerprint,
                "alpha_data_binding_id": self.alpha_data_binding_id,
                "alpha_data_binding_decision": self.alpha_data_binding_decision_fingerprint,
            },
            code_version=code_version,
            parameters={"campaign_fingerprint": campaign.fingerprint, "candidate_parameters": dict(parameters)},
            cost_assumptions=dict(campaign.transaction_cost_assumptions),
            split_spec=campaign.periods.to_payload() | {
                "validation_design": campaign.validation_design,
                "multiple_testing": campaign.multiple_testing,
            },
            random_seed=campaign.random_seed,
            software_versions=software_versions or {},
        )


@dataclass(frozen=True, slots=True)
class CampaignEvidenceEnvelope:
    research_evidence: ResearchEvidence
    trade_count: int
    independent_sample_count: int
    bootstrap_lower_mean: float
    bootstrap_simulations: int
    monte_carlo_simulations: int
    multiple_testing_corrected: bool
    capacity_constraints_passed: bool
    lineage_verified: bool


@dataclass(frozen=True, slots=True)
class CampaignDecision:
    passed: bool
    reasons: tuple[str, ...]


def assess_campaign_candidate(campaign: PreregisteredCampaign, evidence: CampaignEvidenceEnvelope) -> CampaignDecision:
    base = assess_candidate(
        out_of_sample_profitable_after_costs=evidence.research_evidence.oos_mean_after_costs > campaign.statistical_gate.min_oos_mean_after_costs,
        walk_forward_positive_fraction=evidence.research_evidence.walk_forward_positive_fraction,
        parameter_profitable_fraction=evidence.research_evidence.parameter_robust_fraction,
        cost_stress_survived=evidence.research_evidence.cost_stress_passed,
        dsr_probability=evidence.research_evidence.dsr_probability,
        pbo=evidence.research_evidence.pbo,
        reality_check_p=evidence.research_evidence.reality_check_p,
        leakage_checks_passed=evidence.research_evidence.leakage_checks_passed,
        thresholds=campaign.statistical_gate.to_validation_thresholds(),
    )
    minimums = dict(campaign.minimum_requirements)
    checks = {
        "MINIMUM_TRADES_FAILED": evidence.trade_count >= minimums.get("trades", 0),
        "MINIMUM_SAMPLES_FAILED": evidence.independent_sample_count >= minimums.get("independent_samples", 0),
        "BOOTSTRAP_LOWER_BOUND_FAILED": evidence.bootstrap_lower_mean > campaign.statistical_gate.bootstrap_lower_mean_gt,
        "BOOTSTRAP_SIMULATIONS_FAILED": evidence.bootstrap_simulations >= minimums.get("bootstrap_simulations", 0),
        "MONTE_CARLO_SIMULATIONS_FAILED": evidence.monte_carlo_simulations >= minimums.get("monte_carlo_simulations", 0),
        "MULTIPLE_TESTING_CORRECTION_MISSING": evidence.multiple_testing_corrected,
        "CAPACITY_CONSTRAINT_FAILED": evidence.capacity_constraints_passed,
        "LINEAGE_NOT_VERIFIED": evidence.lineage_verified,
    }
    reasons = tuple(base.reasons) + tuple(name for name, passed in checks.items() if not passed)
    return CampaignDecision(not reasons, reasons or ("ALL_PREREGISTERED_GATES_PASSED",))


_COMMON_PERIODS = ResearchPeriods(
    datetime(2023, 1, 1, tzinfo=timezone.utc),
    datetime(2025, 6, 30, 23, 59, 59, tzinfo=timezone.utc),
    datetime(2025, 7, 1, tzinfo=timezone.utc),
    datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc),
    datetime(2026, 4, 1, tzinfo=timezone.utc),
    datetime(2026, 9, 21, 23, 59, 59, tzinfo=timezone.utc),
)
_PREREGISTERED_AT = datetime(2026, 9, 22, 21, 55, tzinfo=timezone.utc)
_GATE = StatisticalGate()


def _costs(order_style: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({
        "fee_policy": "use PIT venue/tier fees when available; otherwise floor maker=2bps,taker=5bps",
        "slippage": "half-spread + adverse 2bps floor + square-root participation impact",
        "impact": "coefficient 10bps at 100% participation; exponent 0.5",
        "order_style": order_style,
        "stress": "re-run at 1.0x,1.5x,2.0x base transaction costs; 2.0x must remain net-positive",
    }.items()))


def _universe(min_daily_quote: int) -> tuple[str, ...]:
    return (
        "Binance USD-M, Bybit Linear and OKX SWAP linear crypto perpetuals only",
        "instrument must be ACTIVE/TRADING/OPEN in the R1.3 point-in-time universe at each decision time",
        "minimum 90 calendar days of PIT history before first eligible signal",
        f"rolling 30-day median quote volume must be >= {min_daily_quote} USD equivalent per day",
        "no survivor-only filtering; delisted instruments remain in historical samples until PIT inactive",
        "no post-hoc asset exclusions after campaign results are inspected",
    )


def _validation() -> tuple[str, ...]:
    return (
        "chronological train/validation/locked-OOS periods exactly as preregistered",
        "rolling walk-forward: 180d train, 30d test, 30d step; minimum 6 completed folds",
        "purge observations whose label/information windows overlap test spans; minimum purge 48h",
        "24h embargo after each test span before training observations can re-enter",
        "all parameter selection occurs on training/validation only; locked OOS is single-use",
    )


def _resampling() -> tuple[str, ...]:
    return (
        "circular block bootstrap of net return mean, >=5000 simulations, 95% interval, deterministic campaign seed",
        "block size = max(1, empirically estimated dependence horizon rounded to observations) and fixed before OOS",
        "Monte Carlo trade-path resampling >=5000 simulations with terminal equity and maximum-drawdown distribution",
        "bootstrap lower 95% bound of mean net return must be >0 for promotion",
    )


def _multiplicity() -> tuple[str, ...]:
    return (
        "count every attempted family configuration, abandoned run and parameter combination in the trial ledger",
        "Holm-Bonferroni family-wise alpha=0.05 across primary candidate p-values",
        "White-style family reality check with >=5000 common-index block-bootstrap samples",
        "Deflated Sharpe probability >=0.95 using total family trial count",
        "CSCV/PBO <=0.25 using the complete comparable candidate matrix",
        "research budget cannot be reset after rejection or code refactor",
    )


def _promotion() -> tuple[str, ...]:
    return (
        "all preregistered statistical, leakage, sample, cost, sensitivity and capacity gates pass",
        "locked OOS net mean >0 after fees, slippage, impact, funding and financing",
        "no tuning, universe edits or feature-definition edits after locked OOS is opened",
        "R1.3 dataset manifest, universe fingerprint, FeatureRunManifest fingerprints and experiment manifest are retained",
        "promotion creates a research CANDIDATE only; PAPER/SHADOW evidence is still required and no live authorization is implied",
    )


def _campaign(
    campaign_id: str,
    family: str,
    hypothesis: str,
    features: tuple[str, ...],
    params: tuple[SearchDimension, ...],
    *,
    order_style: str,
    min_daily_quote: int,
    trades: int,
    samples: int,
    extra_rejections: tuple[str, ...],
    extra_sensitivity: tuple[str, ...],
    seed: int,
) -> PreregisteredCampaign:
    return PreregisteredCampaign(
        campaign_id=campaign_id,
        version="1.0.0",
        family=family,
        economic_hypothesis=hypothesis,
        feature_inputs=features,
        parameter_space=params,
        transaction_cost_assumptions=_costs(order_style),
        funding_financing_treatment=(
            "apply realized/known funding cashflows only at their PIT settlement/availability times; never backfill future funding",
            "funding is included in net P&L for every position spanning a funding settlement, regardless of strategy family",
            "quote-cash financing stress: 0%, 5%, and 10% annualized ACT/365; promotion must survive 5%",
            "missing required funding observations fail closed for intervals that cross a settlement rather than assuming zero",
        ),
        universe_rules=_universe(min_daily_quote),
        periods=_COMMON_PERIODS,
        validation_design=_validation(),
        minimum_requirements=tuple(sorted((
            ("bootstrap_simulations", 5000),
            ("independent_samples", samples),
            ("monte_carlo_simulations", 5000),
            ("trades", trades),
        ))),
        rejection_conditions=(
            "any point-in-time leakage, future revision use, universe survivorship leak or lineage mismatch",
            "locked OOS net mean <=0 after all costs/funding/financing",
            "walk-forward positive fraction <2/3 or parameter profitable fraction <2/3",
            "DSR probability <0.95, PBO >0.25, reality-check p>0.05, or multiplicity correction fails",
            "2x transaction-cost stress is not net-positive",
            "bootstrap lower 95% bound of mean net return <=0",
        ) + extra_rejections,
        sensitivity_tests=(
            "one-step perturbation on every numeric parameter and all adjacent categorical choices",
            "full bounded coarse parameter surface; reject isolated optimum islands",
            "transaction costs at 1.0x,1.5x,2.0x",
            "signal latency +100ms,+500ms,+1000ms where source granularity supports it",
            "venue leave-one-out and top-volume-instrument leave-one-out",
            "subperiod and regime slices including high-volatility/high-stress periods",
        ) + extra_sensitivity,
        bootstrap_monte_carlo=_resampling(),
        multiple_testing=_multiplicity(),
        statistical_gate=_GATE,
        capacity_liquidity_constraints=(
            "simulated participation <=1% of observed interval traded quote volume",
            "simulated marketable size <=5% of reconstructed top-5 book notional when L2 is required",
            "candidate rejected if >10% of its gross pre-cost P&L arises from fills that violate liquidity caps",
            "candidate rejected if a single instrument contributes >40% of net OOS P&L without passing leave-one-out robustness",
            "capacity estimate must remain positive after 2x costs and at half the volume-liquidity threshold",
        ),
        promotion_requirements=_promotion(),
        max_parameter_combinations=192,
        max_family_trials=256,
        random_seed=seed,
        preregistered_at=_PREREGISTERED_AT,
    )


def institutional_crypto_perp_campaigns() -> tuple[PreregisteredCampaign, ...]:
    campaigns = (
        _campaign(
            "ALPHA-FUNDING-BASIS-CARRY-001", "funding-basis-carry",
            "Extreme or persistent PIT funding and mark-index basis, conditioned on liquidity and open-interest state, can predict net convergence/carry returns after execution costs and realized funding.",
            ("funding.current_rate", "funding.mean_rate_300s", "funding.change_300s", "basis.mark_index_bps", "basis.change_300s_bps", "open_interest.change_300s_pct", "regime.state", "regime.liquidity_score"),
            (
                SearchDimension("funding_z_entry", "float", 1.0, 3.0, 0.5),
                SearchDimension("basis_abs_entry_bps", "float", 5, 50, 5),
                SearchDimension("holding_hours", "int", 1, 24, 1),
                SearchDimension("exit_fraction", "float", 0.25, 1.0, 0.25),
                SearchDimension("direction", "choice", choices=("convergence", "carry_aligned", "both_predeclared")),
            ),
            order_style="marketable at signal+latency; maker variant is sensitivity only", min_daily_quote=10_000_000,
            trades=150, samples=120,
            extra_rejections=("edge disappears when realized funding cashflows are removed from gross attribution or is explained only by one venue's funding convention",),
            extra_sensitivity=("funding settlement-aligned versus non-settlement entry slices",), seed=1101,
        ),
        _campaign(
            "ALPHA-ORDER-FLOW-MICRO-001", "order-flow-microstructure",
            "Short-horizon PIT order-book and aggressor-flow imbalance, when depth/spread conditions are adequate, can predict next-interval returns net of crossing costs and impact.",
            ("micro.spread_bps", "micro.book_imbalance_l5", "micro.microprice_deviation_bps", "micro.top_depth_notional", "micro.trade_imbalance_60s", "volatility.realized_300s_bps", "regime.liquidity_score"),
            (
                SearchDimension("imbalance_entry", "float", 0.2, 0.8, 0.1),
                SearchDimension("microprice_entry_bps", "float", 0.5, 5.0, 0.5),
                SearchDimension("max_spread_bps", "float", 2, 20, 2),
                SearchDimension("holding_seconds", "int", 30, 600, 30),
                SearchDimension("signal_logic", "choice", choices=("book_only", "trade_only", "agreement")),
            ),
            order_style="marketable with half-spread + impact; passive fill assumptions prohibited in primary test", min_daily_quote=25_000_000,
            trades=500, samples=500,
            extra_rejections=("primary result requires unverified passive queue priority or same-event fill", "book sequence integrity failure exceeds zero in signal windows"),
            extra_sensitivity=("drop top-of-book signals and re-evaluate using trade-flow only", "double feed-to-decision latency"), seed=1201,
        ),
        _campaign(
            "ALPHA-LIQUIDITY-SHOCK-MR-001", "liquidity-shock-mean-reversion",
            "Abrupt PIT spread/depth deterioration accompanied by liquidation or one-sided flow can create temporary price displacement that partially mean-reverts after the shock, net of stressed execution costs.",
            ("micro.spread_bps", "micro.top_depth_notional", "micro.trade_imbalance_60s", "liquidation.notional_60s", "liquidation.imbalance_60s", "momentum.return_60s_bps", "volatility.realized_300s_bps", "regime.stress_score"),
            (
                SearchDimension("shock_z", "float", 2.0, 5.0, 0.5),
                SearchDimension("liquidation_min_usd", "float", 50_000, 2_000_000, 50_000),
                SearchDimension("entry_delay_seconds", "int", 5, 180, 5),
                SearchDimension("holding_minutes", "int", 1, 60, 1),
                SearchDimension("shock_source", "choice", choices=("spread_depth", "liquidation", "combined")),
            ),
            order_style="marketable only after preregistered entry delay; no fills inside the shock event", min_daily_quote=25_000_000,
            trades=300, samples=250,
            extra_rejections=("reversal is absent after excluding the first post-shock executable interval", "effect is driven solely by stale/crossed book states"),
            extra_sensitivity=("exclude liquidation feature entirely", "entry delay 2x and 4x base"), seed=1301,
        ),
        _campaign(
            "ALPHA-MOMENTUM-TREND-001", "momentum-trend",
            "Persistent PIT multi-horizon returns with directional efficiency can continue over intraday horizons, particularly outside stressed liquidity states, after turnover, funding and impact costs.",
            ("momentum.return_60s_bps", "momentum.return_300s_bps", "momentum.efficiency_300s", "volatility.realized_300s_bps", "open_interest.change_300s_pct", "regime.state", "regime.stress_score"),
            (
                SearchDimension("short_return_bps", "float", 5, 100, 5),
                SearchDimension("long_return_bps", "float", 10, 250, 10),
                SearchDimension("efficiency_min", "float", 0.2, 0.9, 0.1),
                SearchDimension("holding_minutes", "int", 5, 240, 5),
                SearchDimension("direction", "choice", choices=("long_short", "long_only", "short_only")),
            ),
            order_style="marketable at next eligible event; turnover costs charged on every rebalance", min_daily_quote=10_000_000,
            trades=200, samples=180,
            extra_rejections=("net effect disappears after excluding the highest-return instrument or month",),
            extra_sensitivity=("remove open-interest conditioning", "exclude TREND regime label and use raw momentum only"), seed=1401,
        ),
        _campaign(
            "ALPHA-VOLATILITY-REGIME-001", "volatility-regime",
            "PIT volatility, liquidity and stress regime state can condition the sign or magnitude of short-horizon expected returns sufficiently to improve a simple directional/flat policy after costs without relying on ex-post regime labels.",
            ("volatility.realized_300s_bps", "volatility.return_std_300s_bps", "volatility.range_300s_bps", "regime.state", "regime.liquidity_score", "regime.stress_score", "momentum.return_300s_bps", "micro.spread_bps"),
            (
                SearchDimension("vol_threshold_bps", "float", 25, 250, 25),
                SearchDimension("stress_max", "float", 0.2, 1.0, 0.1),
                SearchDimension("regime_action", "choice", choices=("trend_in_low_stress", "mean_revert_high_vol", "flat_high_stress")),
                SearchDimension("holding_minutes", "int", 5, 240, 5),
            ),
            order_style="marketable at next eligible event; flat is a valid action and incurs no fabricated return", min_daily_quote=10_000_000,
            trades=200, samples=180,
            extra_rejections=("regime labels use information later than decision time", "benefit is only lower exposure without positive incremental net return versus exposure-matched baseline"),
            extra_sensitivity=("replace discrete regime.state with continuous raw volatility/liquidity features",), seed=1501,
        ),
        _campaign(
            "ALPHA-CROSS-SECTION-PERPS-001", "cross-sectional-crypto-perpetuals",
            "Relative PIT funding, momentum, liquidity and open-interest characteristics across simultaneously eligible perpetuals can rank subsequent cross-sectional net returns after neutralization and realistic rebalance costs.",
            ("funding.current_rate", "basis.mark_index_bps", "momentum.return_300s_bps", "momentum.efficiency_300s", "open_interest.change_300s_pct", "volatility.realized_300s_bps", "micro.spread_bps", "regime.stress_score"),
            (
                SearchDimension("rebalance_minutes", "int", 15, 240, 15),
                SearchDimension("selection_quantile", "float", 0.1, 0.4, 0.05),
                SearchDimension("weighting", "choice", choices=("equal", "inverse_vol", "rank")),
                SearchDimension("neutralization", "choice", choices=("dollar", "venue_dollar", "beta_proxy")),
                SearchDimension("feature_blend", "choice", choices=("momentum", "carry", "liquidity", "predeclared_composite")),
            ),
            order_style="synchronous next-eligible-event rebalance with per-leg costs; no stale cross-venue price synchronization", min_daily_quote=10_000_000,
            trades=300, samples=180,
            extra_rejections=("fewer than 8 simultaneously eligible instruments in >20% of rebalance timestamps", "portfolio result depends on one venue or one instrument for >40% of OOS P&L"),
            extra_sensitivity=("leave-one-venue-out", "leave-one-instrument-out for each top-five P&L contributor", "equal-weight versus inverse-vol versus rank weighting"), seed=1601,
        ),
    )
    return tuple(sorted(campaigns, key=lambda item: item.campaign_id))
