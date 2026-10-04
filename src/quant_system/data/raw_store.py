from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredBlob:
    content_hash: str
    uri: str
    size_bytes: int


class ImmutableRawStore:
    """Content-addressed local raw store used by the first data-plane implementation.

    Content-addressed writes are idempotent. Existing content is never overwritten.
    Production object-storage adapters can implement the same contract.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: bytes) -> StoredBlob:
        digest = sha256(payload).hexdigest()
        path = self.root / digest[:2] / digest[2:4] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if sha256(existing).hexdigest() != digest:
                raise RuntimeError("content-address collision or raw-store corruption")
        else:
            path.write_bytes(payload)
        return StoredBlob(content_hash=digest, uri=str(path), size_bytes=len(payload))

    def get(self, content_hash: str) -> bytes:
        path = self.root / content_hash[:2] / content_hash[2:4] / content_hash
        payload = path.read_bytes()
        if sha256(payload).hexdigest() != content_hash:
            raise RuntimeError("raw-store corruption detected")
        return payload
