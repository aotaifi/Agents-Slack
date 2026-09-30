#!/usr/bin/env python3
"""Register one human using an administrator's credentials, without printing tokens."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError, Client  # noqa: E402


def private_parents(directory):
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700, exist_ok=True)


def register_researcher(url, admin_credentials, name, output, handle=None):
    with Path(admin_credentials).open(encoding="utf-8") as source:
        credentials = json.load(source)
    token = credentials.get("token") if isinstance(credentials, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Administrator credentials must contain a nonempty token")
    destination = Path(output)
    private_parents(destination.parent)
    # Reserve exclusively before the network call: an existing file or a concurrent
    # invocation cannot cause another actor to be created with an unsaved token.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as saved:
            client = Client(url, token)
            administrator = client.request("GET", "me")
            if administrator.get("kind") != "human" or administrator.get("is_admin") is not True:
                raise ValueError("A human administrator is required to register researchers")
            body = {"name": name, "kind": "human"}
            if handle is not None:
                body["handle"] = handle
            result = client.request("POST", "actors", data=body)
            json.dump({"actor": result["actor"], "token": result["token"]}, saved)
            saved.write("\n")
            saved.flush()
            os.fsync(saved.fileno())
        return result["actor"]
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--admin-credentials", required=True, help="private administrator JSON file"
    )
    parser.add_argument("--name", required=True, help="researcher's chosen display name")
    parser.add_argument("--handle", help="optional chosen lowercase slug; generated if omitted")
    parser.add_argument(
        "--output", required=True, help="new private researcher credentials JSON file"
    )
    args = parser.parse_args(argv)
    try:
        actor = register_researcher(
            args.url, args.admin_credentials, args.name, args.output, args.handle
        )
    except (ApiError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Registration failed: {error}\n")
    print(f"Registered {actor['name']} (@{actor['handle']}, actor ID {actor['id']})")
    print(f"Credentials written to {args.output}")


if __name__ == "__main__":
    main()
