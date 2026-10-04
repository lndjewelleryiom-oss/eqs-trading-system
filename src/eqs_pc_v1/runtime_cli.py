from __future__ import annotations
import argparse
import json
from pathlib import Path
from .runtime import PortfolioConstructionRuntime


def main() -> int:
    parser = argparse.ArgumentParser("eqs-pc-runtime")
    parser.add_argument("bundle", help="JSON with runtime_request and capital_risk_response")
    parser.add_argument("--schema-dir", default=str(Path(__file__).resolve().parents[1] / "schemas"))
    args = parser.parse_args()
    bundle = json.loads(Path(args.bundle).read_text())
    runtime = PortfolioConstructionRuntime(args.schema_dir)
    request = bundle["runtime_request"]
    prep = runtime.prepare(request)
    if prep.status != "RISK_PENDING":
        result = runtime.finalize(request, prep, {})
    else:
        result = runtime.finalize(request, prep, bundle["capital_risk_response"])
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status in {"EXECUTION_READY","RISK_DENIED","RISK_BLOCKED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
