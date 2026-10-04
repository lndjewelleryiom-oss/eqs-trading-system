from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import base64
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class RawCaptureRecord:
    venue: str
    channel: str
    available_at: datetime
    received_at: datetime
    raw_sha256: str
    size_bytes: int
    raw_b64: str

    @classmethod
    def from_bytes(
        cls,
        *,
        venue: str,
        channel: str,
        payload: bytes,
        available_at: datetime,
        received_at: datetime,
    ) -> "RawCaptureRecord":
        if available_at.tzinfo is None or received_at.tzinfo is None:
            raise ValueError("capture timestamps must be timezone-aware")
        if received_at < available_at:
            raise ValueError("received_at cannot precede available_at")
        return cls(
            venue=venue,
            channel=channel,
            available_at=available_at.astimezone(timezone.utc),
            received_at=received_at.astimezone(timezone.utc),
            raw_sha256=sha256(payload).hexdigest(),
            size_bytes=len(payload),
            raw_b64=base64.b64encode(payload).decode("ascii"),
        )

    def payload(self) -> bytes:
        raw = base64.b64decode(self.raw_b64.encode("ascii"), validate=True)
        if len(raw) != self.size_bytes or sha256(raw).hexdigest() != self.raw_sha256:
            raise RuntimeError("persistent raw-capture record failed integrity verification")
        return raw


class PersistentRawCapture:
    """Append-only JSONL raw capture with fsync-backed durability.

    This is intentionally simple and auditable. Production object storage can implement the
    same record contract, but a successful append is not counted until flush+fsync succeeds.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: RawCaptureRecord) -> None:
        row = asdict(record)
        row["available_at"] = record.available_at.isoformat()
        row["received_at"] = record.received_at.isoformat()
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            import os
            os.fsync(handle.fileno())

    def records(self) -> tuple[RawCaptureRecord, ...]:
        if not self.path.exists():
            return ()
        rows: list[RawCaptureRecord] = []
        for line in self.path.read_text().splitlines():
            item = json.loads(line)
            rows.append(RawCaptureRecord(
                venue=item["venue"], channel=item["channel"],
                available_at=datetime.fromisoformat(item["available_at"]),
                received_at=datetime.fromisoformat(item["received_at"]),
                raw_sha256=item["raw_sha256"], size_bytes=int(item["size_bytes"]),
                raw_b64=item["raw_b64"],
            ))
        return tuple(rows)


@dataclass(slots=True)
class DropAccounting:
    socket_frames_received: int = 0
    raw_records_persisted: int = 0
    parse_failures: int = 0
    persistence_failures: int = 0
    sequence_gaps: int = 0
    forced_disconnects: int = 0
    reconnects: int = 0
    resynchronizations: int = 0

    @property
    def detected_drops(self) -> int:
        return self.parse_failures + self.persistence_failures + self.sequence_gaps

    @property
    def persistence_gap(self) -> int:
        return max(0, self.socket_frames_received - self.raw_records_persisted)

    def assert_consistent(self) -> None:
        if self.raw_records_persisted > self.socket_frames_received:
            raise ValueError("persisted records cannot exceed received frames")
        if self.persistence_gap != self.persistence_failures:
            raise ValueError("every unpersisted received frame must be explicitly accounted")


@dataclass(frozen=True, slots=True)
class HealthCheckpoint:
    checkpoint_at: datetime
    elapsed: timedelta
    counters: dict[str, int]
    healthy: bool
    reasons: tuple[str, ...] = ()


class HourlyCheckpointSchedule:
    def __init__(self, started_at: datetime, period: timedelta = timedelta(hours=1)):
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if period <= timedelta(0):
            raise ValueError("checkpoint period must be positive")
        self.started_at = started_at
        self.period = period
        self._next = started_at + period

    def due(self, now: datetime, counters: DropAccounting) -> tuple[HealthCheckpoint, ...]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        out: list[HealthCheckpoint] = []
        while now >= self._next:
            reasons: list[str] = []
            if counters.persistence_gap:
                reasons.append("raw-persistence-gap")
            if counters.sequence_gaps:
                reasons.append("sequence-gap")
            out.append(HealthCheckpoint(
                checkpoint_at=self._next,
                elapsed=self._next - self.started_at,
                counters={
                    "socket_frames_received": counters.socket_frames_received,
                    "raw_records_persisted": counters.raw_records_persisted,
                    "parse_failures": counters.parse_failures,
                    "persistence_failures": counters.persistence_failures,
                    "sequence_gaps": counters.sequence_gaps,
                    "forced_disconnects": counters.forced_disconnects,
                    "reconnects": counters.reconnects,
                    "resynchronizations": counters.resynchronizations,
                    "detected_drops": counters.detected_drops,
                },
                healthy=not reasons,
                reasons=tuple(reasons),
            ))
            self._next += self.period
        return tuple(out)


@dataclass(frozen=True, slots=True)
class BackfillCoverage:
    expected_buckets: int
    observed_buckets: int
    missing_buckets: tuple[int, ...]
    duplicate_buckets: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return self.observed_buckets == self.expected_buckets and not self.missing_buckets and not self.duplicate_buckets


def reconcile_fixed_interval_backfill(
    timestamps_ms: Iterable[int], *, start_ms: int, end_ms: int, interval_ms: int
) -> BackfillCoverage:
    if interval_ms <= 0 or end_ms < start_ms:
        raise ValueError("invalid reconciliation interval/window")
    expected = tuple(range(start_ms, end_ms + 1, interval_ms))
    counts: dict[int, int] = {}
    for ts in timestamps_ms:
        if start_ms <= ts <= end_ms and (ts - start_ms) % interval_ms == 0:
            counts[ts] = counts.get(ts, 0) + 1
    missing = tuple(ts for ts in expected if counts.get(ts, 0) == 0)
    duplicates = tuple(ts for ts, count in sorted(counts.items()) if count > 1)
    return BackfillCoverage(
        expected_buckets=len(expected), observed_buckets=len(counts),
        missing_buckets=missing, duplicate_buckets=duplicates,
    )
