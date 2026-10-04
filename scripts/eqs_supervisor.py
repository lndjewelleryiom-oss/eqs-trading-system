from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import shutil
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen

ROOT = Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
EV = ROOT / "artifacts" / "test-evidence"
TOOLS = EV / "prepared-tools"
RUNTIME_DIR = ROOT / "artifacts" / "commissioning" / "current_runtime_v1"
RUNTIME_DB = RUNTIME_DIR / "runtime.db"
RUNTIME_SCRIPT = "run_current_paper_shadow_v1.py"
RUNTIME_TASK = "EQS_Current_PaperShadow"
DASH_TASK = "EQS_Dashboard_ReadOnly"
DASH_PROCESS = "serve_overlay.py"
DASH_HEALTH = "http://127.0.0.1:8766/api/healthz"
SUPERVISOR_STATE = EV / "EQS_SUPERVISOR_STATE.json"
SUPERVISOR_EVENTS = EV / "EQS_SUPERVISOR_EVENTS.jsonl"
BLOCKER_QUEUE = EV / "EQS_SUPERVISOR_BLOCKER_QUEUE.json"
LOCK = EV / ".eqs_supervisor.lock"
HEALTH_LATEST = EV / "EQS_SUSTAINED_PAPER_HEALTH_LATEST.json"
TRACKER_V4 = EV / "EQS_CANONICAL_TRACKER_RECONCILIATION_V4.json"
TRACKER_SNAPSHOT_V4 = EV / "EQS_CANONICAL_TRACKER_SNAPSHOT_V4.json"
PERFORMANCE_STATE = EV / "EQS_PERFORMANCE_AUTONOMY_STATE.json"
MANAGED_PERFORMANCE_EVIDENCE = EV / "EQS_MANAGED_PAPER_PERFORMANCE_EVIDENCE.json"
PERFORMANCE_READINESS = EV / "EQS_PERFORMANCE_AUTONOMY_READINESS.json"

VERSION = "1.3.0"
EXPECTED_RUNTIME_IDS = {
    "current-bybit_linear-paper",
    "current-bybit_linear-shadow",
    "current-okx_swap-paper",
    "current-okx_swap-shadow",
}
STORAGE_RESERVE_BYTES = 8 * 1024 ** 3
STORAGE_CRITICAL_BYTES = 4 * 1024 ** 3

def storage_guard() -> dict:
    free = int(shutil.disk_usage(ROOT).free)
    state = (
        "CRITICAL_RESERVE" if free <= STORAGE_CRITICAL_BYTES
        else "RESEARCH_BACKPRESSURE" if free <= STORAGE_RESERVE_BYTES
        else "NORMAL"
    )
    return {
        "state": state,
        "free_bytes": free,
        "reserve_bytes": STORAGE_RESERVE_BYTES,
        "critical_bytes": STORAGE_CRITICAL_BYTES,
        "allow_new_research": state == "NORMAL",
        "allow_large_downloads": state == "NORMAL",
    }

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def load_json(path: Path):
    pointer = path.with_name(path.stem + ".pointer.json")
    target = path
    expected_file_hash = None
    if pointer.is_file():
        pointer_doc = json.loads(pointer.read_text(encoding="utf-8"))
        pointer_body = dict(pointer_doc)
        pointer_seal = pointer_body.pop("record_sha256", None)
        if not isinstance(pointer_seal, str) or hashlib.sha256(canonical(pointer_body)).hexdigest() != pointer_seal:
            raise ValueError("publication pointer seal invalid")
        generation_name = pointer_doc.get("generation_file")
        if not isinstance(generation_name, str) or Path(generation_name).name != generation_name:
            raise ValueError("invalid publication generation")
        generation_dir = path.parent / (path.stem + ".generations")
        target = generation_dir / generation_name
        expected_file_hash = pointer_doc.get("generation_sha256")
    raw = target.read_bytes()
    if expected_file_hash is not None and hashlib.sha256(raw).hexdigest() != expected_file_hash:
        raise ValueError("publication generation hash mismatch")
    return json.loads(raw)

def valid_seal(doc: dict, field: str) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get(field), str):
        return False
    x = dict(doc)
    expected = x.pop(field)
    return hashlib.sha256(canonical(x)).hexdigest() == expected


def eqs02_source_authority_satisfied(auth: dict) -> bool:
    return bool(
        valid_seal(auth, "seal_sha256")
        and auth.get("result") == "PASS"
        and auth.get("classification") == "AUTHENTICATED_PRODUCTION_MARKET_DATA_READ_ONLY"
        and auth.get("production_paper_market_data_authority") is True
        and auth.get("trading_endpoint_used") is False
        and auth.get("broker_submission_enabled") is False
        and auth.get("live_authority") is False
    )

def atomic_json(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)

def append_event(kind: str, **fields) -> None:
    event = {
        "schema_id": "EQS-SUPERVISOR-EVENT-V1",
        "at": utcnow().isoformat().replace("+00:00", "Z"),
        "kind": kind,
        **fields,
    }
    event["event_sha256"] = hashlib.sha256(canonical(event)).hexdigest()
    with SUPERVISOR_EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")

def run(cmd: list[str], timeout: int = 120, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)

def powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return run(["powershell", "-NoProfile", "-Command", script], timeout=timeout)

def python_processes(pattern: str) -> list[dict]:
    escaped = pattern.replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*" + escaped + "*' } | "
        "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    cp = powershell(ps, timeout=30)
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    data = json.loads(cp.stdout)
    return data if isinstance(data, list) else [data]

def task_run(task_name: str) -> tuple[bool, str]:
    cp = run(["schtasks", "/Run", "/TN", task_name], timeout=30)
    return cp.returncode == 0, (cp.stdout + cp.stderr).strip()

def task_end(task_name: str) -> None:
    run(["schtasks", "/End", "/TN", task_name], timeout=30)

def kill_pids(pids: list[int]) -> None:
    for pid in pids:
        run(["taskkill", "/PID", str(pid), "/F"], timeout=30)

def acquire_lock() -> bool:
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
            if age < 600:
                return False
            LOCK.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {utcnow().isoformat()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock() -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass

def boundary_check() -> dict:
    blockers = []
    try:
        state = load_json(EV / "EQS00_PROGRAMME_STATE.json")
        if not valid_seal(state, "record_sha256"):
            blockers.append("PROGRAMME_STATE_SEAL_INVALID")
        hard = state.get("hard_boundaries", {})
        expected = {
            "broker_submission": "DISABLED",
            "live_authority": False,
            "eqs06_options": "FROZEN_EXCLUDED",
            "real_trades_permitted": False,
        }
        for key, value in expected.items():
            if hard.get(key) != value:
                blockers.append("HARD_BOUNDARY_MISMATCH:" + key)
        if state.get("paper_admission_state") != "PAPER_AUTHORISED":
            blockers.append("PAPER_NOT_AUTHORISED")
    except Exception as exc:
        blockers.append("PROGRAMME_STATE_UNREADABLE:" + type(exc).__name__)

    try:
        cp = run([sys.executable, str(TOOLS / "validate_eqs00_admission_decision.py")], timeout=45)
        if cp.returncode != 0:
            blockers.append("ADMISSION_DECISION_INVALID")
    except Exception as exc:
        blockers.append("ADMISSION_VALIDATION_ERROR:" + type(exc).__name__)

    autonomy_state = "UNKNOWN"
    try:
        live_policy = load_json(EV / "EQS_LIVE_ADMISSION_POLICY.json")
        if not valid_seal(live_policy, "record_sha256"):
            blockers.append("LIVE_ADMISSION_POLICY_SEAL_INVALID")
        if live_policy.get("programme_rule") != "LIVE_PROHIBITED_UNTIL_PERFORMANCE_AUTONOMY_READY":
            blockers.append("LIVE_ADMISSION_POLICY_RULE_INVALID")
        if live_policy.get("autonomy_definition") != "END_TO_END_PERFORMANCE_AUTONOMY":
            blockers.append("PERFORMANCE_AUTONOMY_DEFINITION_INVALID")
        if live_policy.get("live_authority_permitted") is not False:
            blockers.append("LIVE_AUTHORITY_POLICY_NOT_FALSE")
        if live_policy.get("broker_submission_permitted") is not False:
            blockers.append("BROKER_SUBMISSION_POLICY_NOT_FALSE")
        if live_policy.get("autonomy_readiness_required") is not True:
            blockers.append("AUTONOMY_READINESS_REQUIREMENT_MISSING")
        autonomy_state = str(live_policy.get("autonomy_readiness_state", "UNKNOWN"))
    except Exception as exc:
        blockers.append("LIVE_ADMISSION_POLICY_UNREADABLE:" + type(exc).__name__)

    return {
        "ok": not blockers,
        "blockers": blockers,
        "autonomy_readiness_state": autonomy_state,
        "live_promotion_allowed": False,
    }

def runtime_db_snapshot() -> dict:
    if not RUNTIME_DB.is_file():
        return {"ok": False, "reason": "RUNTIME_DB_MISSING", "runtimes": []}
    try:
        con = sqlite3.connect(RUNTIME_DB)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select runtime_id,mode,status,generation,lease_expires_at,last_heartbeat_at,halt_reason "
            "from runtime_state order by runtime_id"
        )]
        con.close()
        ids = {r["runtime_id"] for r in rows}
        return {"ok": ids == EXPECTED_RUNTIME_IDS, "runtimes": rows, "ids": sorted(ids)}
    except Exception as exc:
        return {"ok": False, "reason": "RUNTIME_DB_READ_ERROR:" + type(exc).__name__, "runtimes": []}

def health_check() -> dict:
    env = dict(os.environ)
    env["EQS_SUPERVISOR_CHILD"] = "1"
    cp = subprocess.run(
        [sys.executable, str(TOOLS / "record_sustained_paper_health.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=90,
        env=env,
    )
    parsed = None
    if cp.stdout.strip():
        try:
            parsed = json.loads(cp.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    return {
        "ok": cp.returncode == 0 and isinstance(parsed, dict) and parsed.get("status") == "PASS",
        "returncode": cp.returncode,
        "result": parsed,
        "stderr": cp.stderr.strip()[-2000:],
    }

def ensure_runtime(repair: bool, boundaries: dict) -> dict:
    procs = python_processes(RUNTIME_SCRIPT)
    result = {"process_count_before": len(procs), "action": "NONE", "verified": False}
    if not boundaries["ok"]:
        result["action"] = "BLOCKED_BY_BOUNDARIES"
        result["blockers"] = boundaries["blockers"]
        return result

    capacity = storage_guard()
    result["storage"] = capacity
    if capacity["state"] != "NORMAL":
        result["action"] = "STORAGE_BACKPRESSURE_PAUSE"
        if repair and procs:
            task_end(RUNTIME_TASK)
            kill_pids([int(p["ProcessId"]) for p in procs])
            time.sleep(2)
        remaining = python_processes(RUNTIME_SCRIPT)
        result["process_count_after"] = len(remaining)
        result["verified"] = False
        result["paused_safely"] = len(remaining) == 0
        append_event(
            "RUNTIME_STORAGE_BACKPRESSURE",
            state=capacity["state"],
            free_bytes=capacity["free_bytes"],
            reserve_bytes=capacity["reserve_bytes"],
            old_pids=[p["ProcessId"] for p in procs],
            paused_safely=result["paused_safely"],
        )
        return result

    if len(procs) > 1:
        result["action"] = "DUPLICATE_FAIL_CLOSED_RESTART"
        if repair:
            task_end(RUNTIME_TASK)
            kill_pids([int(p["ProcessId"]) for p in procs])
            time.sleep(2)
            ok, detail = task_run(RUNTIME_TASK)
            result["task_start_ok"] = ok
            result["task_start_detail"] = detail
            append_event("RUNTIME_DUPLICATE_REMEDIATION", old_pids=[p["ProcessId"] for p in procs], task_start_ok=ok)
    elif len(procs) == 0:
        result["action"] = "START_RUNTIME"
        if repair:
            ok, detail = task_run(RUNTIME_TASK)
            result["task_start_ok"] = ok
            result["task_start_detail"] = detail
            append_event("RUNTIME_RESTART_REQUESTED", task_start_ok=ok)
    else:
        result["verified"] = True

    if repair and result["action"] != "NONE":
        deadline = time.time() + 50
        while time.time() < deadline:
            time.sleep(5)
            current = python_processes(RUNTIME_SCRIPT)
            if len(current) == 1:
                result["verified"] = True
                result["process_count_after"] = 1
                result["pid"] = current[0]["ProcessId"]
                break
        if not result["verified"]:
            result["process_count_after"] = len(python_processes(RUNTIME_SCRIPT))
            append_event("RUNTIME_RECOVERY_FAILED", result=result)

    if result["verified"]:
        snap = runtime_db_snapshot()
        result["runtime_db"] = snap
    return result

def dashboard_health() -> dict:
    try:
        with urlopen(DASH_HEALTH, timeout=5) as r:
            payload = json.loads(r.read().decode("utf-8"))
        ok = (
            payload.get("status") == "ok"
            and payload.get("access_mode") == "READ_ONLY"
            and payload.get("mutations_enabled") is False
        )
        return {"ok": ok, "payload": payload}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ":" + str(exc)}

def start_dashboard_direct() -> tuple[bool, str]:
    overlay = Path(r"C:\Temp\EQS\dashboard_overlay")
    out = (EV / "eqs_dashboard.stdout.log").open("ab")
    err = (EV / "eqs_dashboard.stderr.log").open("ab")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [
                sys.executable,
                "serve_overlay.py",
                "--port",
                "8766",
                "--runtime-db",
                str(RUNTIME_DB),
            ],
            cwd=overlay,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        return True, "DIRECT_DETACHED_START"
    except Exception as exc:
        return False, type(exc).__name__ + ":" + str(exc)
    finally:
        out.close()
        err.close()

def ensure_dashboard(repair: bool, boundaries: dict) -> dict:
    health = dashboard_health()
    procs = python_processes(DASH_PROCESS)
    result = {"health_before": health, "process_count_before": len(procs), "action": "NONE", "verified": health["ok"] and len(procs) == 1}
    if not boundaries["ok"]:
        result["action"] = "BLOCKED_BY_BOUNDARIES"
        return result
    if result["verified"]:
        return result
    result["action"] = "RESTART_READ_ONLY_DASHBOARD"
    if not repair:
        return result
    if procs:
        kill_pids([int(p["ProcessId"]) for p in procs])
        time.sleep(1)
    ok, detail = task_run(DASH_TASK)
    if not ok:
        ok, detail = start_dashboard_direct()
    result["task_start_ok"] = ok
    result["task_start_detail"] = detail
    time.sleep(3)
    result["health_after"] = dashboard_health()
    result["process_count_after"] = len(python_processes(DASH_PROCESS))
    result["verified"] = result["health_after"]["ok"] and result["process_count_after"] == 1
    append_event("DASHBOARD_RECOVERY", verified=result["verified"], task_start_ok=ok)
    return result

def verified_artifact(path: Path, seal_field: str, accepted: tuple[str, ...] = ("PASS",)) -> bool:
    try:
        doc = load_json(path)
        return valid_seal(doc, seal_field) and doc.get("result", doc.get("status")) in accepted
    except Exception:
        return False

def execute_dependency_safe_jobs(repair: bool, boundaries: dict) -> list[dict]:
    jobs = []
    if not boundaries["ok"]:
        return [{"job": "ALL", "status": "BLOCKED_BY_BOUNDARIES"}]

    # EQS-02 pre-PAPER certification is deterministic, non-live and broker-free.
    e2 = EV / "EQS02_PRE_PAPER_STAGE_CERTIFICATION.json"
    if not verified_artifact(e2, "seal_sha256"):
        row = {"job": "EQS02_PRE_PAPER_CERTIFICATION", "status": "READY"}
        if repair:
            cp = run([sys.executable, str(TOOLS / "certify_eqs02_pre_paper_stage.py")], timeout=360)
            row["returncode"] = cp.returncode
            row["status"] = "VERIFIED_COMPLETE" if cp.returncode == 0 and verified_artifact(e2, "seal_sha256") else "FAILED"
            row["stdout"] = cp.stdout.strip()[-2000:]
            row["stderr"] = cp.stderr.strip()[-2000:]
            append_event("JOB_EXECUTION", job=row["job"], status=row["status"])
        jobs.append(row)

    # Keep one canonical tracker projection current. V2/V3 writers are retained as
    # historical tools only; the supervisor owns the V4 + forward-gate projection.
    prereqs = [
        EV / "EQS02_PRE_PAPER_STAGE_CERTIFICATION.json",
        EV / "EQS00_PAPER_ADMISSION_DECISION.json",
        EV / "EQS_SHARED06_FINAL_READINESS_SEAL.json",
        HEALTH_LATEST,
    ]
    base_updater = TOOLS / "reconcile_canonical_tracker_v4.py"
    forward_updater = TOOLS / "reconcile_forward_gates.py"
    forward_inputs = [
        EV / "EQS02_ALPACA_FORWARD_PAPER_STATUS.json",
        EV / "EQS03_FX_FORWARD_PAPER_STATUS.json",
        EV / "XAUUSD_FORWARD_SPOT_PAPER_STATUS.json",
        EV / "EQS05_RATES_FORWARD_CURVE_STATUS.json",
    ]
    should_update = base_updater.is_file() and forward_updater.is_file() and all(p.is_file() for p in prereqs)
    if should_update:
        observed = prereqs + [p for p in forward_inputs if p.is_file()]
        newest = max(p.stat().st_mtime for p in observed)
        stale = not TRACKER_V4.is_file() or TRACKER_V4.stat().st_mtime < newest
        if stale:
            row = {"job": "CANONICAL_TRACKER_V4", "status": "READY"}
            if repair:
                base = run([sys.executable, str(base_updater)], timeout=180)
                forward = run([sys.executable, str(forward_updater)], timeout=180) if base.returncode == 0 else None
                row["returncode"] = base.returncode if base.returncode != 0 else (forward.returncode if forward else 1)
                row["process_result"] = "PASS" if row["returncode"] == 0 else "FAILED"
                row["status"] = "VERIFIED_COMPLETE" if row["returncode"] == 0 and TRACKER_V4.is_file() else "FAILED"
                row["stdout"] = (
                    base.stdout.strip()[-1000:]
                    + ("\n" + forward.stdout.strip()[-1000:] if forward else "")
                ).strip()
                row["stderr"] = (
                    base.stderr.strip()[-1000:]
                    + ("\n" + forward.stderr.strip()[-1000:] if forward else "")
                ).strip()
                append_event("JOB_EXECUTION", job=row["job"], status=row["status"], process_result=row["process_result"])
            jobs.append(row)

    # Retry broker-free forward-source acceptance on a bounded cadence, but never
    # download new source data while storage is below the configured reserve.
    capacity = storage_guard()
    forward_specs = (
        ("EQS02_FORWARD_PAPER_ACCEPTANCE", TOOLS / "run_eqs02_forward_paper_acceptance.py",
         EV / "EQS02_ALPACA_FORWARD_PAPER_STATUS.json", EV / "EQS02_ALPACA_FORWARD_PAPER_ACCEPTANCE.json"),
        ("EQS03_FORWARD_PAPER_ACCEPTANCE", TOOLS / "run_eqs03_forward_paper_acceptance.py",
         EV / "EQS03_FX_FORWARD_PAPER_STATUS.json", EV / "EQS03_FX_FORWARD_PAPER_ACCEPTANCE.json"),
        ("XAUUSD_FORWARD_SPOT_ACCEPTANCE", TOOLS / "run_xauusd_forward_spot_acceptance.py",
         EV / "XAUUSD_FORWARD_SPOT_PAPER_STATUS.json", EV / "XAUUSD_FORWARD_SPOT_PAPER_ACCEPTANCE.json"),
        ("EQS05_FORWARD_CURVE_ACCEPTANCE", TOOLS / "run_eqs05_forward_curve_acceptance.py",
         EV / "EQS05_RATES_FORWARD_CURVE_STATUS.json", EV / "EQS05_RATES_FORWARD_CURVE_ACCEPTANCE.json"),
    )
    for job_name, tool, status_path, acceptance_path in forward_specs:
        if verified_artifact(acceptance_path, "seal_sha256"):
            continue
        status_age = float("inf") if not status_path.is_file() else max(0.0, time.time() - status_path.stat().st_mtime)
        if status_age < 900:
            continue
        row = {"job": job_name, "status": "READY"}
        if capacity["state"] != "NORMAL":
            row["status"] = "BLOCKED"
            row["process_result"] = "NOT_RUN"
            row["evidence_result"] = "STORAGE_CAPACITY_GUARD"
            jobs.append(row)
            continue
        if not tool.is_file():
            row["status"] = "FAILED"
            row["process_result"] = "NOT_RUN"
            row["evidence_result"] = "TOOL_MISSING"
            jobs.append(row)
            continue
        if repair:
            cp = run([sys.executable, str(tool)], timeout=120)
            row["returncode"] = cp.returncode
            row["process_result"] = "PASS" if cp.returncode == 0 else "FAILED"
            evidence_result = None
            if status_path.is_file():
                try:
                    evidence_result = load_json(status_path).get("state")
                except Exception:
                    evidence_result = None
            row["evidence_result"] = evidence_result
            accepted = verified_artifact(acceptance_path, "seal_sha256")
            row["status"] = (
                "VERIFIED_COMPLETE" if accepted
                else "FAILED" if cp.returncode != 0
                else "RUNNING"
            )
            row["stdout"] = cp.stdout.strip()[-2000:]
            row["stderr"] = cp.stderr.strip()[-2000:]
            append_event(
                "JOB_EXECUTION",
                job=row["job"],
                status=row["status"],
                process_result=row["process_result"],
                evidence_result=evidence_result,
            )
        jobs.append(row)

    # Run the PAPER-only performance-autonomy cycle continuously. It may ingest
    # evidence and progress dependency-safe lifecycle work, but cannot enable LIVE.
    perf_tool = TOOLS / "run_performance_autonomy_cycle.py"
    perf_stale = (
        not PERFORMANCE_STATE.is_file()
        or (time.time() - PERFORMANCE_STATE.stat().st_mtime) > 90
    )
    if perf_tool.is_file() and perf_stale:
        row = {"job": "PERFORMANCE_AUTONOMY_CYCLE", "status": "READY"}
        if repair:
            cp = run([sys.executable, str(perf_tool)], timeout=180)
            row["returncode"] = cp.returncode
            verified = False
            if cp.returncode == 0 and PERFORMANCE_STATE.is_file():
                try:
                    doc = load_json(PERFORMANCE_STATE)
                    verified = (
                        valid_seal(doc, "record_sha256")
                        and doc.get("mode") == "PAPER_ONLY"
                        and doc.get("live_authority") is False
                        and doc.get("broker_submission_enabled") is False
                        and isinstance(doc.get("performance_autonomy_ready"), bool)
                    )
                except Exception:
                    verified = False
            evidence_result = None
            if verified:
                try:
                    evidence_result = load_json(PERFORMANCE_STATE).get("status")
                except Exception:
                    evidence_result = None
            row["process_result"] = "PASS" if cp.returncode == 0 else "FAILED"
            row["evidence_result"] = evidence_result
            row["status"] = (
                "FAILED" if not verified
                else "VERIFIED_COMPLETE" if evidence_result in {"PASS", "COMPLETE"}
                else "BLOCKED" if evidence_result in {"BLOCKED", "READY_WAITING_FOR_GENUINE_STRATEGY"}
                else "RUNNING"
            )
            row["stdout"] = cp.stdout.strip()[-2000:]
            row["stderr"] = cp.stderr.strip()[-2000:]
            append_event("JOB_EXECUTION", job=row["job"], status=row["status"], process_result=row["process_result"], evidence_result=evidence_result)
        jobs.append(row)

    managed_tool = TOOLS / "record_managed_paper_evidence.py"
    managed_stale = (
        not MANAGED_PERFORMANCE_EVIDENCE.is_file()
        or (time.time() - MANAGED_PERFORMANCE_EVIDENCE.stat().st_mtime) > 90
    )
    if managed_tool.is_file() and managed_stale:
        row = {"job": "MANAGED_PAPER_PERFORMANCE_EVIDENCE", "status": "READY"}
        if repair:
            cp = run([sys.executable, str(managed_tool)], timeout=120)
            row["returncode"] = cp.returncode
            verified = False
            evidence_status = None
            if cp.returncode == 0 and MANAGED_PERFORMANCE_EVIDENCE.is_file():
                try:
                    doc = load_json(MANAGED_PERFORMANCE_EVIDENCE)
                    evidence_status = doc.get("status")
                    verified = (
                        valid_seal(doc, "record_sha256")
                        and evidence_status in {"PASS", "RUNNING", "BLOCKED"}
                        and doc.get("live_authority") is False
                        and int(doc.get("broker_send_count", -1)) == 0
                        and isinstance(doc.get("attribution_hash_chain_valid"), bool)
                    )
                except Exception:
                    verified = False
            row["process_result"] = "PASS" if cp.returncode == 0 else "FAILED"
            row["evidence_result"] = evidence_status
            row["status"] = (
                "FAILED" if not verified
                else "VERIFIED_COMPLETE" if evidence_status == "PASS"
                else "RUNNING" if evidence_status == "RUNNING"
                else "BLOCKED"
            )
            row["evidence_status"] = evidence_status
            row["stdout"] = cp.stdout.strip()[-2000:]
            row["stderr"] = cp.stderr.strip()[-2000:]
            append_event(
                "JOB_EXECUTION",
                job=row["job"],
                status=row["status"],
                evidence_status=evidence_status,
            )
        jobs.append(row)

    readiness_tool = TOOLS / "evaluate_performance_autonomy_readiness.py"
    readiness_stale = (
        not PERFORMANCE_READINESS.is_file()
        or (time.time() - PERFORMANCE_READINESS.stat().st_mtime) > 90
        or (
            MANAGED_PERFORMANCE_EVIDENCE.is_file()
            and PERFORMANCE_READINESS.is_file()
            and PERFORMANCE_READINESS.stat().st_mtime < MANAGED_PERFORMANCE_EVIDENCE.stat().st_mtime
        )
    )
    if readiness_tool.is_file() and readiness_stale:
        row = {"job": "PERFORMANCE_AUTONOMY_READINESS", "status": "READY"}
        if repair:
            cp = run([sys.executable, str(readiness_tool)], timeout=120)
            row["returncode"] = cp.returncode
            verified = False
            result = None
            if cp.returncode == 0 and PERFORMANCE_READINESS.is_file():
                try:
                    doc = load_json(PERFORMANCE_READINESS)
                    result = doc.get("result")
                    verified = (
                        valid_seal(doc, "record_sha256")
                        and result in {"PASS", "BLOCKED"}
                        and doc.get("live_authority") is False
                        and doc.get("broker_submission_enabled") is False
                    )
                except Exception:
                    verified = False
            row["process_result"] = "PASS" if cp.returncode == 0 else "FAILED"
            row["evidence_result"] = result
            row["status"] = (
                "FAILED" if not verified
                else "VERIFIED_COMPLETE" if result == "PASS"
                else "BLOCKED"
            )
            row["readiness_result"] = result
            row["stdout"] = cp.stdout.strip()[-2000:]
            row["stderr"] = cp.stderr.strip()[-2000:]
            append_event(
                "JOB_EXECUTION",
                job=row["job"],
                status=row["status"],
                readiness_result=result,
            )
        jobs.append(row)
    return jobs

def build_blocker_queue() -> dict:
    external = []
    internal = []
    deferred = []

    capacity = storage_guard()
    if capacity["state"] != "NORMAL":
        internal.append({
            "workstream": "J04-STORAGE-CAPACITY",
            "category": "INTERNAL_CAPACITY",
            "item": f'{capacity["state"]}: free_bytes={capacity["free_bytes"]}; reserve_bytes={capacity["reserve_bytes"]}',
            "auto_bypass_permitted": False,
        })

    # EQS-02 supersession-aware gate. The earlier pre-PAPER certificate can retain a historical
    # source-authority blocker after a later sealed production-data acceptance has satisfied it.
    e2 = EV / "EQS02_PRE_PAPER_STAGE_CERTIFICATION.json"
    e2_auth = EV / "EQS02_ALPACA_AUTHENTICATED_ACCEPTANCE.json"
    e2_source_authorised = False
    if e2_auth.is_file():
        try:
            auth = load_json(e2_auth)
            e2_source_authorised = eqs02_source_authority_satisfied(auth)
        except Exception:
            e2_source_authorised = False
    if e2.is_file():
        try:
            doc = load_json(e2)
            for blocker in doc.get("blockers", []):
                if blocker == "APPROVED_PRODUCTION_STOCKS_ETFS_MARKET_DATA_SOURCE_REQUIRED" and e2_source_authorised:
                    continue
                target = external if blocker == "APPROVED_PRODUCTION_STOCKS_ETFS_MARKET_DATA_SOURCE_REQUIRED" else deferred
                target.append({
                    "workstream": "EQS-02-PAPER",
                    "category": "EXTERNAL_DATA_AUTHORITY" if target is external else "DEFERRED_AFTER_EXTERNAL_BLOCKER",
                    "item": blocker,
                    "auto_bypass_permitted": False,
                })
        except Exception:
            pass

    # Genuine R1.3 historical source feasibility is also an outside dependency
    # when the frozen required series cannot be obtained from official public sources.
    r13_feasibility = EV / "EQS_R13_SOURCE_FEASIBILITY.json"
    if r13_feasibility.is_file():
        try:
            doc = load_json(r13_feasibility)
            if (
                valid_seal(doc, "record_sha256")
                and doc.get("external_historical_provider_required") is True
            ):
                missing = doc.get("missing_public_requirements") or []
                external.append({
                    "workstream": "R1.3-GENUINE-ALPHA",
                    "category": "EXTERNAL_HISTORICAL_DATA_PROVIDER",
                    "item": "approved long-horizon provider required for frozen missing series: "
                            + ", ".join(
                                sorted(
                                    f"{row.get('venue')}:{row.get('series')}"
                                    for row in missing
                                    if isinstance(row, dict)
                                )
                            ),
                    "auto_bypass_permitted": False,
                })
        except Exception:
            pass

    # Explicit R1.3 setup requirements discovered after provider/storage sizing.
    r13_setup = EV / "EQS_R13_EXTERNAL_SETUP_REQUIREMENTS.json"
    if r13_setup.is_file():
        try:
            doc = load_json(r13_setup)
            if valid_seal(doc, "record_sha256"):
                for item in doc.get("requirements", []):
                    if not isinstance(item, dict) or item.get("required") is not True:
                        continue
                    external.append({
                        "workstream": "R1.3-GENUINE-ALPHA",
                        "category": str(item.get("category") or "EXTERNAL_SETUP"),
                        "item": str(item.get("item") or item.get("id") or "UNKNOWN"),
                        "auto_bypass_permitted": False,
                    })
        except Exception:
            pass

    # Only genuinely unsatisfied outside dependencies go in the external queue.
    external_needles = {
        "EQS-02-PAPER": ("approve/provision production-grade Stocks/ETFs market-data source", "EXTERNAL_DATA_AUTHORITY"),
        "EQS-03-PAPER": ("obtain genuine alternate-source coverage for AUDJPY 2026-08-13 17:00-19:00 UTC", "EXTERNAL_DATA_SOURCE"),
        "EQS-04-PAPER": ("provision CME DataMine, CME delayed/real-time licensed feed, or approved licensed distributor source", "EXTERNAL_CREDENTIAL_OR_DATA_SOURCE"),
        "EQS-05-PAPER": ("approve/provision genuine secondary Treasury price/yield curve source", "EXTERNAL_DATA_AUTHORITY"),
    }

    tracker = ROOT / "tracker.json"
    if TRACKER_SNAPSHOT_V4.is_file() or tracker.is_file():
        try:
            if TRACKER_SNAPSHOT_V4.is_file():
                snap = load_json(TRACKER_SNAPSHOT_V4)
                if not valid_seal(snap, "record_sha256"):
                    raise ValueError("canonical tracker snapshot seal invalid")
                doc = snap.get("tracker")
                if not isinstance(doc, dict):
                    raise ValueError("canonical tracker snapshot missing tracker")
            else:
                doc = load_json(tracker)
            for row in doc.get("components", []):
                wid = row.get("id")
                if wid not in external_needles or row.get("status") not in {"BLOCKED", "IN PROGRESS"}:
                    continue
                external_text, external_category = external_needles[wid]
                for rem in row.get("remaining", []):
                    target = external if rem == external_text else deferred
                    if target is external and any(x.get("workstream") == wid for x in external):
                        # A sealed gate may already express the same outside dependency canonically.
                        continue
                    target.append({
                        "workstream": wid,
                        "category": external_category if target is external else "DEFERRED_AFTER_EXTERNAL_BLOCKER",
                        "item": rem,
                        "auto_bypass_permitted": False,
                    })
        except Exception:
            pass

    def dedupe(rows):
        unique = {}
        for item in rows:
            key = (item["workstream"], item["category"], item["item"])
            unique[key] = item
        return [unique[k] for k in sorted(unique)]

    external = dedupe(external)
    internal = dedupe(internal)
    deferred = dedupe(deferred)
    all_blockers = external + internal
    record = {
        "schema_id": "EQS-SUPERVISOR-BLOCKER-QUEUE-V3",
        "updated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "items": all_blockers,
        "external_items": external,
        "internal_items": internal,
        "deferred_jobs": deferred,
        "count": len(all_blockers),
        "external_count": len(external),
        "internal_count": len(internal),
        "deferred_count": len(deferred),
    }
    record["record_sha256"] = hashlib.sha256(canonical(record)).hexdigest()
    atomic_json(BLOCKER_QUEUE, record)
    return record

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="Observe only; make no repair or job-execution changes.")
    args = ap.parse_args()
    repair = not args.audit

    if not acquire_lock():
        return 0
    started = utcnow()
    try:
        boundaries = boundary_check()
        runtime = ensure_runtime(repair, boundaries)
        dashboard = ensure_dashboard(repair, boundaries)
        capacity = storage_guard()

        # Run a fresh safety health record if the monitor is stale or after a runtime recovery.
        health = None
        health_stale = not HEALTH_LATEST.is_file() or (time.time() - HEALTH_LATEST.stat().st_mtime) > 420
        if repair and boundaries["ok"] and (health_stale or runtime.get("action") != "NONE"):
            health = health_check()
            append_event("SUSTAINED_HEALTH_REFRESH", ok=health["ok"], result=health.get("result"))
        elif HEALTH_LATEST.is_file():
            try:
                cached_doc = load_json(HEALTH_LATEST)
                cached_seal_valid = valid_seal(cached_doc, "record_sha256")
                health = {"ok": cached_seal_valid and cached_doc.get("status") == "PASS", "cached": True, "seal_valid": cached_seal_valid}
            except Exception:
                health = {"ok": False, "cached": True}

        jobs = execute_dependency_safe_jobs(repair, boundaries)
        queue = build_blocker_queue()

        healthy = (
            boundaries["ok"]
            and runtime.get("verified") is True
            and dashboard.get("verified") is True
            and capacity.get("state") == "NORMAL"
            and (health is None or health.get("ok") is True)
            and all(j.get("status") not in {"FAILED"} for j in jobs)
        )
        state = {
            "schema_id": "EQS-SUPERVISOR-STATE-V1",
            "supervisor_version": VERSION,
            "observed_at": utcnow().isoformat().replace("+00:00", "Z"),
            "mode": "AUDIT" if args.audit else "ACTIVE",
            "status": "HEALTHY" if healthy else "ATTENTION",
            "hard_boundaries": boundaries,
            "runtime": runtime,
            "dashboard": dashboard,
            "storage_guard": capacity,
            "sustained_health": health,
            "jobs": jobs,
            "blocker_queue_sha256": queue["record_sha256"],
            "external_or_unresolved_blocker_count": queue["count"],
            "safety_contract": {
                "broker_submission_may_be_enabled": False,
                "live_authority_may_be_granted": False,
                "options_may_be_unfrozen": False,
                "failed_gates_may_be_bypassed": False,
            },
            "elapsed_seconds": (utcnow() - started).total_seconds(),
        }
        state["record_sha256"] = hashlib.sha256(canonical(state)).hexdigest()
        atomic_json(SUPERVISOR_STATE, state)
        append_event("SUPERVISOR_CYCLE", status=state["status"], mode=state["mode"], record_sha256=state["record_sha256"])
        print(json.dumps({
            "status": state["status"],
            "mode": state["mode"],
            "runtime_verified": runtime.get("verified"),
            "dashboard_verified": dashboard.get("verified"),
            "storage_state": capacity.get("state"),
            "hard_boundaries_ok": boundaries["ok"],
            "jobs": [{"job": j.get("job"), "status": j.get("status")} for j in jobs],
            "blocker_count": queue["count"],
            "record_sha256": state["record_sha256"],
        }, sort_keys=True))
        return 0 if healthy else 2
    finally:
        release_lock()

if __name__ == "__main__":
    raise SystemExit(main())
