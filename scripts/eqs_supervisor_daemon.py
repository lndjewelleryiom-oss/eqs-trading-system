from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
EV = ROOT / "artifacts" / "test-evidence"
SUPERVISOR = ROOT / "scripts" / "eqs_supervisor.py"
LOCK = EV / ".eqs_supervisor_daemon.lock"
STATE = EV / "EQS_SUPERVISOR_DAEMON_STATE.json"
INTERVAL_SECONDS = 60

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def pid_alive(pid: int) -> bool:
    cp = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    return cp.returncode == 0 and str(pid) in cp.stdout and "No tasks" not in cp.stdout

def acquire() -> bool:
    if LOCK.exists():
        try:
            text = LOCK.read_text(encoding="utf-8").strip()
            old_pid = int(text.split()[0])
            if pid_alive(old_pid):
                return False
        except Exception:
            pass
        try:
            LOCK.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {utcnow()}".encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False

def write_state(status: str, **extra) -> None:
    doc = {
        "schema_id": "EQS-SUPERVISOR-DAEMON-STATE-V1",
        "pid": os.getpid(),
        "updated_at": utcnow(),
        "status": status,
        "interval_seconds": INTERVAL_SECONDS,
        **extra,
    }
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE)

def main() -> int:
    if not acquire():
        return 0
    try:
        write_state("RUNNING")
        while True:
            started = time.time()
            try:
                cp = subprocess.run(
                    [sys.executable, str(SUPERVISOR)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=240,
                )
                write_state(
                    "RUNNING",
                    last_cycle_returncode=cp.returncode,
                    last_cycle_stdout=cp.stdout.strip()[-2000:],
                    last_cycle_stderr=cp.stderr.strip()[-2000:],
                    last_cycle_completed_at=utcnow(),
                )
            except Exception as exc:
                write_state(
                    "DEGRADED",
                    last_cycle_error=type(exc).__name__ + ":" + str(exc),
                    last_cycle_completed_at=utcnow(),
                )
            elapsed = time.time() - started
            time.sleep(max(5, INTERVAL_SECONDS - min(elapsed, INTERVAL_SECONDS - 5)))
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass

if __name__ == "__main__":
    raise SystemExit(main())
