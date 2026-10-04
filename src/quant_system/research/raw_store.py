from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse
import os


class RawStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RawObjectDigest:
    uri: str
    sha256: str
    byte_count: int


def _hash_stream(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    h = sha256()
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
    return h.hexdigest(), total


def _parse_s3_root(root: str) -> tuple[str, str]:
    parsed = urlparse(root)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise RawStoreError("invalid s3 root URI")
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/").rstrip("/")
    return bucket, prefix


def _join_key(prefix: str, filename: str) -> str:
    clean = filename.replace("\\", "/").lstrip("/")
    return f"{prefix}/{clean}" if prefix else clean


def _s3_client_from_env():
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        raise RawStoreError("boto3/botocore unavailable") from exc

    access = os.environ.get("R13_RAW_S3_ACCESS_KEY", "").strip()
    secret = os.environ.get("R13_RAW_S3_SECRET_KEY", "").strip()
    endpoint = os.environ.get("R13_RAW_S3_ENDPOINT", "").strip() or None
    region = os.environ.get("R13_RAW_S3_REGION", "").strip() or "auto"
    if not access or not secret:
        raise RawStoreError("R13 raw S3 credentials are not configured")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def verify_raw_object(
    remote_raw_root: str | Path,
    filename: str,
    expected_sha256: str,
    *,
    s3_client=None,
) -> RawObjectDigest:
    """Re-hash a retained raw object from local disk or S3-compatible storage."""
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise RawStoreError("expected_sha256 must be lowercase SHA-256")

    root_text = str(remote_raw_root)
    if root_text.startswith("s3://"):
        bucket, prefix = _parse_s3_root(root_text)
        key = _join_key(prefix, filename)
        client = s3_client or _s3_client_from_env()
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            digest, size = _hash_stream(body)
        except Exception as exc:
            raise RawStoreError(f"unable to read s3 raw object: {filename}") from exc
        if digest != expected_sha256:
            raise RawStoreError(f"raw object hash mismatch: {filename}")
        return RawObjectDigest(
            uri=f"s3://{bucket}/{key}",
            sha256=digest,
            byte_count=size,
        )

    path = Path(remote_raw_root) / filename
    if not path.is_file():
        raise RawStoreError(f"raw object missing: {filename}")
    with path.open("rb") as handle:
        digest, size = _hash_stream(handle)
    if digest != expected_sha256:
        raise RawStoreError(f"raw object hash mismatch: {filename}")
    return RawObjectDigest(uri=str(path), sha256=digest, byte_count=size)


def upload_raw_file(
    local_path: str | Path,
    remote_raw_root: str,
    filename: str,
    *,
    s3_client=None,
) -> RawObjectDigest:
    """Upload a local staging file to S3-compatible immutable raw storage.

    The caller is responsible for choosing a content-stable filename. This
    function verifies the uploaded bytes by re-reading and hashing the object.
    """
    path = Path(local_path)
    if not path.is_file():
        raise RawStoreError("local staging file missing")
    with path.open("rb") as handle:
        local_sha, local_size = _hash_stream(handle)

    bucket, prefix = _parse_s3_root(remote_raw_root)
    key = _join_key(prefix, filename)
    client = s3_client or _s3_client_from_env()

    try:
        with path.open("rb") as handle:
            client.upload_fileobj(
                handle,
                bucket,
                key,
                ExtraArgs={
                    "Metadata": {
                        "sha256": local_sha,
                        "eqs-classification": "real-market-raw",
                    }
                },
            )
    except Exception as exc:
        raise RawStoreError(f"unable to upload s3 raw object: {filename}") from exc

    verified = verify_raw_object(
        remote_raw_root,
        filename,
        local_sha,
        s3_client=client,
    )
    if verified.byte_count != local_size:
        raise RawStoreError(f"raw object byte-count mismatch: {filename}")
    return verified
