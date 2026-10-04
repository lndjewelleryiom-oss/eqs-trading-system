from pathlib import Path

import pytest

from quant_system.data.infrastructure_gate import (
    DatasetConsumer,
    InfrastructureDatasetBoundary,
    InfrastructureDatasetGateError,
)


MANIFEST = Path(
    r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925"
    r"\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json"
)


def test_frozen_manifest_loads_fail_closed():
    boundary = InfrastructureDatasetBoundary.load(MANIFEST)
    assert len(boundary.fingerprint) == 64
    assert boundary.payload["policy"]["strict_pit_alpha_oos_admitted"] is False
@pytest.mark.parametrize(
    "consumer",
    [
        DatasetConsumer.FEATURE_ENGINE,
        DatasetConsumer.REPLAY,
        DatasetConsumer.PAPER,
        DatasetConsumer.SHADOW,
        DatasetConsumer.TERMINAL,
    ],
)
def test_infrastructure_consumers_are_admitted(consumer):
    InfrastructureDatasetBoundary.load(MANIFEST).authorize(consumer)


@pytest.mark.parametrize(
    "consumer",
    [DatasetConsumer.ALPHA, DatasetConsumer.OOS, DatasetConsumer.LIVE, DatasetConsumer.BROKER],
)
def test_research_and_live_consumers_are_rejected(consumer):
    with pytest.raises(InfrastructureDatasetGateError):
        InfrastructureDatasetBoundary.load(MANIFEST).authorize(consumer)
def test_explicit_exclusions_are_exposed():
    boundary = InfrastructureDatasetBoundary.load(MANIFEST)
    scopes = {item["scope"] for item in boundary.explicit_exclusions}
    assert "BYBIT strict Alpha/OOS" in scopes
    assert "OKX strict Alpha/OOS" in scopes


def test_unadmitted_record_fails_closed():
    boundary = InfrastructureDatasetBoundary.load(MANIFEST)
    with pytest.raises(InfrastructureDatasetGateError):
        boundary.admitted_record("BYBIT", "not-a-real-record")
