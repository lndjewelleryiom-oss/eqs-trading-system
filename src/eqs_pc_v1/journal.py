from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple
import hashlib
import json

from .validator import canonical_json, sha256_obj

JOURNAL_VERSION = "1.0.0"
EVENT_DOMAIN = b"EQS-PC-JOURNAL-EVENT-V1\x00"
SEAL_DOMAIN = b"EQS-PC-JOURNAL-SEAL-V1\x00"
EMPTY_HEAD = hashlib.sha256(b"EQS-PC-JOURNAL-EMPTY-V1\x00").hexdigest()


def _domain_hash(domain: bytes, obj: Any) -> str:
    return hashlib.sha256(domain + canonical_json(obj)).hexdigest()


@dataclass(frozen=True)
class JournalEvent:
    journal_version: str
    run_id: str
    sequence: int
    event_type: str
    occurred_at: str
    payload_json: str
    payload_sha256: str
    previous_event_sha256: str
    event_sha256: str

    @property
    def payload(self) -> Dict[str, Any]:
        return json.loads(self.payload_json)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journal_version": self.journal_version,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "previous_event_sha256": self.previous_event_sha256,
            "event_sha256": self.event_sha256,
        }


@dataclass(frozen=True)
class JournalSeal:
    seal_type: str
    journal_version: str
    run_id: str
    event_count: int
    head_sha256: str
    event_type_counts: Dict[str, int]
    seal_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seal_type": self.seal_type,
            "journal_version": self.journal_version,
            "run_id": self.run_id,
            "event_count": self.event_count,
            "head_sha256": self.head_sha256,
            "event_type_counts": dict(sorted(self.event_type_counts.items())),
            "seal_sha256": self.seal_sha256,
        }


@dataclass(frozen=True)
class ImmutableLifecycleJournal:
    run_id: str
    events: Tuple[JournalEvent, ...] = ()

    @property
    def head_sha256(self) -> str:
        return self.events[-1].event_sha256 if self.events else EMPTY_HEAD

    def append(self, event_type: str, occurred_at: str, payload: Dict[str, Any]) -> "ImmutableLifecycleJournal":
        # Canonical round-trip prevents caller mutation from changing an already appended event.
        payload_json = canonical_json(payload).decode("utf-8")
        frozen_payload = json.loads(payload_json)
        payload_hash = sha256_obj(frozen_payload)
        sequence = len(self.events) + 1
        previous = self.head_sha256
        core = {
            "journal_version": JOURNAL_VERSION,
            "run_id": self.run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload_sha256": payload_hash,
            "previous_event_sha256": previous,
        }
        event_hash = _domain_hash(EVENT_DOMAIN, core)
        event = JournalEvent(
            journal_version=JOURNAL_VERSION,
            run_id=self.run_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            payload_json=payload_json,
            payload_sha256=payload_hash,
            previous_event_sha256=previous,
            event_sha256=event_hash,
        )
        return ImmutableLifecycleJournal(self.run_id, self.events + (event,))

    def verify(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        previous = EMPTY_HEAD
        for expected_sequence, event in enumerate(self.events, 1):
            if event.run_id != self.run_id:
                errors.append(f"event {expected_sequence}: run_id mismatch")
            if event.sequence != expected_sequence:
                errors.append(f"event {expected_sequence}: sequence mismatch")
            if event.previous_event_sha256 != previous:
                errors.append(f"event {expected_sequence}: previous hash mismatch")
            try:
                payload = json.loads(event.payload_json)
                if sha256_obj(payload) != event.payload_sha256:
                    errors.append(f"event {expected_sequence}: payload hash mismatch")
            except Exception as exc:
                errors.append(f"event {expected_sequence}: invalid payload json: {exc}")
                payload = None
            core = {
                "journal_version": event.journal_version,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "payload_sha256": event.payload_sha256,
                "previous_event_sha256": event.previous_event_sha256,
            }
            if _domain_hash(EVENT_DOMAIN, core) != event.event_sha256:
                errors.append(f"event {expected_sequence}: event hash mismatch")
            previous = event.event_sha256
        return (not errors, errors)

    def seal(self) -> JournalSeal:
        ok, errors = self.verify()
        if not ok:
            raise ValueError("cannot seal invalid journal: " + "; ".join(errors))
        counts: Dict[str, int] = {}
        for event in self.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        core = {
            "seal_type": "EQS-PC-LIFECYCLE-JOURNAL-SEAL-V1",
            "journal_version": JOURNAL_VERSION,
            "run_id": self.run_id,
            "event_count": len(self.events),
            "head_sha256": self.head_sha256,
            "event_type_counts": dict(sorted(counts.items())),
        }
        return JournalSeal(**core, seal_sha256=_domain_hash(SEAL_DOMAIN, core))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journal_version": JOURNAL_VERSION,
            "run_id": self.run_id,
            "events": [e.to_dict() for e in self.events],
            "head_sha256": self.head_sha256,
        }
