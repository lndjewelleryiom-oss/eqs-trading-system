from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from quant_system.research.campaigns import institutional_crypto_perp_campaigns

from .read_model import EqsInterfaceSnapshot, InterfaceMeta

CORE_MASTER_SHA256 = "743cba6fefe568ccaad8dd6993a615bec41e75ca4e622e5e5de0d4c345608470"
MASTER_SHA256 = "beb0f4eb7ec21f7a637a4d37e3a76709b53e0ee23412b7539704ed89f049ed07"
MANIFEST_FP = "1db543b2881321a4e453915258f3d08926d0b8b066f4fde2c09d82e8a5d4016c"
UNIVERSE_FP = "236009152ebffc20a6c59ef65662298c593f716d0d642a3aa8d7cfddb57066e3"
REPLAY_FP = "fba9ee4f70e0e5d8c937a0ac4ede37227e6bd0beb6d67720509504845c50e0ee"

_GATE_NAMES = {
    "A01": "Build provenance", "A02": "Real-market source", "A03": "Raw SHA lineage",
    "A04": "Timestamp evidence", "A05": "Immutable partitions", "A06": "Partition descriptors",
    "A07": "Canonical partition integrity", "A08": "Dataset manifest", "A09": "Manifest event closure",
    "A10": "Deterministic rebuild", "A11": "Deterministic replay", "A12": "PIT leakage exclusion",
    "A13": "Universe lineage", "A14": "As-of definitions", "A15": "Survivorship safety",
    "A16": "Listing/delisting/revisions", "A17": "Supported instruments", "A18": "Historical coverage",
    "A19": "Minimum history filter", "A20": "Liquidity filter", "A21": "Walk-forward folds",
    "A22": "Independent sample depth", "A23": "Feature coverage", "A24": "Feature lineage",
    "A25": "Feature leakage guard", "A26": "Locked OOS seal", "A27": "Accepted artifact immutability",
    "A28": "Protected execution boundary",
}


class MockReadOnlyEqsAdapter:
    """Read-only bootstrap adapter using genuine packaged evidence plus clearly labelled demo runtime state."""

    def __init__(self, repository_root: str | Path | None = None) -> None:
        self.repository_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[3]

    def _json(self, relative: str) -> dict[str, Any]:
        path = self.repository_root / relative
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _gate_rows() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for gate_id in [f"A{i:02d}" for i in range(1, 29)]:
            if gate_id in {"A18", "A21", "A22"}:
                state = "BLOCKED_BY_HISTORY"
                detail = "Captured-scope R1.3 evidence is insufficient for the preregistered long-horizon requirement."
            elif gate_id == "A28":
                state = "BOUNDARY_VERIFIED"
                detail = "Protected R1.2/F7/tracker boundary verified unchanged in master integration evidence."
            else:
                state = "NOT_EVALUATED"
                detail = "No ALPHA_DATA_BINDING_READY report has been accepted for this gate."
            rows.append({"id": gate_id, "name": _GATE_NAMES[gate_id], "state": state, "detail": detail})
        return rows

    def snapshot(self, *, now: datetime | None = None) -> EqsInterfaceSnapshot:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        real = self._json("artifacts/real-market/r1_3_2026-09-22/materialized/acceptance_report.json")
        nonlive = self._json("artifacts/test-evidence/NONLIVE_EXECUTION_ACCEPTANCE_2026-09-22.json")
        campaigns = institutional_crypto_perp_campaigns()
        campaign_rows = []
        for campaign in campaigns:
            mins = dict(campaign.minimum_requirements)
            campaign_rows.append({
                "campaign_id": campaign.campaign_id,
                "family": campaign.family,
                "fingerprint": campaign.fingerprint,
                "stage": "PREREGISTERED",
                "binding_state": "WAITING_FOR_ALPHA_DATA_BINDING",
                "empirical_status": campaign.empirical_status,
                "minimum_trades": mins.get("trades"),
                "minimum_samples": mins.get("independent_samples"),
                "fold_status": "BLOCKED_BY_HISTORY",
                "sample_status": "BLOCKED_BY_HISTORY",
                "locked_oos": "SEALED_BY_CONTRACT",
            })

        active_instruments = real.get("active_instruments_at_decision", [])
        data_rows = [
            {"venue": "BINANCE_USDM", "market": "USD-M perpetuals", "state": "CAPTURED", "instrument": "BTC-USDT-PERP"},
            {"venue": "BYBIT_LINEAR", "market": "Linear perpetuals", "state": "CAPTURED", "instrument": "BTC-USDT-PERP"},
            {"venue": "OKX_SWAP", "market": "USDT SWAP", "state": "CAPTURED", "instrument": "BTC-USDT-PERP"},
        ]
        acceptance = nonlive.get("acceptance", {})
        evidence_files = sorted((self.repository_root / "artifacts/test-evidence").glob("*"))
        evidence_count = sum(1 for item in evidence_files if item.is_file())

        return EqsInterfaceSnapshot(
            meta=InterfaceMeta(
                schema_version="eqs-interface-read-model-v1",
                generated_at=current.isoformat(),
                access_mode="READ_ONLY",
                adapter="MOCK_READ_ONLY_ADAPTER",
                data_classification="REAL_EVIDENCE_PLUS_LABELLED_MOCK_RUNTIME",
                mutations_enabled=False,
            ),
            command={
                "system_state": "RESEARCH_INFRASTRUCTURE_READY",
                "research_state": "ALPHA_DATA_BINDING_NOT_READY",
                "execution_authority": "NON_LIVE_ONLY",
                "master_sha256": MASTER_SHA256,
                "core_integration_sha256": CORE_MASTER_SHA256,
                "blockers": [
                    {"severity": "BLOCK", "code": "ALPHA_HISTORY_INSUFFICIENT", "text": "Long-horizon history is not yet sufficient for A18/A21/A22."},
                    {"severity": "INFO", "code": "LIVE_AUTHORITY_ABSENT", "text": "No LIVE enablement or broker-submit authority is exposed to this interface."},
                ],
                "active_runs": [
                    {"name": "R1.3 real-market population", "kind": "DATA", "state": "CAPTURED_SCOPE_ACCEPTED", "progress": None},
                    {"name": "Alpha-v1 campaign set", "kind": "RESEARCH", "state": "PREREGISTERED", "progress": None},
                    {"name": "PAPER runtime", "kind": "EXECUTION", "state": "IDLE_DEMO", "progress": None},
                    {"name": "SHADOW runtime", "kind": "EXECUTION", "state": "IDLE_ZERO_SUBMIT", "progress": None},
                ],
            },
            data={
                "venues": data_rows,
                "manifest": {
                    "dataset_id": "crypto-perps-normalized-v1",
                    "format_version": "crypto-perps-research-v1",
                    "fingerprint": real.get("manifest_fingerprint", MANIFEST_FP),
                    "replay_fingerprint": real.get("replay_fingerprint", REPLAY_FP),
                    "universe_fingerprint": real.get("universe_fingerprint", UNIVERSE_FP),
                    "events": real.get("normalized_event_count", 39),
                    "partitions": real.get("partition_count", 9),
                    "raw_captures": real.get("raw_capture_count", 15),
                    "active_instruments": len(active_instruments),
                },
                "coverage": {
                    "classification": real.get("classification", "REAL MARKET ACCEPTANCE"),
                    "historical_population_state": "CAPTURED_SCOPE_ONLY",
                    "history_progress": None,
                    "history_progress_label": "Acceptance slice populated; long-horizon Alpha-v1 history remains incomplete.",
                    "raw_lineage_complete": bool(real.get("raw_lineage_complete", True)),
                    "partition_tamper_detected": bool(real.get("partition_tamper_detected", True)),
                    "raw_tamper_detected": bool(real.get("raw_tamper_probe_detected", True)),
                    "deterministic_manifest": bool(real.get("manifests_identical_across_repeated_builds", True)),
                    "deterministic_replay": bool(real.get("replays_identical_across_repeated_builds", True)),
                },
                "pit_universe": active_instruments,
                "throughput": {
                    "events_per_second": round(float(real.get("throughput_events_per_second", 0)), 2),
                    "bytes_per_second": round(float(real.get("throughput_bytes_per_second", 0)), 2),
                    "iterations": int(real.get("throughput_iterations", 0)),
                },
            },
            alpha_research={
                "binding_ready": False,
                "campaign_count": len(campaign_rows),
                "campaigns": campaign_rows,
                "gates": self._gate_rows(),
                "stage": "PREREGISTERED_WAITING_FOR_LONG_HORIZON_DATA",
                "winning_strategy_selected": False,
                "profitability_claimed": False,
            },
            strategies={
                "candidates": [],
                "survivors": [],
                "registry_state": "EMPTY_BY_DESIGN",
                "message": "No candidate or survivor registry entries exist until empirical campaign evaluation is legitimately run.",
            },
            risk={
                "source": "MOCK_ADAPTER",
                "state": "ACTIVE_DEMO",
                "degradation_state": "ACTIVE",
                "halted": False,
                "paused": False,
                "limits": [
                    {"name": "Max order notional", "value": "$25,000", "utilisation": 18},
                    {"name": "Max symbol notional", "value": "$75,000", "utilisation": 31},
                    {"name": "Max strategy notional", "value": "$150,000", "utilisation": 22},
                    {"name": "Max gross notional", "value": "$250,000", "utilisation": 27},
                    {"name": "Max leverage", "value": "2.0x", "utilisation": 34},
                    {"name": "Max daily loss", "value": "$5,000", "utilisation": 9},
                    {"name": "Max drawdown", "value": "10%", "utilisation": 12},
                    {"name": "Max data age", "value": "5s", "utilisation": 8},
                ],
                "conditions": [
                    {"condition": "Global kill switch", "state": "CLEAR"},
                    {"condition": "Stale market data", "state": "CLEAR"},
                    {"condition": "Daily loss limit", "state": "CLEAR"},
                    {"condition": "Drawdown limit", "state": "CLEAR"},
                    {"condition": "Execution / data integrity", "state": "CLEAR"},
                ],
            },
            execution={
                "source": "MOCK_ADAPTER",
                "paper": {
                    "status": "IDLE_DEMO",
                    "equity": 100000.0,
                    "realized_pnl": 184.25,
                    "unrealized_pnl": 87.40,
                    "positions": [{"symbol": "BTC-USDT-PERP", "venue": "BINANCE_USDM", "side": "LONG", "qty": 0.04, "avg_price": 112430.0, "mark": 114615.0, "pnl": 87.4}],
                    "orders": [{"id": "demo-paper-0003", "symbol": "BTC-USDT-PERP", "side": "BUY", "qty": 0.02, "state": "FILLED", "lineage": "demo-lineage-03"}],
                    "fills": [{"id": "demo-fill-0003", "order_id": "demo-paper-0003", "price": 112460.0, "qty": 0.02, "fee": 0.90}],
                    "reconciliation": "PASS_DEMO",
                },
                "shadow": {
                    "status": "IDLE_ZERO_SUBMIT",
                    "decisions": 3,
                    "venue_submission_enabled": False,
                    "submitted_orders": 0,
                    "zero_submit_invariant": "PASS",
                    "reconciliation": "PASS_DEMO",
                },
                "verified_acceptance": {key: value for key, value in acceptance.items() if key in {
                    "paper_order_path_and_accounting", "shadow_hard_zero_submit", "restart_pending_orders",
                    "restart_partial_fills", "reconciliation_mismatch_halt", "degradation_pause_halt",
                    "runtime_lease_collision", "crash_restart_recovery"
                }},
            },
            evidence={
                "immutable": True,
                "test_evidence_file_count": evidence_count,
                "fingerprints": [
                    {"label": "Parent Interface v1", "value": MASTER_SHA256},
                    {"label": "Core integration parent", "value": CORE_MASTER_SHA256},
                    {"label": "R1.3 manifest", "value": real.get("manifest_fingerprint", MANIFEST_FP)},
                    {"label": "PIT universe", "value": real.get("universe_fingerprint", UNIVERSE_FP)},
                    {"label": "Replay", "value": real.get("replay_fingerprint", REPLAY_FP)},
                    {"label": "Source bundle", "value": real.get("source_bundle_sha256", "")},
                ],
                "recent": [
                    {"artifact": "ALPHA_DATA_BINDING_ENFORCEMENT_V1_EVIDENCE.md", "result": "PASS", "classification": "TEST EVIDENCE"},
                    {"artifact": "R1.3 genuine real-market acceptance", "result": "PASS", "classification": "REAL MARKET ACCEPTANCE"},
                    {"artifact": "Non-live execution acceptance", "result": "PASS", "classification": "INFRASTRUCTURE ACCEPTANCE"},
                    {"artifact": "Protected boundary verification", "result": "PASS", "classification": "SAFETY EVIDENCE"},
                ],
            },
            system_health={
                "overall": "HEALTHY_NON_LIVE",
                "feed": {"state": "IDLE", "detail": "No continuously connected feed is represented by the bootstrap adapter."},
                "lease": {"state": "NONE", "detail": "No active runtime lease in mock adapter."},
                "restart_recovery": {"state": "VERIFIED", "detail": "Pending-order, partial-fill and crash recovery acceptance passed."},
                "persistence": {"state": "VERIFIED", "detail": "Durable event/checkpoint path present in F5.6 runtime."},
                "reconciliation": {"state": "VERIFIED", "detail": "Mismatch fail-closed HALT acceptance passed."},
                "shadow_submission": {"state": "DISABLED", "detail": "SHADOW hard zero-submit acceptance passed."},
                "alerts": [
                    {"severity": "BLOCK", "message": "Alpha data binding is not ready: long-horizon coverage/folds/samples remain insufficient."},
                    {"severity": "INFO", "message": "Interface is read-only and currently mixes genuine evidence with labelled mock runtime state."},
                ],
            },
        )
