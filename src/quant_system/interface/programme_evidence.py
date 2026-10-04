from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ProgrammeEvidenceReader:
    """Read-only, fail-closed projection of canonical programme evidence."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.phase_b = self.root / "artifacts" / "commissioning" / "phase_b_shared_stack_v1_20260927"
        self.evidence = self.root / "artifacts" / "test-evidence"

    @staticmethod
    def _canonical(obj: Any) -> bytes:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def _valid_seal(cls, doc: dict[str, Any], field: str) -> bool:
        expected = doc.get(field)
        if not isinstance(expected, str) or len(expected) != 64:
            return False
        core = dict(doc)
        core.pop(field, None)
        return hashlib.sha256(cls._canonical(core)).hexdigest() == expected

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > 4_000_000:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _sealed(self, path: Path, field: str) -> tuple[dict[str, Any] | None, bool]:
        doc = self._load(path)
        return doc, bool(doc and self._valid_seal(doc, field))

    def _latest_paper_run(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool, bool]:
        runs = self.phase_b / "runs"
        candidates: list[tuple[int, Path]] = []
        if runs.is_dir():
            for path in runs.glob("EQS-PHASE-B-PAPER-*.json"):
                match = re.fullmatch(r"EQS-PHASE-B-PAPER-(\d{3})\.json", path.name)
                if match:
                    candidates.append((int(match.group(1)), path))
        if not candidates:
            return None, None, False, False
        _, run_path = max(candidates)
        run, run_ok = self._sealed(run_path, "run_manifest_sha256")
        if not run:
            return None, None, False, False
        status_path = self.phase_b / "observer" / str(run.get("run_id")) / "STATUS.json"
        status, status_ok = self._sealed(status_path, "status_sha256")
        return run, status, run_ok, status_ok

    def snapshot(self) -> dict[str, Any]:
        state, state_ok = self._sealed(self.evidence / "EQS00_PROGRAMME_STATE.json", "record_sha256")
        shared04, shared04_ok = self._sealed(
            self.evidence / "EQS_SHARED04_CANONICAL_RECONCILIATION_SEAL.json", "seal_sha256"
        )
        shared05, shared05_ok = self._sealed(
            self.evidence / "EQS_SHARED05_CANONICAL_CERTIFICATION_SEAL.json", "seal_sha256"
        )
        promotion, promotion_ok = self._sealed(
            self.evidence / "EQS_CANONICAL_SHARED04_05_PROMOTION_RECEIPT.json", "receipt_sha256"
        )
        baseline, baseline_ok = self._sealed(self.phase_b / "BASELINE_MANIFEST.json", "manifest_sha256")
        preflight, preflight_ok = self._sealed(self.phase_b / "PREFLIGHT_RESULT_ACCEPTED_V2.json", "preflight_sha256")
        run, status, run_ok, status_ok = self._latest_paper_run()

        terminal = None
        terminal_ok = False
        if run and run.get("run_id"):
            terminal_dir = self.phase_b / "observer" / str(run.get("run_id")) / "terminal"
            terminal_candidates = sorted(terminal_dir.glob("*_TERMINAL_PASS_SEAL.json")) if terminal_dir.is_dir() else []
            if terminal_candidates:
                terminal, terminal_ok = self._sealed(terminal_candidates[-1], "seal_sha256")
        final_phase_b_pass = bool(
            terminal_ok
            and terminal
            and terminal.get("run_id") == (run or {}).get("run_id")
            and terminal.get("result") == "PASS"
            and terminal.get("broker_submission_enabled") is False
            and terminal.get("live_authority") is False
        )

        shared03_candidates = sorted(self.evidence.glob("EQS_SHARED03_CANONICAL_INTEGRATION_SEAL*.json"))
        shared03 = self._load(shared03_candidates[-1]) if shared03_candidates else None
        shared03_complete = bool(shared03 and shared03.get("result") == "PASS")

        shared06_prior = self._load(self.evidence / "EQS_SHARED06_CANONICAL_INTEGRATION_SEAL_V1_20260927.json")
        shared06_final, shared06_final_ok = self._sealed(
            self.evidence / "EQS_SHARED06_FINAL_READINESS_SEAL.json", "seal_sha256"
        )
        if not shared03_complete:
            shared06_state = "REVALIDATION_REQUIRED"
        elif shared06_final_ok and shared06_final and shared06_final.get("state") == "COMPLETE":
            shared06_state = "COMPLETE"
        else:
            shared06_state = "READINESS_EVIDENCE_REQUIRED"

        technical, technical_ok = self._sealed(
            self.evidence / "EQS00_PAPER_ADMISSION_TECHNICAL_PACKAGE.json", "package_sha256"
        )
        admission, admission_ok = self._sealed(
            self.evidence / "EQS00_PAPER_ADMISSION_DECISION.json", "decision_sha256"
        )
        decision_bindings = ("phase_b_qualified_candidate_tree_sha256", "post_apply_readiness_tree_sha256",
                             "phase_b_terminal_seal_sha256", "shared03_integration_seal_sha256",
                             "shared06_final_readiness_seal_sha256", "dashboard_post_phase_b_patch_seal_sha256")
        authority = (admission or {}).get("programme_authority_confirmation", {})
        expected_boundaries = {"broker_submission":"DISABLED","live_authority":False,"eqs06_options":"FROZEN_EXCLUDED","real_trades_permitted":False}
        authorised = bool(
            admission_ok and technical_ok and admission and technical
            and admission.get("decision_status") == "PAPER_AUTHORISED"
            and admission.get("template_only") is False
            and admission.get("technical_package_sha256") == technical.get("package_sha256")
            and all(admission.get(key) == technical.get(key) for key in decision_bindings)
            and admission.get("hard_boundaries") == expected_boundaries
            and authority.get("authority_role") == "EQS-00 Programme Authority"
            and isinstance(authority.get("approval_id"), str) and bool(authority.get("approval_id").strip())
            and isinstance(authority.get("approved_at"), str) and bool(authority.get("approved_at").strip())
        )

        first_run, first_run_ok = self._sealed(
            self.evidence / "EQS_FIRST_PAPER_RUN_RECEIPT.json", "receipt_sha256"
        )

        phase_status = (status or {}).get("status", "UNAVAILABLE")
        qualifying = float((status or {}).get("qualifying_elapsed_seconds", 0.0) or 0.0)
        required = float((status or {}).get("required_qualifying_duration_seconds", 0.0) or 0.0)
        phase = {
            "run_id": (run or {}).get("run_id"),
            "status": phase_status,
            "qualifying_elapsed_seconds": qualifying,
            "required_qualifying_duration_seconds": required,
            "progress_fraction": qualifying / required if required > 0 else 0.0,
            "run_seal_valid": run_ok,
            "status_seal_valid": status_ok,
            "preflight": "PASS" if preflight_ok and preflight and preflight.get("result") == "PASS" else "BLOCKED",
            "commissioning_seal": "PASS" if final_phase_b_pass else "PENDING",
        }

        workstreams = {
            "SHARED-03 Capital & Risk": {
                "state": "COMPLETE" if shared03_complete else ("BLOCKED_PHASE_B" if not final_phase_b_pass else "READY_FOR_GUARDED_APPLY"),
                "evidence_id": (shared03 or {}).get("seal_sha256"),
            },
            "SHARED-04 Execution": {
                "state": "COMPLETE" if shared04_ok and shared04 and shared04.get("result") == "PASS" and promotion_ok else "BLOCKED",
                "evidence_id": (shared04 or {}).get("seal_sha256"),
            },
            "SHARED-05 Portfolio Construction": {
                "state": "COMPLETE" if shared05_ok and shared05 and shared05.get("result") == "PASS" and promotion_ok else "BLOCKED",
                "evidence_id": (shared05 or {}).get("seal_sha256"),
            },
            "SHARED-06 Monitoring & Operations": {
                "state": shared06_state,
                "evidence_id": (shared06_final or shared06_prior or {}).get("seal_sha256"),
            },
        }

        blockers = []
        if not state_ok:
            blockers.append("PROGRAMME_STATE_EVIDENCE_INVALID")
        if not baseline_ok:
            blockers.append("PHASE_B_BASELINE_INVALID")
        if phase["preflight"] != "PASS":
            blockers.append("PHASE_B_PREFLIGHT_NOT_PASS")
        if not final_phase_b_pass:
            blockers.append("PHASE_B_COMMISSIONING_NOT_PASS")
        if not shared03_complete:
            blockers.append("SHARED03_GUARDED_APPLY_NOT_COMPLETE")
        if shared06_state != "COMPLETE":
            blockers.append("SHARED06_EXACT_CANDIDATE_READINESS_NOT_COMPLETE")
        if not authorised:
            blockers.append("EQS00_PAPER_ADMISSION_NOT_AUTHORISED")

        return {
            "evidence_state": "VERIFIED" if state_ok else "DEGRADED",
            "programme_state": (state or {}).get("programme_state", "UNKNOWN"),
            "hard_boundaries": (state or {}).get("hard_boundaries", {}),
            "asset_workstreams": (state or {}).get("active_domains", []),
            "phase_b": phase,
            "workstreams": workstreams,
            "canonical_candidate_tree_sha256": (baseline or {}).get("candidate_identity", {}).get("canonical_candidate_tree_sha256"),
            "post_apply_readiness_tree_sha256": (shared06_final or {}).get("post_apply_readiness_tree_sha256") if shared06_final_ok else None,
            "certifications": {
                "shared04": (shared04 or {}).get("seal_sha256"),
                "shared05": (shared05 or {}).get("seal_sha256"),
                "promotion": (promotion or {}).get("receipt_sha256"),
                "baseline": (baseline or {}).get("manifest_sha256"),
                "preflight": (preflight or {}).get("preflight_sha256"),
            },
            "policy_generation": {
                "state": ("VERIFIED_IN_FIRST_PAPER_ACCEPTANCE" if first_run_ok and (first_run or {}).get("acceptance", {}).get("policy_generation_bound") is True
                          else ("AUTHORISED_NOT_STARTED" if authorised else "NOT_ACTIVE_BEFORE_PAPER_ADMISSION")),
                "generation": (first_run or {}).get("policy_generation") if first_run_ok else None,
            },
            "reservations": {
                "state": ("VERIFIED_IN_FIRST_PAPER_ACCEPTANCE" if first_run_ok and (first_run or {}).get("acceptance", {}).get("capital_risk_reservations_valid") is True
                          else ("AUTHORISED_NOT_STARTED" if authorised else "NOT_ACTIVE_BEFORE_PAPER_ADMISSION")),
                "active_count": (first_run or {}).get("active_reservation_count", 0) if first_run_ok else 0,
            },
            "paper_admission": {
                "state": "AUTHORISED" if authorised else "BLOCKED",
                "decision_sha256": (admission or {}).get("decision_sha256"),
            },
            "first_paper_run": {
                "state": (first_run or {}).get("status", "NOT_STARTED"),
                "receipt_sha256": (first_run or {}).get("receipt_sha256") if first_run_ok else None,
            },
            "blockers": blockers,
            "read_only": True,
        }
