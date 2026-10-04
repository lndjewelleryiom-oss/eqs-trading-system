from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant_system.backtest.events import BarEvent
from quant_system.research.executable_strategy import (
    DeterministicThresholdFixtureStrategy,
    ExecutableInterfaceCertificationLedger,
    ExecutableStrategyManifest,
)


CAMPAIGN_FINGERPRINT = "3dadf02243e6a7c5176d165450a333ce622c957ca95ae002aa0ef72880e99a94"


def _events() -> tuple[BarEvent, ...]:
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


def genuine_fixture_certification(tmp_path: Path, *, strategy_id: str, strategy_version: str):
    ledger = ExecutableInterfaceCertificationLedger(tmp_path / f"{strategy_id}-exec-certs.db")

    def factory():
        return DeterministicThresholdFixtureStrategy(
            strategy_id=strategy_id,
            symbol="FIXTURE-USD",
            quantity=Decimal("2"),
            entry_above=Decimal("103"),
            exit_below=Decimal("99"),
        )

    manifest = ExecutableStrategyManifest(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        family="TEST_FIXTURE_ONLY",
        campaign_fingerprint=CAMPAIGN_FINGERPRINT,
        implementation_id="deterministic-threshold-fixture-v1",
        parameters={"quantity": "2", "entry_above": "103", "exit_below": "99"},
        feature_ids=("bar.close",),
        instrument_ids=("FIXTURE-USD",),
        allowed_modes=("RESEARCH_REPLAY", "INTERNAL_PAPER", "PAPER_CANARY"),
        warmup_events=0,
        max_event_age_seconds=300,
        state_schema_version=1,
        source_class="GENUINE",
    )
    cert = ledger.certify(manifest=manifest, factory=factory, events=_events())
    return ledger, cert
