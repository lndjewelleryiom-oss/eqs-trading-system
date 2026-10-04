from __future__ import annotations

from pathlib import Path
import re
import sys


FORBIDDEN = ("alpha", "oos", "live", "broker")
REQUIRED_ALLOWED = ("feature_engine", "replay", "paper", "shadow", "terminal")


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    gate = root / "src" / "quant_system" / "data" / "infrastructure_gate.py"
    broker = root / "src" / "quant_system" / "execution" / "broker.py"
    commissioning = root / "src" / "quant_system" / "commissioning" / "submission.py"
    e2e = root / "tests" / "test_infrastructure_boundary_e2e.py"

    missing = [str(p) for p in (gate, broker, commissioning, e2e) if not p.exists()]
    if missing:
        raise SystemExit("INFRASTRUCTURE_BOUNDARY_CI_FAIL missing: " + ", ".join(missing))

    gate_text = gate.read_text(encoding="utf-8").lower()
    broker_text = broker.read_text(encoding="utf-8")
    commissioning_text = commissioning.read_text(encoding="utf-8")
    e2e_text = e2e.read_text(encoding="utf-8")
    for consumer in REQUIRED_ALLOWED + FORBIDDEN:
        if consumer not in gate_text:
            raise SystemExit(f"INFRASTRUCTURE_BOUNDARY_CI_FAIL missing consumer policy: {consumer}")

    hard_reason = "INFRASTRUCTURE_ONLY_DATASET_BLOCKED"
    if hard_reason not in broker_text or hard_reason not in commissioning_text:
        raise SystemExit("INFRASTRUCTURE_BOUNDARY_CI_FAIL hard broker/commissioning block removed")

    required_e2e = (
        "DatasetConsumer.LIVE",
        "InfrastructureDatasetGateError",
        "venue_submission_enabled=True",
        hard_reason,
        "adapter.submitted == []",
        "live_adapter.submitted == []",
    )
    for token in required_e2e:
        if token not in e2e_text:
            raise SystemExit(f"INFRASTRUCTURE_BOUNDARY_CI_FAIL E2E assertion missing: {token}")

    print("INFRASTRUCTURE_BOUNDARY_CI_PASS: Alpha/OOS/LIVE/broker fail-closed assertions present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
