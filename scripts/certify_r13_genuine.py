from __future__ import annotations

import argparse
from pathlib import Path

from quant_system.research.r13_admission import R13AdmissionLedger
from quant_system.research.r13_certification import GenuineR13CertificationRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble and certify a genuine EQS R1.3 manifest.")
    parser.add_argument("--inputs", required=True, help="Path to genuine certification input index JSON.")
    parser.add_argument("--output", required=True, help="Output canonical R1.3 manifest JSON.")
    parser.add_argument("--project-root", default=None, help="Project root for resolving referenced evidence.")
    parser.add_argument("--ledger", default=None, help="R1.3 admission SQLite path.")
    parser.add_argument("--admit", action="store_true", help="Admit the manifest after successful certification.")
    parser.add_argument("--supersedes", default=None, help="Admission ID explicitly superseded by this generation.")
    args = parser.parse_args()

    ledger = None
    try:
        if args.admit:
            if not args.ledger:
                parser.error("--admit requires --ledger")
            ledger = R13AdmissionLedger(Path(args.ledger))
        result = GenuineR13CertificationRunner(project_root=args.project_root).run(
            args.inputs,
            args.output,
            admission_ledger=ledger,
            admit=args.admit,
            supersedes_admission_id=args.supersedes,
        )
        print(f"status=COMPLETE")
        print(f"manifest_hash={result.manifest['manifest_hash']}")
        print(f"output_sha256={result.output_sha256}")
        print(f"output={result.output_path}")
        print(f"admission_id={result.admission.admission_id if result.admission else ''}")
        return 0
    finally:
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
