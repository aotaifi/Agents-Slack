#!/usr/bin/env python3
"""Register one human using an administrator's credentials, without printing tokens."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError, Client  # noqa: E4…8774 tokens truncated…pends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id)
        snapshot = p.cursor
        rows = list(
            db.scalars(
                select(Event)
                .where(Event.project_id == p.id, Event.id > after, Event.id <= snapshot)
                .order_by(Event.id)
                .limit(limit + 1)
            )
        )
        return {
            "items": [event_json(e) for e in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            "cursor": snapshot,
        }

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static / "index.html")

    return app


app = create_app()
