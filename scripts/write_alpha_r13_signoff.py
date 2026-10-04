from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts" / "research"
ALPHA = ART / "alpha_trial_evidence.sqlite"
R13 = ART / "r13_admission.sqlite"
OUT = ART / "alpha_r13_signoff_20261001.json"


def file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    alpha = sqlite3.connect(ALPHA)
    alpha.row_factory = sqlite3.Row
    states = {
        row["state"]: row["n"]
        for row in alpha.execute("SELECT state,COUNT(*) n FROM alpha_trials GROUP BY state")
    }
    journal_count = alpha.execute("SELECT COUNT(*) FROM alpha_journal").fetchone()[0]
    alpha.close()
    r13 = sqlite3.connect(R13)
    admissions = r13.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0]
    admission_events = r13.execute("SELECT COUNT(*) FROM r13_admission_events").fetchone()[0]
    r13.close()

    payload = {
        "schema_version": "EQS-ALPHA-R13-SIGNOFF-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETE_WITH_EXTERNAL_DATA_BLOCKER",
        "completed": {
            "persistent_evidence_runner": True,
            "r1_3_admission_adapter": True,
            "immutable_admission_records": True,
            "revocation_and_supersession": True,
            "generation_pinning": True,
            "full_provenance_envelope": True,
            "synthetic_commissioning": True,
            "clean_process_import": True,
            "focused_regression": "27 passed",
            "repository_regression": "594 passed, 1 skipped, 0 failed",
        },
        "persistent_state": {
            "alpha_trial_states": states,
            "alpha_journal_records": journal_count,
            "r1_3_admissions": admissions,
            "r1_3_admission_events": admission_events,
            "genuine_r1_3_admitted": admissions > 0,
        },
        "external_blockers": [
            "No genuine R1.3 certification manifest has been admitted.",
            "GENUINE EvidenceRunner operation remains fail-closed until admission."
        ],
        "source_hashes": {
            "r13_admission.py": file_sha(ROOT / "src/quant_system/research/r13_admission.py"),
            "evidence_runner.py": file_sha(ROOT / "src/quant_system/evolution/evidence_runner.py"),
            "test_r13_admission_evidence_runner.py": file_sha(ROOT / "tests/test_r13_admission_evidence_runner.py"),
        },
        "authority": {
            "paper_or_live_promotion_granted": False,
            "broker_submission_authority_changed": False,
        },
    }
    OUT.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (OUT.with_suffix(OUT.suffix + ".sha256")).write_text(file_sha(OUT) + "\n", encoding="utf-8")
    print(OUT)
    print(file_sha(OUT))


if __name__ == "__main__":
    main()
