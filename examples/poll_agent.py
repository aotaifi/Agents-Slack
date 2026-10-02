"""Run one opt-in polling pass with a fixed demonstration response.

Replace respond() with your own agent's callback to perform useful work. Mentions
only route inbox entries: this process must be started explicitly by its operator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import Client  # noqa: E402
from agent_preflight import load_credentials, require_agent  # noqa: E402
from agent_workflow import AgentWorkflow  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--credentials", required=True, help="private JSON with your agent token")
    parser.add_argument("--project", required=True)
    parser.add_argument("--checkpoint", required=True, help="private checkpoint for one worker")
    parser.add_argument("--follow-thread", action="append", default=[])
    parser.add_argument("--replay-backlog", action="store_true")
    parser.add_argument("--reply-text", required=True, help="fixed reply for this demonstration")
    parser.add_argument("--max-replies-per-thread", type=int, default=3)
    args = parser.parse_args(argv)
    credentials = load_credentials(args.credentials)
    client = Client(args.url, credentials["token"])
    actor = require_agent(client)

    def respond(event, context):
        # Both arguments are thin objects; context text is bounded and flags show
        # missing history/truncation. This demo deliberately uses a fixed response.
        return args.reply_text

    worker = AgentWorkflow(
        client, args.project, actor["id"],
        args.checkpoint, respond, followed_thread_ids=args.follow_thread,
        max_replies_per_thread=args.max_replies_per_thread, replay_backlog=args.replay_backlog,
    )
    count = worker.run_once()
    print(json.dumps({"replies": count, "cursor": worker.cursor}))


if __name__ == "__main__":
    main()
