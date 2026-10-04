from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from .lifecycle import PersistentStrategyLifecycle, StrategyLifecycleState
from quant_system.research.executable_strategy import ExecutableInterfaceCertificationLedger

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class PaperCanaryAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperCanaryAdmission:
    admission_id: str
    strategy_id: str
    strategy_version: str
    executable_sha256: str
    interface_certification_sha256: str
    research_evidence_sha256: str
    max_paper_weight: Decimal
    admitted_at: str
    admission_sha256: str


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class PaperCanaryAdmissionLedger:
    """Fail-closed technical gate from VALIDATED research to PAPER_CANARY.

    This authorises only non-live PAPER canaries. There is deliberately no
    broker/LIVE promotion functionality.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        maximum_canary_weight: Decimal = Decimal("0.02"),
        interface_certifications: ExecutableInterfaceCertificationLedger | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maximum_canary_weight = Decimal(maximum_canary_weight)
        self.interface_certifications = interface_certifications
        if not Decimal("0") < self.maximum_canary_weight <= Decimal("0.05"):
            raise ValueError("maximum_canary_weight must be in (0,0.05]")
        self._init_schema()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self):
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_canary_admissions(
                    admission_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    executable_sha256 TEXT NOT NULL,
                    interface_certification_sha256 TEXT NOT NULL,
                    research_evidence_sha256 TEXT NOT NULL,
                    max_paper_weight TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    admission_sha256 TEXT NOT NULL UNIQUE,
                    UNIQUE(strategy_id,strategy_version)
                );
                CREATE TRIGGER IF NOT EXISTS paper_canary_admissions_no_update
                BEFORE UPDATE ON paper_canary_admissions
                BEGIN SELECT RAISE(ABORT,'immutable paper canary admission'); END;
                CREATE TRIGGER IF NOT EXISTS paper_canary_admissions_no_delete
                BEFORE DELETE ON paper_canary_admissions
                BEGIN SELECT RAISE(ABORT,'immutable paper canary admission'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _require_sha(value: str, name: str) -> str:
        if not _SHA256.fullmatch(value):
            raise PaperCanaryAdmissionError(f"{name}_INVALID")
        return value

    def admit(
        self,
        *,
        lifecycle: PersistentStrategyLifecycle,
        strategy_id: str,
        strategy_version: str,
        executable_sha256: str,
        interface_certification_sha256: str,
        research_evidence_sha256: str,
        max_paper_weight: Decimal = Decimal("0.01"),
        paper_authorised: bool,
        broker_submission_enabled: bool,
        live_authority: bool,
        options_frozen_excluded: bool,
    ) -> PaperCanaryAdmission:
        current = lifecycle.get(strategy_id, strategy_version)
        if current.state != StrategyLifecycleState.VALIDATED:
            raise PaperCanaryAdmissionError("STRATEGY_NOT_VALIDATED")
        if current.source_class != "GENUINE":
            raise PaperCanaryAdmissionError("GENUINE_RESEARCH_REQUIRED")
        if not paper_authorised:
            raise PaperCanaryAdmissionError("PAPER_AUTHORITY_REQUIRED")
        if broker_submission_enabled:
            raise PaperCanaryAdmissionError("BROKER_SUBMISSION_MUST_BE_DISABLED")
        if live_authority:
            raise PaperCanaryAdmissionError("LIVE_AUTHORITY_MUST_BE_FALSE")
        if not options_frozen_excluded:
            raise PaperCanaryAdmissionError("OPTIONS_FREEZE_REQUIRED")

        executable_sha256 = self._require_sha(executable_sha256, "EXECUTABLE_SHA256")
        interface_certification_sha256 = self._require_sha(
            interface_certification_sha256, "INTERFACE_CERTIFICATION_SHA256"
        )
        research_evidence_sha256 = self._require_sha(
            research_evidence_sha256, "RESEARCH_EVIDENCE_SHA256"
        )
        if current.research_evidence_sha256 != research_evidence_sha256:
            raise PaperCanaryAdmissionError("RESEARCH_EVIDENCE_BINDING_MISMATCH")
        if self.interface_certifications is None:
            raise PaperCanaryAdmissionError("EXECUTABLE_INTERFACE_CERTIFICATION_REQUIRED")
        if not self.interface_certifications.verify_binding(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            executable_sha256=executable_sha256,
            interface_certification_sha256=interface_certification_sha256,
            require_source_class="GENUINE",
        ):
            raise PaperCanaryAdmissionError("EXECUTABLE_INTERFACE_CERTIFICATION_INVALID")

        max_paper_weight = Decimal(max_paper_weight)
        if not Decimal("0") < max_paper_weight <= self.maximum_canary_weight:
            raise PaperCanaryAdmissionError("CANARY_WEIGHT_OUT_OF_BOUNDS")

        admitted_at = datetime.now(timezone.utc).isoformat()
        body = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "executable_sha256": executable_sha256,
            "interface_certification_sha256": interface_certification_sha256,
            "research_evidence_sha256": research_evidence_sha256,
            "max_paper_weight": str(max_paper_weight),
            "admitted_at": admitted_at,
            "mode": "PAPER_CANARY",
            "broker_submission_enabled": False,
            "live_authority": False,
        }
        admission_sha256 = sha256(_canonical(body)).hexdigest()
        admission_id = "paper-canary-" + admission_sha256[:20]

        con = self._connect()
        try:
            with con:
                con.execute(
                    """INSERT INTO paper_canary_admissions(
                       admission_id,strategy_id,strategy_version,executable_sha256,
                       interface_certification_sha256,research_evidence_sha256,
                       max_paper_weight,admitted_at,admission_sha256
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        admission_id,
                        strategy_id,
                        strategy_version,
                        executable_sha256,
                        interface_certification_sha256,
                        research_evidence_sha256,
                        str(max_paper_weight),
                        admitted_at,
                        admission_sha256,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PaperCanaryAdmissionError("CANARY_ALREADY_ADMITTED") from exc
        finally:
            con.close()

        lifecycle.transition(
            strategy_id,
            strategy_version,
            StrategyLifecycleState.PAPER_CANARY,
            reasons=("PAPER_CANARY_TECHNICAL_ADMISSION_PASS",),
            evidence_sha256=admission_sha256,
        )

        return PaperCanaryAdmission(
            admission_id=admission_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            executable_sha256=executable_sha256,
            interface_certification_sha256=interface_certification_sha256,
            research_evidence_sha256=research_evidence_sha256,
            max_paper_weight=max_paper_weight,
            admitted_at=admitted_at,
            admission_sha256=admission_sha256,
        )
