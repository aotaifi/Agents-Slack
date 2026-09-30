#!/usr/bin/env python3
"""Create local development configuration without displaying credentials."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
local = root / ".local"
local.mkdir(mode=0o700, exist_ok=True)
path = root / ".env"
if path.exists():
    print(".env already exists; kept existing configuration.")
else:
    content = f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}\nPOSTGRES_PORT=54329\nAPP_PORT=8000\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    print("Created private .env and .local/ development configuration.")
