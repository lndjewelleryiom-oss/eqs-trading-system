from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from quant_system.research.raw_store import (
    RawStoreError,
    upload_raw_file,
    verify_raw_object,
)


class FakeBody(BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, handle, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = handle.read()

    def get_object(self, Bucket, Key):
        try:
            raw = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise RuntimeError("missing object") from exc
        return {"Body": FakeBody(raw)}


def test_local_raw_verification(tmp_path):
    raw = b"genuine-provider-bytes"
    path = tmp_path / "raw" / "a.bin"
    path.parent.mkdir()
    path.write_bytes(raw)
    expected = sha256(raw).hexdigest()
    result = verify_raw_object(path.parent, "a.bin", expected)
    assert result.sha256 == expected
    assert result.byte_count == len(raw)


def test_local_hash_mismatch_fails_closed(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    with pytest.raises(RawStoreError, match="hash mismatch"):
        verify_raw_object(tmp_path, "a.bin", "0" * 64)


def test_s3_upload_and_rehash(tmp_path):
    local = tmp_path / "source.bin"
    raw = b"historical-market-raw" * 100
    local.write_bytes(raw)
    fake = FakeS3()

    uploaded = upload_raw_file(
        local,
        "s3://eqs-raw/r13",
        "BYBIT_LINEAR/trades/2023-01-01/BTCUSDT.csv.gz",
        s3_client=fake,
    )
    assert uploaded.sha256 == sha256(raw).hexdigest()
    assert uploaded.byte_count == len(raw)
    assert uploaded.uri.startswith("s3://eqs-raw/r13/")

    verified = verify_raw_object(
        "s3://eqs-raw/r13",
        "BYBIT_LINEAR/trades/2023-01-01/BTCUSDT.csv.gz",
        uploaded.sha256,
        s3_client=fake,
    )
    assert verified == uploaded


def test_s3_tamper_fails_closed(tmp_path):
    local = tmp_path / "source.bin"
    local.write_bytes(b"good")
    fake = FakeS3()
    result = upload_raw_file(local, "s3://bucket/prefix", "x.bin", s3_client=fake)
    fake.objects[("bucket", "prefix/x.bin")] = b"tampered"
    with pytest.raises(RawStoreError, match="hash mismatch"):
        verify_raw_object(
            "s3://bucket/prefix",
            "x.bin",
            result.sha256,
            s3_client=fake,
        )
