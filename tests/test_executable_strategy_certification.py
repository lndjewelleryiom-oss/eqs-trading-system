from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_system.backtest.events import BarEvent
from quant_system.research.executable_strategy import (
    DeterministicThresholdFixtureStrategy,
    ExecutableInterfaceCertificationLedger,
    ExecutableStrategyError,
    ExecutableStrategyManifest,
)


CAMPAIGN = "3dadf02243e6a7c5176d165450a333ce622c957ca95ae002aa0ef72880e99a94"


def events() -> tuple[BarEvent, ...]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = ("100", "102", "104", "103", "99", "98")
    return tuple(
        BarEvent(
            symbol="FIXTURE-USD",
            timestamp=start + timedelta(minutes=i),
            open=Decimal(px),
            high=Decimal(px) + Decimal("1"),
            low=Decimal(px) - Decimal("1"),
            close=Decimal(px),
            volume=Decimal("1000"),
        )
        for i, px in enumerate(closes)
    )


def manifest(*, source_class: str = "SYNTHETIC_MECHANICS") -> ExecutableStrategyManifest:
    return ExecutableStrategyManifest(
        strategy_id="fixture.threshold",
        strategy_version="v1",
        family="momentum-trend",
        campaign_fingerprint=CAMPAIGN,
        implementation_id="deterministic-threshold-fixture-v1",
        parameters={
            "quantity": "2",
            "entry_above": "103",
            "exit_below": "99",
        },
        feature_ids=("bar.close",),
        instrument_ids=("FIXTURE-USD",),
        allowed_modes=("RESEARCH_REPLAY", "INTERNAL_PAPER", "PAPER_CANARY"),
        warmup_events=0,
        max_event_age_seconds=300,
        state_schema_version=1,
        source_class=source_class,
    )


def factory():
    return DeterministicThresholdFixtureStrategy(
        strategy_id="fixture.threshold",
        symbol="FIXTURE-USD",
        quantity=Decimal("2"),
        entry_above=Decimal("103"),
        exit_below=Decimal("99"),
    )


def test_certification_binds_source_config_determinism_restart_and_no_broker(tmp_path: Path) -> None:
    ledger = ExecutableInterfaceCertificationLedger(tmp_path / "certs.db")
    cert = ledger.certify(manifest=manifest(), factory=factory, events=events())

    assert len(cert.executable_sha256) == 64
    assert len(cert.interface_certification_sha256) == 64
    assert cert.deterministic_replay
    assert cert.restart_equivalent
    assert cert.duplicate_or_nonmonotonic_rejected
    assert cert.no_broker_boundary
    assert cert.test_event_count == len(events())
    assert ledger.verify_binding(
        strategy_id="fixture.threshold",
        strategy_version="v1",
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
    )


def test_substituted_executable_or_strategy_identity_is_rejected(tmp_path: Path) -> None:
    ledger = ExecutableInterfaceCertificationLedger(tmp_path / "certs.db")
    cert = ledger.certify(manifest=manifest(), factory=factory, events=events())

    assert not ledger.verify_binding(
        strategy_id="other",
        strategy_version="v1",
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
    )
    assert not ledger.verify_binding(
        strategy_id="fixture.threshold",
        strategy_version="v1",
        executable_sha256="f" * 64,
        interface_certification_sha256=cert.interface_certification_sha256,
    )
    assert not ledger.verify_binding(
        strategy_id="fixture.threshold",
        strategy_version="v2",
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
    )


def test_source_class_can_be_required_at_admission_boundary(tmp_path: Path) -> None:
    ledger = ExecutableInterfaceCertificationLedger(tmp_path / "certs.db")
    cert = ledger.certify(manifest=manifest(source_class="SYNTHETIC_MECHANICS"), factory=factory, events=events())
    assert not ledger.verify_binding(
        strategy_id="fixture.threshold",
        strategy_version="v1",
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
        require_source_class="GENUINE",
    )


def test_live_or_broker_capable_manifest_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="allowed_modes"):
        ExecutableStrategyManifest(
            strategy_id="x",
            strategy_version="1",
            family="x",
            campaign_fingerprint=CAMPAIGN,
            implementation_id="x",
            parameters={},
            feature_ids=(),
            instrument_ids=("X",),
            allowed_modes=("LIVE",),
            warmup_events=0,
            max_event_age_seconds=1,
            state_schema_version=1,
            source_class="SYNTHETIC_MECHANICS",
        )

    with pytest.raises(ValueError, match="broker"):
        ExecutableStrategyManifest(
            strategy_id="x",
            strategy_version="1",
            family="x",
            campaign_fingerprint=CAMPAIGN,
            implementation_id="x",
            parameters={},
            feature_ids=(),
            instrument_ids=("X",),
            allowed_modes=("RESEARCH_REPLAY",),
            warmup_events=0,
            max_event_age_seconds=1,
            state_schema_version=1,
            source_class="SYNTHETIC_MECHANICS",
            broker_submission_enabled=True,
        )


def test_nondeterministic_or_nonmonotonic_strategy_cannot_certify(tmp_path: Path) -> None:
    class BadStrategy:
        def __init__(self):
            self.i = 0

        def on_event(self, event):
            self.i += 1
            return None

        def export_state(self):
            return {"i": self.i}

        def restore_state(self, state):
            self.i = state["i"]

    ledger = ExecutableInterfaceCertificationLedger(tmp_path / "certs.db")
    with pytest.raises(ExecutableStrategyError, match="NONMONOTONIC"):
        ledger.certify(manifest=manifest(), factory=BadStrategy, events=events())
