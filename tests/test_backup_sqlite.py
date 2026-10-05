import importlib.util
import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import auth, setup_thread
from fastapi.testclient import TestClient

from agent_commons.cli import bootstrap
from agent_commons.db import Base, make_engine
from agent_commons.main import create_app

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


def test_restore_round_trip_serves_the_same_thread(tmp_path):
    """Back up a live workspace with the real script, restore the file, serve it again."""
    url = f"sqlite:///{tmp_path}/live.db"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    owner = bootstrap("Owner", database_url=url)
    app = create_app(url)
    with TestClient(app) as client:
        client.headers.update(auth(owner["token"]))
        pid, tid = setup_thread(client)
        first = client.post(f"/v1/threads/{tid}/messages", json={"text": "Evidence ☃"}).json()
        client.post(
            f"/v1/threads/{tid}/messages", json={"text": "Reply", "reply_to": first["id"]}
        )
        before = client.get(f"/v1/threads/{tid}/messages").json()
        events_before = client.get(f"/v1/projects/{pid}/events").json()
        # The server is still running while the backup is taken, through the real interface.
        script = Path(module.__file__)
        done = subprocess.run(
            [sys.executable, str(script), "--database", str(tmp_path / "live.db"),
             "--destination-dir", str(tmp_path / "backups")],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        backup = Path(json.loads(done.stdout)["backup"])
        # A later write must not leak into the already-taken backup.
        client.post(f"/v1/threads/{tid}/messages", json={"text": "After the backup"})
    app.state.engine.dispose()

    restored_path = tmp_path / "restored" / "workspace.db"
    restored_path.parent.mkdir()
    shutil.copyfile(backup, restored_path)
    restored = create_app(f"sqlite:///{restored_path}")
    with TestClient(restored) as other:
        other.headers.update(auth(owner["token"]))  # credentials survive the restore
        assert other.get(f"/v1/threads/{tid}/messages").json() == before
        assert other.get(f"/v1/projects/{pid}/events").json() == events_before
        again = other.post(f"/v1/threads/{tid}/messages", json={"text": "Writable"})
        assert again.status_code == 201
        assert again.json()["sequence"] == events_before["cursor"] + 1
    restored.state.engine.dispose()
