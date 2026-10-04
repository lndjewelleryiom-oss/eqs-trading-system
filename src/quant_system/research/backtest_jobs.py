from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from quant_system.operations.storage_guard import StorageGuardState, evaluate_storage_guard
from quant_system.research.replay_fixture import run_fixture_replay
from quant_system.research.run_registry import StrategyRunRegistry


ALLOWED_REQUEST_KEYS = {
    "schema_version", "idempotency_key", "strategy_id", "strategy_version",
    "engine", "mode", "source_id", "instrument_id", "parameters",
    "data_manifest_ref", "data_manifest_hash", "historical_start",
    "historical_end", "cost_model_ref", "cost_model_hash",
    "risk_config_ref", "risk_config_hash", "trial_id", "random_seed",
    "resource_budget",
}
FORBIDDEN_KEY_FRAGMENTS = ("password", "secret", "token", "api_key", "credential", "broker")


OPERATIONAL_OOS_EVENT_TYPES = {
    "RUN_STARTED", "RUN_PROGRESS", "HEARTBEAT", "RUN_BLOCKED",
    "RUN_FAILED", "RUN_PAUSED", "RUN_FINISHED",
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return keys


def validate_request(request: dict[str, Any]) -> None:
    unknown = sorted(set(request) - ALLOWED_REQUEST_KEYS)
    if unknown:
        raise ValueError("unsupported request fields: " + ",".join(unknown))
    for key in _walk_keys(request):
        lowered = key.lower()
        if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError("credentials and broker connectivity are forbidden in research requests")
    required = {
        "schema_version", "idempotency_key", "strategy_id", "strategy_version",
        "engine", "mode", "source_id", "instrument_id", "parameters",
    }
    missing = sorted(required - set(request))
    if missing:
        raise ValueError("missing required fields: " + ",".join(missing))
    if request["schema_version"] != "EQS-BACKTEST-JOB-REQUEST-V1":
        raise ValueError("unsupported request schema")
    if request["engine"] != "EQS_NATIVE":
        raise ValueError("only EQS_NATIVE is commissioned in preparation scope")
    if request["mode"] != "RESEARCH_REPLAY":
        raise ValueError("backtest jobs must use RESEARCH_REPLAY")
    if request["strategy_id"] != "fixture.deterministic.v1":
        raise ValueError("strategy package is not registered for preparation replay")
    if request["source_id"] != "DETERMINISTIC_FIXTURE":
        raise ValueError("preparation replay requires the deterministic fixture source")
    if not isinstance(request["parameters"], dict):
        raise ValueError("parameters must be an object")
    budget = request.get("resource_budget") or {}
    if budget and not isinstance(budget, dict):
        raise ValueError("resource_budget must be an object")
    if isinstance(budget, dict):
        max_output = int(budget.get("max_output_events", 5000))
        if max_output <= 0 or max_output > 5000:
            raise ValueError("max_output_events exceeds preparation scope")


class BacktestJobService:
    """Bounded non-broker J51 job service.

    Only registered server-side strategies can execute. Current preparation scope
    uses the deterministic fixture and the canonical EQS simulator.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        registry: StrategyRunRegistry | None = None,
        storage_guard: Callable[[str | Path], StorageGuardState] = evaluate_storage_guard,
        max_workers: int = 1,
    ):
        self.root = Path(root).resolve()
        self.registry = registry or StrategyRunRegistry(
            self.root / "artifacts" / "research" / "strategy_runs" / "research_runs.db"
        )
        self.storage_guard = storage_guard
        if max_workers != 1:
            raise ValueError("preparation scope requires exactly one research worker")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eqs-backtest")
        self._futures: dict[str, Future[dict[str, object]]] = {}
        self._lock = Lock()

    @staticmethod
    def _run_id(request: dict[str, Any]) -> str:
        request_hash = sha256(_canonical(request)).hexdigest()
        key_hash = sha256(str(request["idempotency_key"]).encode("utf-8")).hexdigest()
        return "bt-" + key_hash[:12] + "-" + request_hash[:12]

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        validate_request(request)
        run_id = self._run_id(request)
        idempotency_key = str(request["idempotency_key"])
        request_hash = sha256(_canonical(request)).hexdigest()
        existing_request = self.registry.get_backtest_request(idempotency_key)
        if existing_request is not None:
            if existing_request["request_hash"] != request_hash:
                raise ValueError("idempotency key reused with different request")
            existing_run = self.registry.get_run(str(existing_request["run_id"]))
            return {
                "job_id": existing_run["run_id"],
                "run_id": existing_run["run_id"],
                "idempotent_replay": True,
                "job_status": existing_run["status"],
                "evidence_result": existing_run["result"],
            }

        self.registry.create_run(
            run_id=run_id,
            strategy_id=str(request["strategy_id"]),
            strategy_version=str(request["strategy_version"]),
            mode="RESEARCH_REPLAY",
            source_id=str(request["source_id"]),
            instrument_id=str(request["instrument_id"]),
            parameters=dict(request["parameters"]),
            dataset_hash=request.get("data_manifest_hash"),
            config_hash=sha256(_canonical(request["parameters"])).hexdigest(),
            rationale="J51 bounded research job; no broker connectivity.",
            fill_provenance="SIMULATED",
        )

        admitted_id, created = self.registry.admit_backtest_request(
            idempotency_key=idempotency_key,
            run_id=run_id,
            request=request,
        )
        if admitted_id != run_id or not created:
            raise RuntimeError("idempotency ledger admission mismatch")

        run = self.registry.get_run(run_id)

        guard = self.storage_guard(self.root)
        if not guard.allow_new_research:
            self.registry.set_status(run_id, "WAITING_RESOURCE", result="BLOCKED:" + guard.state)
            self.registry.append_event(
                run_id,
                "RUN_BLOCKED",
                market_time_utc=None,
                payload={
                    "reason": guard.state,
                    "detail": guard.reason,
                    "free_bytes": guard.free_bytes,
                    "reserve_bytes": guard.reserve_bytes,
                    "broker_submission_enabled": False,
                    "live_authority": False,
                },
            )
            return {
                "job_id": run_id,
                "run_id": run_id,
                "idempotent_replay": False,
                "job_status": "WAITING_RESOURCE",
                "evidence_result": "BLOCKED",
                "blocker": guard.state,
                "storage_guard": asdict(guard),
            }

        future = self.executor.submit(run_fixture_replay, self.registry, run_id)
        with self._lock:
            self._futures[run_id] = future
        return {
            "job_id": run_id,
            "run_id": run_id,
            "idempotent_replay": False,
            "job_status": "QUEUED",
            "evidence_result": None,
        }

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.registry.get_run(run_id)
        sealed = run.get("visibility_class") == "SEALED_OOS_OPERATIONAL_ONLY"
        future_state = None
        with self._lock:
            future = self._futures.get(run_id)
        if future is not None:
            future_state = "DONE" if future.done() else "RUNNING"
        return {
            "job_id": run_id,
            "run_id": run_id,
            "job_status": run["status"],
            "evidence_result": run["result"],
            "event_count": run["event_count"],
            "last_sequence": run["last_sequence"],
            "worker_state": future_state,
            "broker_submission_enabled": run["broker_submission_enabled"],
            "live_authority": run["live_authority"],
        }

    def events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> dict[str, Any]:
        rows = self.registry.events(run_id, after=after, limit=limit)
        return {
            "run_id": run_id,
            "after": after,
            "events": rows,
            "next_cursor": rows[-1]["sequence"] if rows else after,
        }

    def results(self, run_id: str) -> dict[str, Any]:
        run = self.registry.get_run(run_id)
        events = self.registry.events(run_id, after=0, limit=5000)
        fills = [e for e in events if e["event_type"] in {"FILL", "PARTIAL_FILL"}]
        trades = [e for e in events if e["event_type"] == "TRADE_CLOSED"]
        equity = [e for e in events if e["event_type"] == "EQUITY_UPDATE"]
        return {
            "run": run,
            "hash_chain_valid": self.registry.verify_chain(run_id),
            "fills": fills,
            "trades": trades,
            "equity": equity,
            "historical_replay": True,
            "counts_toward_168h_100_trade_gate": False,
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.registry.get_run(run_id)
        if run["status"] not in {"QUEUED", "WAITING_RESOURCE"}:
            raise ValueError("only queued or resource-blocked jobs can be cancelled safely")
        with self._lock:
            future = self._futures.get(run_id)
        if future is not None and not future.cancel():
            raise ValueError("job has already started and cannot be cancelled by this adapter")
        self.registry.set_status(run_id, "CANCELLED", result="CANCELLED_BY_OPERATOR")
        return self.status(run_id)

    def wait(self, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            future = self._futures.get(run_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.status(run_id)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
