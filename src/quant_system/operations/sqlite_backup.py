from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SqliteVerification:
    path: str
    size_bytes: int
    file_sha256: str
    integrity_check: str
    tables: tuple[str, ...]
    row_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class SqliteBackupReport:
    source: SqliteVerification
    backup: SqliteVerification
    source_size_before: int
    source_size_after: int
    source_stable_during_backup: bool
    isolated_restore_only: bool
    broker_submission_enabled: bool = False
    live_authority: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _uri(path: Path, mode: str) -> str:
    return path.resolve().as_uri() + f"?mode={mode}"


def verify_sqlite_database(
    path: str | Path,
    *,
    expected_tables: Iterable[str] = (),
) -> SqliteVerification:
    db = Path(path)
    if not db.is_file():
        raise FileNotFoundError(db)
    expected = tuple(sorted(set(expected_tables)))
    with sqlite3.connect(_uri(db, "ro"), uri=True, timeout=5.0) as con:
        con.row_factory = sqlite3.Row
        result = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if result.lower() != "ok":
            raise RuntimeError(f"SQLITE_INTEGRITY_CHECK_FAILED:{result}")
        tables = tuple(
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        missing = sorted(set(expected) - set(tables))
        if missing:
            raise RuntimeError("SQLITE_REQUIRED_TABLES_MISSING:" + ",".join(missing))
        row_counts = {
            table: int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in expected
        }
    return SqliteVerification(
        path=str(db.resolve()),
        size_bytes=db.stat().st_size,
        file_sha256=_sha256(db),
        integrity_check=result,
        tables=tables,
        row_counts=row_counts,
    )


def _require_capacity(
    source: Path,
    destination: Path,
    *,
    reserve_bytes: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = int(shutil.disk_usage(destination.parent).free)
    required = int(source.stat().st_size) + int(reserve_bytes)
    if free < required:
        raise RuntimeError(
            f"INSUFFICIENT_BACKUP_CAPACITY:free_bytes={free};required_bytes={required}"
        )


def create_sqlite_backup(
    source: str | Path,
    destination: str | Path,
    *,
    expected_tables: Iterable[str] = (),
    reserve_bytes: int = 1024 ** 3,
) -> SqliteBackupReport:
    src = Path(source).resolve()
    dst = Path(destination).resolve()
    if src == dst:
        raise ValueError("backup destination must differ from source")
    if dst.exists():
        raise FileExistsError(dst)
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be non-negative")
    if not src.is_file():
        raise FileNotFoundError(src)

    _require_capacity(src, dst, reserve_bytes=reserve_bytes)
    before = src.stat().st_size
    source_verification = verify_sqlite_database(src, expected_tables=expected_tables)

    source_uri = _uri(src, "ro")
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_con:
            with sqlite3.connect(dst, timeout=5.0) as backup_con:
                source_con.backup(backup_con)
                backup_con.commit()
    except Exception:
        if dst.exists():
            dst.unlink()
        raise

    after = src.stat().st_size
    backup_verification = verify_sqlite_database(dst, expected_tables=expected_tables)
    if backup_verification.row_counts != source_verification.row_counts:
        raise RuntimeError("SQLITE_BACKUP_ROW_COUNT_MISMATCH")

    return SqliteBackupReport(
        source=source_verification,
        backup=backup_verification,
        source_size_before=before,
        source_size_after=after,
        source_stable_during_backup=(before == after),
        isolated_restore_only=True,
    )


def restore_sqlite_backup(
    backup: str | Path,
    destination: str | Path,
    *,
    expected_tables: Iterable[str] = (),
    reserve_bytes: int = 1024 ** 3,
) -> SqliteVerification:
    src = Path(backup).resolve()
    dst = Path(destination).resolve()
    if src == dst:
        raise ValueError("restore destination must differ from backup")
    if dst.exists():
        raise FileExistsError(dst)
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be non-negative")

    verified = verify_sqlite_database(src, expected_tables=expected_tables)
    _require_capacity(src, dst, reserve_bytes=reserve_bytes)
    try:
        with sqlite3.connect(_uri(src, "ro"), uri=True, timeout=5.0) as source_con:
            with sqlite3.connect(dst, timeout=5.0) as restore_con:
                source_con.backup(restore_con)
                restore_con.commit()
    except Exception:
        if dst.exists():
            dst.unlink()
        raise

    restored = verify_sqlite_database(dst, expected_tables=expected_tables)
    if restored.row_counts != verified.row_counts:
        raise RuntimeError("SQLITE_RESTORE_ROW_COUNT_MISMATCH")
    return restored
