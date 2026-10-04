from decimal import Decimal

import pytest

from executable_cert_support import genuine_fixture_certification
from quant_system.performance.lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)
from quant_system.performance.paper_canary import (
    PaperCanaryAdmissionError,
    PaperCanaryAdmissionLedger,
)


def _validated(tmp_path):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    lifecycle.register(
        strategy_id="s1",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-1",
        research_evidence_sha256="a" * 64,
        research_manifest_sha256="b" * 64,
    )
    lifecycle.transition(
        "s1",
        "v1",
        StrategyLifecycleState.VALIDATED,
        reasons=("GENUINE_RESEARCH_PASS",),
        genuine_research_gate_passed=True,
    )
    return lifecycle


def _gate_and_hashes(tmp_path):
    certifications, cert = genuine_fixture_certification(
        tmp_path, strategy_id="s1", strategy_version="v1"
    )
    gate = PaperCanaryAdmissionLedger(
        tmp_path / "canary.db",
        interface_certifications=certifications,
    )
    return gate, cert.executable_sha256, cert.interface_certification_sha256


def test_validated_genuine_strategy_admits_to_paper_canary(tmp_path):
    lifecycle = _validated(tmp_path)
    gate, executable_sha, certification_sha = _gate_and_hashes(tmp_path)
    admission = gate.admit(
        lifecycle=lifecycle,
        strategy_id="s1",
        strategy_version="v1",
        executable_sha256=executable_sha,
        interface_certification_sha256=certification_sha,
        research_evidence_sha256="a" * 64,
        max_paper_weight=Decimal("0.01"),
        paper_authorised=True,
        broker_submission_enabled=False,
        live_authority=False,
        options_frozen_excluded=True,
    )
    assert admission.max_paper_weight == Decimal("0.01")
    assert lifecycle.get("s1", "v1").state is StrategyLifecycleState.PAPER_CANARY


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"paper_authorised": False}, "PAPER_AUTHORITY_REQUIRED"),
        ({"broker_submission_enabled": True}, "BROKER_SUBMISSION_MUST_BE_DISABLED"),
        ({"live_authority": True}, "LIVE_AUTHORITY_MUST_BE_FALSE"),
        ({"options_frozen_excluded": False}, "OPTIONS_FREEZE_REQUIRED"),
        ({"max_paper_weight": Decimal("0.03")}, "CANARY_WEIGHT_OUT_OF_BOUNDS"),
        ({"research_evidence_sha256": "e" * 64}, "RESEARCH_EVIDENCE_BINDING_MISMATCH"),
        ({"executable_sha256": "c" * 64}, "EXECUTABLE_INTERFACE_CERTIFICATION_INVALID"),
        ({"interface_certification_sha256": "d" * 64}, "EXECUTABLE_INTERFACE_CERTIFICATION_INVALID"),
    ],
)
def test_canary_gate_fails_closed(tmp_path, kwargs, code):
    lifecycle = _validated(tmp_path)
    gate, executable_sha, certification_sha = _gate_and_hashes(tmp_path)
    params = dict(
        lifecycle=lifecycle,
        strategy_id="s1",
        strategy_version="v1",
        executable_sha256=executable_sha,
        interface_certification_sha256=certification_sha,
        research_evidence_sha256="a" * 64,
        max_paper_weight=Decimal("0.01"),
        paper_authorised=True,
        broker_submission_enabled=False,
        live_authority=False,
        options_frozen_excluded=True,
    )
    params.update(kwargs)
    with pytest.raises(PaperCanaryAdmissionError, match=code):
        gate.admit(**params)
    assert lifecycle.get("s1", "v1").state is StrategyLifecycleState.VALIDATED


def test_canary_requires_actual_interface_certification_ledger(tmp_path):
    lifecycle = _validated(tmp_path)
    gate = PaperCanaryAdmissionLedger(tmp_path / "canary.db")
    with pytest.raises(PaperCanaryAdmissionError, match="EXECUTABLE_INTERFACE_CERTIFICATION_REQUIRED"):
        gate.admit(
            lifecycle=lifecycle,
            strategy_id="s1",
            strategy_version="v1",
            executable_sha256="c" * 64,
            interface_certification_sha256="d" * 64,
            research_evidence_sha256="a" * 64,
            max_paper_weight=Decimal("0.01"),
            paper_authorised=True,
            broker_submission_enabled=False,
            live_authority=False,
            options_frozen_excluded=True,
        )
