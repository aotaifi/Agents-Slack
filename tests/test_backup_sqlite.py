import importlib.util
import sqlite3
import stat
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "backup_sqlite", Path(__file__).resolve().parents[1] / "scripts" / "backup-sqlite.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_backup_preserves_live_data_and_source_and_private_permissions(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as writer:
        writer.execute("CREATE TABLE research (value TEXT)")
        writer.execute("INSERT INTO research VALUES ('evidence')")
        writer.commit()
        first = module.backup(source, tmp_path / "backups")
        writer.execute("INSERT INTO research VALUES ('new result')")
        writer.commit()
        second = module.backup(source, tmp_path / "backups")
        assert first != second
        assert writer.execute("SELECT count(*) FROM research").fetchone()[0] == 2
    with sqlite3.connect(first) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT value FROM research").fetchall() == [("evidence",)]
    with sqlite3.connect(second) as restored:
        assert restored.execute("SELECT count(*) FROM research").fetchone()[0] == 2
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert not list(first.parent.glob(".backup-*"))


def test_missing_source_does_not_create_empty_database(tmp_path):
    source = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="does not exist"):
        module.backup(source, tmp_path / "backups")
    assert not source.exists()
    assert not (tmp_path / "backups").exists()


def test_destination_collision_never_overwrites_completed_backup(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE results (value TEXT)")
    destination = tmp_path / "backups"
    destination.mkdir()
    preserved = destination / "preserved.db"
    preserved.write_bytes(b"original backup")

    def conflicting_link(temporary, output):
        assert Path(temporary).is_file()
        raise FileExistsError("simulated collision")

    monkeypatch.setattr(module.os, "link", conflicting_link)
    with pytest.raises(FileExistsError):
        module.backup(source, destination)
    assert preserved.read_bytes() == b"original backup"
    assert not list(destination.glob(".backup-*"))
