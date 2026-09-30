#!/usr/bin/env python3
"""Run integration tests in a separate local Compose PostgreSQL database."""

import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

root = Path(__file__).resolve().parents[1]
values = dotenv_values(root / ".env")
password = values.get("POSTGRES_PASSWORD")
if not password:
    raise SystemExit("Run python scripts/init-dev.py and start the Compose database first.")
url = URL.create(
    "postgresql+psycopg",
    username="agent_commons",
    password=password,
    host="127.0.0.1",
    port=int(values.get("POSTGRES_PORT") or "54329"),
    database="agent_commons",
)
engine = create_engine(url, isolation_level="AUTOCOMMIT")
with engine.connect() as connection:
    exists = connection.scalar(
        text("SELECT 1 FROM pg_database WHERE datname = :name"),
        {"name": "agent_commons_test"},
    )
    if not exists:
        connection.execute(text("CREATE DATABASE agent_commons_test"))
engine.dispose()
environment = os.environ.copy()
environment["TEST_DATABASE_URL"] = url.set(database="agent_commons_test").render_as_string(
    hide_password=False,
)
raise SystemExit(
    subprocess.run(["uv", "run", "pytest", "-q"], cwd=root, env=environment).returncode
)
