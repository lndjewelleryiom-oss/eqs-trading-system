from __future__ import annotations

from collections import namedtuple
from pathlib import Path
import sqlite3

import pytest

from quant_system.operations.sqlite_backup import (
    create_sqlite_backup,
    restore_sqlite_backup,
    verify_sqlite_database,
)


Usage = namedtuple("Usage", "total used free")


def seed(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE runtime_state(runtime_id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE runtime_events(id INTEGER PRIMARY KEY, runtime_id TEXT NOT NULL, event_type TEXT NOT NULL);
            CREATE TABLE runtime_checkpoints(id INTEGER PRIMARY KEY, runtime_id TEXT NOT NULL, payload TEXT NOT NULL);
            INSERT INTO runtime_state VALUES('paper-main','RUNNING');
            INSERT INTO runtime_events(runtime_id,event_type) VALUES('paper-main','HEARTBEAT');
            INSERT INTO runtime_checkpoints(runtime_id,payload) VALUES('paper-main','{}');
            """
        )
        con.commit()


def test_consistent_backup_and_isolated_restore(tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    seed(source)

    report = create_sqlite_backup(
        source,
        backup,
        expected_tables=("runtime_state", "runtime_events", "runtime_checkpoints"),
        reserve_bytes=0,
    )
    assert report.source.row_counts == {
        "runtime_checkpoints": 1,
        "runtime_events": 1,
        "runtime_state": 1,
    }
    assert report.backup.row_counts == report.source.row_counts
    assert report.backup.integrity_check == "ok"
    assert report.isolated_restore_only is True
    assert report.broker_submission_enabled is False
    assert report.live_authority is False

    restore = restore_sqlite_backup(
        backup,
        restored,
        expected_tables=("runtime_state", "runtime_events", "runtime_checkpoints"),
        reserve_bytes=0,
    )
    assert restore.row_counts == report.source.row_counts
    assert verify_sqlite_database(restored).integrity_check == "ok"


def test_backup_fails_closed_without_capacity(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    seed(source)
    monkeypatch.setattr(
        "quant_system.operations.sqlite_backup.shutil.disk_usage",
        lambda path: Usage(1000, 999, 1),
    )
    with pytest.raises(RuntimeError, match="INSUFFICIENT_BACKUP_CAPACITY"):
        create_sqlite_backup(source, tmp_path / "backup.db", reserve_bytes=1024)
    assert not (tmp_path / "backup.db").exists()


def test_backup_never_overwrites_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    destination = tmp_path / "backup.db"
    seed(source)
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        create_sqlite_backup(source, destination, reserve_bytes=0)
    assert destination.read_bytes() == b"existing"


def test_restore_refuses_in_place_or_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    backup = tmp_path / "backup.db"
    seed(source)
    create_sqlite_backup(source, backup, reserve_bytes=0)
    with pytest.raises(ValueError):
        restore_sqlite_backup(backup, backup, reserve_bytes=0)
    target = tmp_path / "target.db"
    target.write_bytes(b"do-not-overwrite")
    with pytest.raises(FileExistsError):
        restore_sqlite_backup(backup, target, reserve_bytes=0)
