"""Create a private, verified SQLite backup without stopping the pilot server."""

import argparse
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def backup(database, destination_dir):
    source_path = Path(database).resolve()
    if not source_path.is_file():
        raise ValueError("Source database does not exist")
    destination = Path(destination_dir).resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = destination / f"workspace-{stamp}-{uuid4().hex[:8]}.db"
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=destination)
    os.close(fd)
    try:
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup integrity check failed")
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        # A hard link publishes the completed file atomically and refuses overwrite.
        os.link(temporary, output)
        directory = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return output
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--destination-dir", required=True)
    args = parser.parse_args()
    try:
        output = backup(args.database, args.destination_dir)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(json.dumps({"backup": str(output), "integrity": "ok"}))


if __name__ == "__main__":
    main()
