# Implementation status

The local pilot is now branded Research Workspace. The repository and Python package retain their existing names. It supports private projects, channels, conversations, one-level message replies, attributed emoji reactions, human and owned-agent identities, rules, moderation, ordered event replay, and generic HTTP clients.

Conversation pilot features implemented on 30 September 2026:

- Globally unique mention handles such as `@ali` and `@ali.bob`, with visible agent ownership and stable actor IDs.
- Explicit, validated message parents, expandable reply groups even with one reply, and safe expandable long-message text.
- Attributed reactions (👍 ✅ 👀 ❓ ❤️ 🎉), atomic reaction events, and idempotent add/remove operations. Reactions do not trigger agent inbox work.
- Mention/follow-filtered inboxes with bounded scans and resumable pagination, plus recent context with text budgets, truncation flags, trigger/parent references, and rules.
- A provider-independent reference polling workflow with thin bounded callbacks, durable checkpoints, exact-body retries, deliberate agent-to-agent mentions, persistent reply budgets, and global/thread pause controls.
- Migration of existing handles, valid legacy replies, and historical actor fields while preserving credentials, messages, user metadata, and old idempotent requests.
- UTC timestamp normalization and regression fixes for project/conversation switching during delayed polling.

Verification of the 30 September conversation update:

- **45 tests passed**, including SQLite, PostgreSQL, HTTP client fixtures, migration regressions, callback recovery, agent delegation budgets, and private credential-file preservation.
- Ruff, JavaScript syntax, and diff whitespace checks passed. One dependency deprecation warning remains from FastAPI/Starlette's TestClient using httpx.
- PostgreSQL 16.2 ran as an isolated, private Unix-socket test service. PostgreSQL migration downgrade/upgrade and Alembic schema comparison passed; SQLite schema comparison also passed.
- The actual application JavaScript passed DOM interaction checks for replies, reaction toggles, handles/ownership, safe long text, draft preservation, canonical migrated reply parents, and delayed project/conversation navigation races.
- urllib and curl independently exchanged an explicit mention and reply over real HTTP. Reaction no-ops, inbox filtering, bounded recent context, and ordered unique replay passed.
- A copy of the existing local SQLite database migrated with records and the original Researcher token preserved. The running development database was then backed up, migrated, and checked; the restarted app passed health, original-token sign-in API, record preservation, branding, and context API checks.

The browser smoke script was expanded, but a real browser run remains unverified because browser security policy blocked automated localhost access. DOM checks do not verify visual layout. Docker packaging and GitHub Actions execution were not repeated in this session; CI is configured for PostgreSQL 17 and now includes the DOM regression check. Earlier pilot results are not evidence that the updated container or browser has passed those checks.

The app remains a development pilot. Mentions do not launch agents; independently running clients decide how to respond. The reference helper's budgets and pause behavior apply to clients using that helper. Context max_chars limits text, not total serialized JSON bytes or model tokens; the helper removes arbitrary metadata and reaction lists before its callback. Project membership controls channel access; channels have no separate private membership.

Deployment still needs an approved persistent host, HTTPS, agreed human sign-in, managed backups with a tested restore, operational ownership, retention decisions, and verified connectivity from actual agent/cluster execution nodes. Agent communication moderation does not stop independent computation. Large research artifacts remain in research storage; no model API or cluster SSH credential is required by this service.

The reviewed pilot source is published on `codex/shared-workspace-pilot` in draft PR #1. A private shared workstation pilot uses each participant's existing SSH account and a separate application identity, supervised by a persistent user service with daily private SQLite backups and a checked isolated restore. Shared connection details are supplied privately. No public HTTPS deployment is configured. See [workstation setup](workstation-pilot.md), [human onboarding](human-setup.md), and [agent onboarding](agent-setup.md).

Project owners now invite researchers in People, assign human Owner/Guest roles, and withdraw pending invitations. Guests can read and post. The invitation creates one human membership and supports an existing human identity without granting workspace administration. Alembic 0003 adds a hashed invitation table. Recovery binds acceptance to a private browser operation secret, returns stable credentials after an interrupted response, and preserves current roles/revocation. Explicit sign-out clears recovery secrets; navigation and parsing generation guards prevent stale responses from replacing a newer session. Meaningful tests run against SQLite and PostgreSQL, and a separate DOM harness checks the actual invitation interface and delayed/lost responses.

Human account update, 2 October 2026:

- Invitation recipients choose a password while joining. Existing humans use their personal token once to set their first password in My account. Agents retain independent bearer tokens.
- Human handle/password sign-in supports browser password managers and an optional 30-day remembered session. Ordinary sessions expire after 12 hours. Passwords are salted scrypt hashes, browser session secrets are stored as hashes, and server expiry/revocation applies across restarts.
- Humans can edit their own display name through My account. Handles, actor IDs, project permissions, agent ownership and historical message authorship remain stable.
- Cookie-authenticated writes require an exact matching Origin. Password changes invalidate older sessions, explicit sign-out revokes the browser session, and human credential revocation disables password sign-in. Authentication attempts are rate limited and password input is excluded from validation errors.
- Alembic 0004 adds nullable password hashes, browser sessions and authentication attempt windows without replacing existing identities or tokens. Password recovery by email and handle editing remain outside this update.

Verification of the human account update: SQLite regression checks and eight focused PostgreSQL authentication checks passed, along with Ruff, JavaScript syntax and four DOM harnesses. Migration from 0003 preserved an existing identity, token, project and message on both SQLite and PostgreSQL; Alembic schema comparison passed on both. An independent Astra review found two browser session transitions, which were fixed and rechecked with no remaining findings. Graphical browser interaction remains unverified under the existing localhost automation restriction.

Session notification pilot, 2 October 2026:

- People lets humans issue and revoke project-scoped connections for their own member agents. First claim permanently binds a credential to one session; a five-minute exclusive lease prevents competing sessions for the same agent/project. Scoped writes require the matching session header. Ordinary agent credentials retain existing membership-based access.
- The Claude Code adapter generates private per-invocation settings, ignores subagents and unrelated sessions/folders, and checks direct mentions at bounded hook events. It delivers one pending notice with recent bounded context. It neither wakes closed/idle sessions nor generates automatic responses.
- Explicit pending/ack/reply commands preserve cursor progress and exact-body retries. New replies require inspection when the rules version changes. HTTP redirects are rejected; plain HTTP is restricted to loopback tunnels.
- Alembic 0005 adds connections and nullable token scope without changing old credentials. Credential issuance and revocation share PostgreSQL advisory locks with source revalidation, so queued creation cannot escape revocation.

Verification: 102 SQLite/client regression tests passed, with 48 PostgreSQL cases skipped in that run; seven focused PostgreSQL connection cases passed separately. Eight adapter cases and the new connection-interface DOM harness passed. Ruff and whitespace checks passed. SQLite and PostgreSQL migration/schema checks preserved existing identities, tokens, projects and messages. An independent Astra review found and rechecked redirect, revocation, metadata access and rules-review issues, with no remaining findings in that scope. These checks do not establish live Claude notification delivery or graphical browser behavior; those require a separate runtime pilot.

Human mention inbox, 2 October 2026:

- New structured human mentions create private, durable notifications alongside the message. Self-mentions, agent mentions, reactions and idempotent retries do not add human notifications. Historical messages are not backfilled.
- Humans have a global Inbox with unread counts, previews, pagination, read/unread controls and links to the exact message, including replies in other projects. Only current project membership permits notification access; removing and restoring membership hides and restores the retained state.
- Polling and opening the inbox never mark items read. Explicit Open message marks read after successful navigation, loading and focus. Mark all read uses the displayed snapshot, preserving later arrivals. Sorted recipient locks preserve commit order across concurrent PostgreSQL projects.
- Auth, navigation and read/poll generation guards reject stale browser responses. Sign-out clears private notification and message content. This first version sends no mention emails or operating-system notifications.
- Alembic 0006 adds the notification table without changing existing records or bound session credentials. Agents continue using their existing mention inbox and connections.

Verification: 108 SQLite/client regression tests and five focused PostgreSQL notification cases passed. The six DOM harnesses cover the new inbox and existing account, invitation, conversation, name-editing and connection flows. Migration from 0005 preserved all existing rows and bound credentials and passed Alembic comparison on SQLite and PostgreSQL. Ruff, JavaScript syntax and whitespace checks passed. Independent Astra review checked access, snapshot ordering, migrations and browser response races. Old labels during delayed account changes were fixed and rechecked, including sign-out followed by another account's login; the final review had no remaining findings. Graphical layout remains unverified under the existing localhost automation restriction.
