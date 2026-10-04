from pathlib import Path

from quant_system.evolution import AcceptanceCriteria, ResearchEvidence
from quant_system.evolution.evidence_runner import AlphaTrialEvidenceLedger, PersistentEvidenceRunner
from quant_system.evolution.factory import StrategyBlueprint

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "artifacts" / "research" / "alpha_trial_evidence.sqlite"


def blueprint() -> StrategyBlueprint:
    return StrategyBlueprint(
        "synthetic-commissioning",
        "commissioning",
        "synthetic",
        "n/a",
        "deterministic evidence-ledger commissioning",
        ("pass-fixture", "reject-fixture"),
    )


def passing(_):
    return ResearchEvidence(0.01, 0.8, 0.8, 0.99, 0.2, 0.01, True, True, 100)
def failing(_):
    return ResearchEvidence(-0.01, 0.2, 0.2, 0.60, 0.9, 0.5, True, False, 100)


def main() -> None:
    ledger = AlphaTrialEvidenceLedger(LEDGER_PATH)
    existing = ledger.db.execute("SELECT COUNT(*) AS n FROM alpha_trials").fetchone()["n"]
    if existing == 0:
        PersistentEvidenceRunner(passing, ledger).run(blueprint(), criteria=AcceptanceCriteria())
        PersistentEvidenceRunner(failing, ledger).run(blueprint(), criteria=AcceptanceCriteria())
    rows = ledger.db.execute(
        "SELECT state,COUNT(*) AS n FROM alpha_trials GROUP BY state ORDER BY state"
    ).fetchall()
    print(f"ledger={LEDGER_PATH}")
    print("states=" + ",".join(f"{row['state']}:{row['n']}" for row in rows))
    print(f"journal_valid={ledger.verify_hash_chain()}")
    ledger.close()


if __name__ == "__main__":
    main()
