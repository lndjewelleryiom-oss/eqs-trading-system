from __future__ import annotations

import argparse

from quant_system.research.r13_evidence_pipeline import (
    R13HistoricalEvidencePipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the genuine EQS R1.3 H02/PIT/RR/A01-A28 evidence bundle."
    )
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args()

    result = R13HistoricalEvidencePipeline(
        project_root=args.project_root
    ).run(args.inputs, args.output_root)

    print(f"status={result.status}")
    print(f"status_path={result.status_path}")
    print(
        "certification_inputs="
        + (
            str(result.certification_inputs_path)
            if result.certification_inputs_path is not None
            else ""
        )
    )
    for blocker in result.blockers:
        print(f"blocker={blocker}")
    return 0 if result.status == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
