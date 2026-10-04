import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class HashChainedAuditLog:
    """Append-only JSONL audit log with simple hash chaining for tamper evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return "GENESIS"
        last = self.path.read_text(encoding="utf-8").strip().splitlines()[-1]
        return json.loads(last)["hash"]

    def append(self, event_type: str, payload: Any) -> str:
        if is_dataclass(payload):
            payload = asdict(payload)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
            "prev_hash": self._prev_hash,
        }
        canonical = json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        record["hash"] = digest
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._prev_hash = digest
        return digest

    def verify(self) -> bool:
        prev = "GENESIS"
        if not self.path.exists():
            return True
        for line in self.path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            digest = item.pop("hash")
            if item["prev_hash"] != prev:
                return False
            canonical = json.dumps(item, sort_keys=True, default=str, separators=(",", ":"))
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
                return False
            prev = digest
        return True
