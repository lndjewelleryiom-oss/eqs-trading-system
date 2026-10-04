"""Institutional alpha-campaign preregistration acceptance.

All performance-like evidence in this module is synthetic falsification data.  It is
not evidence of a genuine market edge and cannot promote any empirical strategy.
"""

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from quant_system.data.crypto_perps.research_datasets import ResearchDatasetManifest
from quant_system.evolution import ResearchEvidence
from quant_system.features import FeatureRunManifest
from quant_system.research import (
    AlphaDataBindingError,
    CampaignEvidenceEnvelope,
    CampaignLineageBinding,
    assess_campaign_candidate,
    institutional_crypto_perp_campaigns,
)


def _h(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _dataset() -> ResearchDatasetManifest:
    return ResearchDatasetManifest(
        dataset_id="genuine-r13-placeholder-contract",
        format_version="crypto-perps-research-v1",
        decision_time=datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc),
        start_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
        partition_descriptors=(),
        universe_fingerprint=_h("universe"),
        event_count=0,
        event_identity_hash=_h("events"),
    )


def _feature(dataset: ResearchDatasetManifest, *, suffix: str = "a") -> FeatureRunManifest:
    return FeatureRunManifest(
        engine_version="crypto-perps-feature-engine-v1",
        config_fingerprint=_h("config"),
        registry_fingerprint=_h("registry"),
        input_batch_fingerprint=_h(f"batch-{suffix}"),
        instrument_id="BTC-USDT-PERP:TEST",
        venue="TEST",
        decision_time=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
        input_dataset_fingerprints=(dataset.fingerprint(),),
        universe_version=dataset.universe_fingerprint,
        output_fingerprints=(_h(f"output-{suffix}"),),
    )


def test_six_required_families_are_preregistered_and_unviewed():
    campaigns = institutional_crypto_perp_campaigns()
    assert len(campaigns) == 6
    assert {c.family for c in campaigns} == {
        "funding-basis-carry",
        "order-flow-microstructure",
        "liquidity-shock-mean-reversion",
        "momentum-trend",
        "volatility-regime",
        "cross-sectional-crypto-perpetuals",
    }
    assert all(c.empirical_status == "UNVIEWED" for c in campaigns)
    assert all(c.required_dataset_format == "crypto-perps-research-v1" for c in campaigns)
    assert all(c.required_feature_engine == "crypto-perps-feature-engine-v1" for c in campaigns)


def test_every_campaign_has_bounded_search_costs_validation_and_promotion_contracts():
    for campaign in institutional_crypto_perp_campaigns():
        assert campaign.parameter_space
        assert campaign.max_parameter_combinations <= 192
        assert campaign.max_family_trials <= 256
        assert all(d.kind == "choice" or (d.low is not None and d.high is not None and d.step is not None) for d in campaign.parameter_space)
        assert dict(campaign.minimum_requirements)["trades"] > 0
        assert dict(campaign.minimum_requirements)["independent_samples"] > 0
        assert "stress" in dict(campaign.transaction_cost_assumptions)
        assert any("funding" in item.lower() for item in campaign.funding_financing_treatment)
        assert any("purge" in item.lower() for item in campaign.validation_design)
        assert any("walk-forward" in item.lower() for item in campaign.validation_design)
        assert any("bootstrap" in item.lower() for item in campaign.bootstrap_monte_carlo)
        assert any("Monte Carlo".lower() in item.lower() for item in campaign.bootstrap_monte_carlo)
        assert any("Holm".lower() in item.lower() for item in campaign.multiple_testing)
        assert any("Deflated Sharpe".lower() in item.lower() for item in campaign.multiple_testing)
        assert any("PBO".lower() in item.lower() for item in campaign.multiple_testing)
        assert any("reality check" in item.lower() for item in campaign.multiple_testing)
        assert campaign.capacity_liquidity_constraints
        assert campaign.promotion_requirements


def test_campaign_fingerprint_is_deterministic_and_immutable_write_fails_closed(tmp_path):
    campaign = institutional_crypto_perp_campaigns()[0]
    assert campaign.fingerprint == institutional_crypto_perp_campaigns()[0].fingerprint
    path = tmp_path / "campaign.json"
    campaign.write_immutable(path)
    first = path.read_bytes()
    campaign.write_immutable(path)
    assert path.read_bytes() == first
    record = json.loads(first)
    assert record["campaign_fingerprint"] == campaign.fingerprint
    path.chmod(0o644)
    path.write_text("{}\n")
    with pytest.raises(RuntimeError, match="immutable"):
        campaign.write_immutable(path)


def test_campaign_maps_to_existing_f3_and_f6_precommitted_thresholds():
    campaign = institutional_crypto_perp_campaigns()[0]
    f3 = campaign.statistical_gate.to_validation_thresholds()
    f6 = campaign.to_hypothesis()
    assert f3.min_dsr_probability == campaign.statistical_gate.min_dsr_probability
    assert f3.max_pbo == campaign.statistical_gate.max_pbo
    assert f6.criteria.fingerprint == campaign.statistical_gate.to_acceptance_criteria().fingerprint
    assert str(f6.hypothesis_id) == str(campaign.to_hypothesis().hypothesis_id)
    assert campaign.fingerprint in f6.source_anomaly
    blueprint = campaign.to_strategy_blueprint()
    budget = campaign.research_budget()
    assert blueprint.hypothesis_id == str(f6.hypothesis_id)
    assert any("locked OOS" in item for item in blueprint.falsification_tests)
    assert budget.max_parameter_combinations == campaign.max_parameter_combinations
    assert budget.max_trials_per_hypothesis == campaign.max_family_trials


def test_low_level_r13_feature_lineage_can_be_tested_but_cannot_create_empirical_manifest():
    campaign = institutional_crypto_perp_campaigns()[0]
    dataset = _dataset()
    run = _feature(dataset)
    binding = CampaignLineageBinding._bind_lineage_only(campaign, dataset, (run,))
    assert binding.dataset_manifest_fingerprint == dataset.fingerprint()
    assert binding.universe_fingerprint == dataset.universe_fingerprint
    assert run.fingerprint in binding.feature_run_fingerprints
    assert binding.alpha_data_binding_id is None
    with pytest.raises(AlphaDataBindingError, match="unverified lineage"):
        binding.to_experiment_manifest(
            campaign,
            code_version="test-sha",
            parameters={"selection_quantile": 0.2},
            software_versions={"python": "test"},
        )


def test_public_lineage_binding_requires_validated_alpha_ready_report():
    campaign = institutional_crypto_perp_campaigns()[0]
    dataset = _dataset()
    run = _feature(dataset)
    with pytest.raises(AlphaDataBindingError, match="data-binding report is required"):
        CampaignLineageBinding.bind(campaign, dataset, (run,))


def test_verified_binding_proof_is_carried_into_experiment_manifest(monkeypatch):
    # Structural A01-A28 report validation is covered independently in
    # test_alpha_data_binding_schema.py. This test isolates the enforcement wiring.
    campaign = institutional_crypto_perp_campaigns()[0]
    dataset = _dataset()
    run = _feature(dataset)
    report = {
        "binding_id": _h("binding-id"),
        "final_decision": {
            "ALPHA_DATA_BINDING_READY": True,
            "decision_fingerprint": _h("decision"),
        },
        "campaign_readiness": [{
            "campaign_id": campaign.campaign_id,
            "campaign_fingerprint": campaign.fingerprint,
            "binding_ready": True,
        }],
        "dataset": {
            "manifest_fingerprint": dataset.fingerprint(),
            "dataset_id": dataset.dataset_id,
            "universe_fingerprint": dataset.universe_fingerprint,
        },
        "feature_engine": {
            "run_manifest_fingerprints": [run.fingerprint],
        },
    }
    monkeypatch.setattr(
        "quant_system.research.alpha_binding.validate_alpha_data_binding_report",
        lambda payload: None,
    )
    binding = CampaignLineageBinding.bind(campaign, dataset, (run,), binding_report=report)
    assert binding.alpha_data_binding_id == report["binding_id"]
    assert binding.alpha_data_binding_decision_fingerprint == report["final_decision"]["decision_fingerprint"]
    experiment = binding.to_experiment_manifest(
        campaign,
        code_version="test-sha",
        parameters={"selection_quantile": 0.2},
        software_versions={"python": "test"},
    )
    assert experiment.data_fingerprints["alpha_data_binding_id"] == report["binding_id"]
    assert experiment.data_fingerprints["alpha_data_binding_decision"] == report["final_decision"]["decision_fingerprint"]


def test_lineage_binding_rejects_feature_run_from_another_dataset_or_universe():
    campaign = institutional_crypto_perp_campaigns()[0]
    dataset = _dataset()
    run = _feature(dataset)
    bad = replace(run, input_dataset_fingerprints=(_h("other-dataset"),))
    with pytest.raises(ValueError, match="not lineage-bound"):
        CampaignLineageBinding._bind_lineage_only(campaign, dataset, (bad,))
    bad_universe = replace(run, universe_version=_h("other-universe"))
    with pytest.raises(ValueError, match="universe lineage"):
        CampaignLineageBinding._bind_lineage_only(campaign, dataset, (bad_universe,))


def test_synthetic_positive_fixture_cannot_pass_without_all_preregistered_evidence():
    campaign = institutional_crypto_perp_campaigns()[0]
    synthetic = ResearchEvidence(0.01, 0.90, 0.90, 0.99, 0.10, 0.01, True, True, 1000)
    incomplete = CampaignEvidenceEnvelope(
        research_evidence=synthetic,
        trade_count=10_000,
        independent_sample_count=10_000,
        bootstrap_lower_mean=0.001,
        bootstrap_simulations=100,
        monte_carlo_simulations=100,
        multiple_testing_corrected=False,
        capacity_constraints_passed=False,
        lineage_verified=False,
    )
    decision = assess_campaign_candidate(campaign, incomplete)
    assert not decision.passed
    assert "MULTIPLE_TESTING_CORRECTION_MISSING" in decision.reasons
    assert "LINEAGE_NOT_VERIFIED" in decision.reasons


def test_controlled_synthetic_fixture_can_only_prove_gate_wiring_not_market_edge():
    campaign = institutional_crypto_perp_campaigns()[0]
    minimums = dict(campaign.minimum_requirements)
    synthetic = ResearchEvidence(0.001, 0.80, 0.80, 0.99, 0.10, 0.01, True, True, 1000)
    envelope = CampaignEvidenceEnvelope(
        research_evidence=synthetic,
        trade_count=minimums["trades"],
        independent_sample_count=minimums["independent_samples"],
        bootstrap_lower_mean=0.0001,
        bootstrap_simulations=minimums["bootstrap_simulations"],
        monte_carlo_simulations=minimums["monte_carlo_simulations"],
        multiple_testing_corrected=True,
        capacity_constraints_passed=True,
        lineage_verified=True,
    )
    decision = assess_campaign_candidate(campaign, envelope)
    assert decision.passed
    assert decision.reasons == ("ALL_PREREGISTERED_GATES_PASSED",)


def test_shipped_preregistration_artifacts_match_code_contracts():
    root = __import__("pathlib").Path("research/preregistrations/v1")
    campaigns = institutional_crypto_perp_campaigns()
    for campaign in campaigns:
        record = json.loads((root / f"{campaign.campaign_id}.json").read_text())
        assert record == json.loads(json.dumps(campaign.to_record()))
    index = json.loads((root / "INDEX.json").read_text())
    assert index["campaign_count"] == 6
    assert index["empirical_selection_status"] == "BLOCKED_PENDING_GENUINE_R1_3_DATASET_EVIDENCE"
    assert index["synthetic_results_authorization"] == "NONE"
    assert {row["fingerprint"] for row in index["campaigns"]} == {c.fingerprint for c in campaigns}
