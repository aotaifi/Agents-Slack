import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import make_engine, session_factory
from .identity import choose_handle
from .main import actor_json
from .mention_email import sweep
from .models import Actor
from .security import issue_token


def bootstrap(name, output=None, database_url=None):
    if not name.strip() or len(name) > 200:
        raise ValueError("Name must contain 1 to 200 nonblank characters")
    engine = make_engine(database_url)
    try:
        with session_factory(engine)() as db:
            if engine.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            else:
                db.execute(text("SELECT pg_advisory_xact_lock(731958240)"))
            if db.scalar(select(Actor.id).limit(1)):
                raise ValueError("Bootstrap refused: an actor already exists")
            actor = Actor(
                name=name.strip(), handle=choose_handle(db, name), kind="human", is_admin=True
            )
            db.add(actor)
            db.flush()
            result = {"actor": actor_json(actor), "token": issue_token(db, actor)}
            # O_EXCL avoids overwriting an operator file; permissions apply at creation.
            if output:
                path = Path(output)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w") as handle:
                        json.dump(result, handle)
                        handle.write("\n")
                    db.commit()
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
            else:
                db.commit()
            return result
    except SQLAlchemyError:
        raise ValueError("Database operation failed; run alembic upgrade head first") from None
    finally:
        engine.dispose()


def send_mention_emails(database_url=None):
    """Retry pending mention emails; returns how many emails were submitted."""
    engine = make_engine(database_url)
    try:
        return sweep(session_factory(engine))
    except SQLAlchemyError:
        raise ValueError("Database operation failed; run alembic upgrade head first") from None
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Agent Commons offline administration")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("bootstrap")
    command.add_argument("--name", required=True)
    command.add_argument("--output")
    commands.add_parser(
        "send-mention-emails", help="send pending mention emails (retries after failures)"
    )
    args = parser.parse_args()
    if args.command == "send-mention-emails":
        try:
            print(f"Mention emails submitted: {send_mention_emails()}")
        except ValueError as error:
            parser.exit(1, f"{error}\n")
        return
    try:
        result = bootstrap(args.name, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    if args.output:
        print(f"Credentials written to {args.output}")
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
