Build an agent-agnostic messaging service. No provider SDK or model API key is needed for the transport.
Use Python 3.12+, FastAPI, SQLAlchemy, PostgreSQL, and a small vanilla HTML/CSS/JS browser interface.
The shared API contract is docs/api-contract.md. Coordinate changes to that contract before changing interfaces.
Never commit runtime credentials, .env files, database files, or generated .local data.
Verify access control, persistence, cursor replay, idempotency, and moderation with meaningful integration tests.
Use uv run pytest and uv run ruff check .; test the database-backed paths against PostgreSQL.
