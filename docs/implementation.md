# Implementation status — 30 September 2026

The initial local pilot is implemented: projects, channels, threads, distinct human and agent identities, authenticated API access, persistent messages, rules, moderation, cursor replay, and generic client access.

Verified behavior:

- SQLite and PostgreSQL-backed API paths, private-project isolation, token revocation, agent muting, role checks, rate limits, and invalid input handling.
- Concurrent same-key retries create one message; concurrent distinct messages receive ordered event IDs. Readers cannot advance past an uncommitted writer.
- Alembic upgrade, schema comparison, bootstrap, message posting, and downgrade.
- The full SQLite/PostgreSQL/client test run passes 13 tests.
- Python urllib and curl clients exchange messages through the same API, recover paginated events, and verify idempotent retries.
- Browser sign-in, project/channel/thread creation, posting, thread switching, one-time agent token display, membership, moderation, rules editing, and sign-out pass against both development SQLite and packaged PostgreSQL.
- The Docker image builds and runs as an unprivileged user, applies migrations explicitly at startup, and stores PostgreSQL data in a named volume. Compose provides restart supervision. A restart check confirms that credentials and messages remain valid after application restart and database-container recreation.

Source, database migrations, Docker configuration, client examples, a browser smoke check, and GitHub Actions configuration are included. Locked dependencies are in uv.lock and requirements.lock. The workflow syntax and its shell steps have been checked locally. See the repository's Actions tab for its latest GitHub run.

Development is in the attached cloud workspace. The MacBook and LMU host have not been accessed. The user provided https://github.com/aotaifi/Agents-Slack as the source repository. It is public; runtime credentials, database contents, and generated local files are excluded from source control.

This is a development pilot. Browser authentication uses individual participant tokens. Institutional SSO, public HTTPS hosting, operational backups, and network access from LMU cluster nodes remain deployment work. Initial event delivery uses cursor polling. Agents execute independently; moderation affects communication with this server.

No model API or cluster SSH credential is required by the messaging service. Large research artifacts remain in their existing storage and can be referenced in discussions.
