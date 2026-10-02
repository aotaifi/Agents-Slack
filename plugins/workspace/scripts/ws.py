#!/usr/bin/env python3
"""Entry point for the Workspace plugin: hooks and /workspace:* commands."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


def hook():
    # Hooks never fail the session. Unconnected sessions exit before any network use.
    raw = sys.stdin.read(1048577)
    if len(raw) > 1048576:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    import wsplugin

    result = wsplugin.run_hook(payload)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["hook"]:
        raise SystemExit(hook())
    import wsplugin

    raise SystemExit(wsplugin.main())
