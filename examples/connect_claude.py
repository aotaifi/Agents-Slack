"""Prepare session-only Claude settings, run quiet notification hooks, inspect/ack/reply.

Launch Claude explicitly with the generated --settings file. No user/project
settings are edited, no model is spawned, and mentions are never answered by hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError  # noqa: E402
from claude_workspace import AdapterError, Workspace, prepare  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="private session adapter config")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--credentials", required=True)
    create.add_argument(
        "--output-dir", required=True, help="new private directory; never overwrite"
    )
    create.add_argument("--url")
    create.add_argument("--project")
    create.add_argument("--label")
    create.add_argument("--cwd", required=True, help="chosen Claude project root")
    create.add_argument("--throttle", type=float, default=30)
    create.add_argument("--replay-backlog", action="store_true")
    for action in ("hook", "status", "pending", "ack", "reply"):
        command = sub.add_parser(action)
        command.add_argument("--config", default=argparse.SUPPRESS)
        if action == "reply":
            command.add_argument("--text-file", help="UTF-8 reply text; otherwise read stdin")
            command.add_argument("--mention", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare(
                args.credentials,
                args.output_dir,
                url=args.url,
                project_id=args.project,
                label=args.label,
                cwd=args.cwd,
                script=__file__,
                python=sys.executable,
                throttle=args.throttle,
                replay_backlog=args.replay_backlog,
            )
        else:
            if not args.config:
                raise AdapterError("Supply the private --config file.")
            workspace = Workspace(args.config)
            if args.action == "hook":
                # Ignore transcript/tool output: only the small identity/event envelope is read.
                raw = sys.stdin.read(1048577)
                if len(raw) > 1048576:
                    return 0
                result = workspace.hook(json.loads(raw))
            else:
                text = None
                if args.action == "reply":
                    if args.text_file:
                        with Path(args.text_file).open(encoding="utf-8") as stream:
                            text = stream.read(20001)
                    else:
                        text = sys.stdin.read(20001)
                result = workspace.command(
                    args.action,
                    session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    text=text,
                    mentions=getattr(args, "mention", ()),
                )
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        if args.action == "hook":
            return 0
        if isinstance(exc, AdapterError):
            explanation = str(exc)
        elif isinstance(exc, ApiError):
            explanation = (
                f"Workspace request failed (HTTP {exc.status}); check access/lease/network."
            )
        else:
            explanation = (
                "Adapter operation failed; check private files, network, and configuration."
            )
        print(explanation, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
