from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_system.operations.storage_guard import evaluate_storage_guard
from quant_system.research.backtest_jobs import BacktestJobService


class ResearchBacktestHandler(BaseHTTPRequestHandler):
    service: BacktestJobService | None = None
    allowed_origins = {"http://127.0.0.1:8765", "http://localhost:8765"}

    def log_message(self, format: str, *args: object) -> None:
        return

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, maximum: int = 64_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > maximum:
            raise ValueError("request body size outside allowed bounds")
        raw = self.rfile.read(length)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Idempotency-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/healthz":
            service = self.service
            if service is None:
                self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            guard = service.storage_guard(service.root)
            self._send_json({
                "status": "ok",
                "service": "EQS_J51_RESEARCH_BACKTESTS",
                "engine": "EQS_NATIVE",
                "max_workers": 1,
                "broker_submission_enabled": False,
                "live_authority": False,
                "storage_state": guard.state,
                "allow_new_research": guard.allow_new_research,
            })
            return
        if path == "/strategy-runs":
            service = self.service
            if service is None:
                self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self._send_json({
                "runs": service.registry.list_runs(limit=int(params.get("limit", ["200"])[0])),
                "broker_submission_enabled": False,
                "live_authority": False,
            })
            return
        if path.startswith("/strategy-runs/"):
            tail = path[len("/strategy-runs/"):]
            parts = [part for part in tail.split("/") if part]
            if not parts:
                self._send_json({"error": "RUN_ID_REQUIRED"}, HTTPStatus.BAD_REQUEST)
                return
            run_id = parts[0]
            try:
                if len(parts) == 1:
                    if self.service is None:
                        self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                        return
                    self._send_json(self.service.status(run_id))
                    return
                if len(parts) == 2 and parts[1] == "events":
                    after = int(params.get("after", ["0"])[0])
                    limit = int(params.get("limit", ["1000"])[0])
                    if self.service is None:
                        self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                        return
                    self._send_json(self.service.events(run_id, after=after, limit=limit))
                    return
                if len(parts) == 2 and parts[1] == "results":
                    if self.service is None:
                        self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                        return
                    self._send_json(self.service.results(run_id))
                    return
            except KeyError:
                self._send_json({"error": "RUN_NOT_FOUND", "run_id": run_id}, HTTPStatus.NOT_FOUND)
                return
            except (ValueError, TypeError) as exc:
                self._send_json({"error": type(exc).__name__, "detail": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
        self._send_json({"error": "NOT_FOUND"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/backtests":
                request = self._read_json()
                header_key = self.headers.get("Idempotency-Key")
                if header_key:
                    body_key = request.get("idempotency_key")
                    if body_key is not None and str(body_key) != header_key:
                        raise ValueError("Idempotency-Key header/body mismatch")
                    request["idempotency_key"] = header_key
                if self.service is None:
                    self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                result = self.service.submit(request)
                status = HTTPStatus.ACCEPTED
                if result.get("evidence_result") == "BLOCKED":
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json(result, status)
                return
            if path.startswith("/backtests/") and path.endswith("/cancel"):
                run_id = path[len("/backtests/"):-len("/cancel")].strip("/")
                if self.service is None:
                    self._send_json({"error": "SERVICE_NOT_INITIALISED"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self._send_json(self.service.cancel(run_id))
                return
        except KeyError as exc:
            self._send_json({"error": "NOT_FOUND", "detail": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": type(exc).__name__, "detail": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"error": "NOT_FOUND"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve bounded EQS historical research replay jobs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8770, type=int)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("J51 preparation API is localhost-only")
    ResearchBacktestHandler.service = BacktestJobService(ROOT)
    server = ThreadingHTTPServer((args.host, args.port), ResearchBacktestHandler)
    print(f"EQS J51 research API: http://{args.host}:{args.port}")
    print("Broker submission: disabled. LIVE authority: false.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if ResearchBacktestHandler.service is not None:
            ResearchBacktestHandler.service.close()
        server.server_close()


if __name__ == "__main__":
    main()
