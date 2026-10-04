from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_system.interface.public_market import PublicMarkets, MultiVenuePublicMarkets
from quant_system.interface.valuation import value_portfolio
from quant_system.interface.dashboard import OperatorDashboard
from quant_system.interface import LiveReadOnlyEqsAdapter, MockReadOnlyEqsAdapter  # noqa: E402
from quant_system.research.run_reader import StrategyRunReadError, StrategyRunReadOnlyReader


def build_adapter(runtime_db: str | Path | None = None):
    selected = runtime_db or os.environ.get("EQS_RUNTIME_DB")
    if selected:
        return LiveReadOnlyEqsAdapter(selected, ROOT)
    return MockReadOnlyEqsAdapter(ROOT)


class ReadOnlyInterfaceHandler(SimpleHTTPRequestHandler):
    markets = PublicMarkets()
    venue_markets = MultiVenuePublicMarkets()
    adapter = build_adapter()
    dashboard = OperatorDashboard(ROOT, os.environ.get("EQS_RUNTIME_DB"))
    strategy_runs = StrategyRunReadOnlyReader(
        ROOT / "artifacts" / "research" / "strategy_runs" / "research_runs.db"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "interface"), **kwargs)

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/markets":
            try:
                venue=params.get('venue',[None])[0]
                if venue:
                    self._send_json(self.venue_markets.snapshot(venue, params.get('symbol',['BTCUSDT'])[0], params.get('interval',['1m'])[0]))
                else:
                    self._send_json(self.markets.snapshot(params.get('symbol',['BTCUSDT'])[0], params.get('interval',['1m'])[0]))
            except ValueError as exc:
                self._send_json({'error':str(exc)},HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/strategy-runs":
            try:
                limit = int(params.get("limit", ["200"])[0])
                runs = self.strategy_runs.list_runs(limit=limit)
                self._send_json({
                    "schema_version": "EQS-STRATEGY-RUN-LIST-V1",
                    "read_only": True,
                    "mutations_enabled": False,
                    "runs": runs,
                })
            except (ValueError, StrategyRunReadError) as exc:
                self._send_json({
                    "schema_version": "EQS-STRATEGY-RUN-LIST-V1",
                    "read_only": True,
                    "mutations_enabled": False,
                    "runs": [],
                    "state": "UNAVAILABLE",
                    "error": str(exc),
                })
            return

        if path.startswith("/api/strategy-runs/"):
            suffix = path[len("/api/strategy-runs/"):]
            parts = [part for part in suffix.split("/") if part]
            if not parts:
                self._send_json({"error": "RUN_ID_REQUIRED"}, HTTPStatus.BAD_REQUEST)
                return
            run_id = parts[0]
            resource = parts[1] if len(parts) > 1 else "run"
            try:
                if resource == "run":
                    self._send_json({
                        "schema_version": "EQS-STRATEGY-RUN-DETAIL-V1",
                        "read_only": True,
                        "run": self.strategy_runs.get_run(run_id),
                    })
                    return
                if resource == "events":
                    after = int(params.get("after", ["0"])[0])
                    limit = int(params.get("limit", ["1000"])[0])
                    self._send_json({
                        "schema_version": "EQS-STRATEGY-RUN-EVENTS-V1",
                        "read_only": True,
                        "run_id": run_id,
                        "after": after,
                        "events": self.strategy_runs.events(run_id, after=after, limit=limit),
                    })
                    return
                projection = self.strategy_runs.event_projection(run_id)
                if resource == "results":
                    self._send_json({
                        "schema_version": "EQS-STRATEGY-RUN-RESULTS-V1",
                        "read_only": True,
                        **{k: v for k, v in projection.items() if k != "events"},
                    })
                    return
                if resource in {"trades", "equity", "fills", "market", "signals", "orders", "diagnostics"}:
                    self._send_json({
                        "schema_version": "EQS-STRATEGY-RUN-PROJECTION-V1",
                        "read_only": True,
                        "run_id": run_id,
                        resource: projection[resource],
                    })
                    return
                self._send_json({"error": "UNKNOWN_STRATEGY_RUN_RESOURCE"}, HTTPStatus.NOT_FOUND)
                return
            except KeyError:
                self._send_json({"error": "STRATEGY_RUN_NOT_FOUND", "run_id": run_id}, HTTPStatus.NOT_FOUND)
                return
            except (ValueError, StrategyRunReadError) as exc:
                self._send_json({"error": str(exc), "run_id": run_id}, HTTPStatus.SERVICE_UNAVAILABLE)
                return

        if path == "/api/dashboard":
            payload = self.dashboard.snapshot()
            payload["valuation"] = value_portfolio(payload["execution"].get("paper", {}), self.markets.observations())
            try:
                payload["research_runs"] = {
                    "state": "CONNECTED",
                    "read_only": True,
                    "runs": self.strategy_runs.list_runs(limit=200),
                }
            except StrategyRunReadError as exc:
                payload["research_runs"] = {
                    "state": "UNAVAILABLE",
                    "read_only": True,
                    "runs": [],
                    "error": str(exc),
                }
            self._send_json(payload)
            return
        if path == "/api/status":
            self._send_json(self.adapter.snapshot().to_payload())
            return
        if path == "/api/runtime/status":
            snapshot = self.adapter.snapshot().to_payload()
            self._send_json({
                "meta": snapshot["meta"],
                "execution": snapshot["execution"],
                "risk": snapshot["risk"],
                "system_health": snapshot["system_health"],
            })
            return
        if path == "/api/healthz":
            snapshot = self.adapter.snapshot().to_payload()
            self._send_json({
                "status": "ok",
                "access_mode": "READ_ONLY",
                "adapter": snapshot["meta"]["adapter"],
                "mutations_enabled": False,
            })
            return
        return super().do_GET()

    def _reject_mutation(self) -> None:
        body = json.dumps({"error": "READ_ONLY_INTERFACE", "mutations_enabled": False}).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _reject_mutation  # type: ignore[assignment]
    do_PUT = _reject_mutation  # type: ignore[assignment]
    do_PATCH = _reject_mutation  # type: ignore[assignment]
    do_DELETE = _reject_mutation  # type: ignore[assignment]


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the EQS read-only command interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument(
        "--runtime-db",
        default=None,
        help="Optional F5.6 runtime SQLite path. Opened read-only; EQS_RUNTIME_DB is also supported.",
    )
    parser.add_argument(
        "--strategy-run-db",
        default=None,
        help="Optional strategy research-run SQLite path. Opened read-only by the interface.",
    )
    args = parser.parse_args()
    ReadOnlyInterfaceHandler.adapter = build_adapter(args.runtime_db)
    ReadOnlyInterfaceHandler.dashboard = OperatorDashboard(ROOT, args.runtime_db or os.environ.get("EQS_RUNTIME_DB"))
    ReadOnlyInterfaceHandler.strategy_runs = StrategyRunReadOnlyReader(
        args.strategy_run_db
        or ROOT / "artifacts" / "research" / "strategy_runs" / "research_runs.db"
    )
    server = ThreadingHTTPServer((args.host, args.port), ReadOnlyInterfaceHandler)
    adapter_name = ReadOnlyInterfaceHandler.adapter.snapshot().meta.adapter
    print(f"EQS read-only interface: http://{args.host}:{args.port}")
    print(f"Adapter: {adapter_name}")
    print("Mutation methods are rejected with HTTP 405.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
