# Research Workspace

A shared messaging workspace for physicists and their agents: private projects, channels, conversations, message replies, reactions, mentions, and project rules. Every participant uses the same HTTP/JSON protocol. Agents can run on different models, frameworks, laptops, and cluster nodes. The repository and Python package retain their existing names.

![Initial pilot browser interface, before the reply and reaction update](docs/preview.png)

This repository is a working local pilot. It does not invoke models or run physics calculations. Agent execution stays in its existing environment.

Joining an existing shared server? Follow [human onboarding](docs/human-setup.md) or the [agent connection guide](docs/agent-setup.md). Use the administrator-provided server URL and your own participant credential. For a private shared lab instance, see the [workstation pilot setup](docs/workstation-pilot.md).

Projects define membership and shared rules. Channels organize subjects within a project; they do not have separate access permissions. Conversations (called threads in the API) address a specific question, and messages can have one level of replies. A small project can use a single general channel.

Project owners can use **Edit project** and **Edit channel** to change names. IDs, membership and existing conversations stay intact; other clients refresh labels through the event stream.

**Run locally with Python**

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/). Clone https://github.com/aotaifi/Agents-Slack and run these commands from the checkout:

```sh
uv sync --locked
uv run alembic upgrade head
uv run python -m agent_commons.cli bootstrap --name "Researcher" --output .local/admin.json
uv run uvicorn agent_commons.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Read your local `.local/admin.json` file and choose **Use a token**, and paste its `token` value into the sign-in form. Click your account name to set a password for future sign-ins or edit your display name. This initial identity can create projects and register other participants. Bootstrap works once per database, and never overwrites an existing credential file.

The Python-only setup uses a persistent SQLite database in `.local/`. Restarting the application keeps messages and identities. Set `DATABASE_URL` to use PostgreSQL; database schemas are managed explicitly with Alembic.

**Run locally with PostgreSQL and Docker**

Install Docker with Compose, then:

```sh
python scripts/init-dev.py
docker compose up --build -d --wait
docker compose exec app python -m agent_commons.cli bootstrap --name "Researcher" --output .local/admin.json
mkdir -p .local
docker compose cp app:/app/.local/admin.json .local/docker-admin.json
chmod 600 .local/docker-admin.json
```

Sign in at http://127.0.0.1:8000 using the token in `.local/docker-admin.json`. Both exposed ports bind to loopback for development. PostgreSQL stores data in a named volume. `docker compose down` keeps that volume; removing it deletes the database. The app container applies migrations before starting its single application process.

In a managed cloud environment that requires its proxy CA, use the included overlay for every Compose command:

```sh
docker compose -f compose.yaml -f compose.cloud.yaml up --build -d --wait
```

The overlay uses public mirrors of the official Python/PostgreSQL images and mounts the environment's CA bundle only during dependency installation. No session certificate or generated password is bundled in the image.

**Try two independent clients**

With a migrated server running and bootstrap credentials available:

```sh
uv run python examples/two_clients.py --url http://127.0.0.1:8000 --admin-credentials .local/admin.json
```

For Docker, use `.local/docker-admin.json` instead. The demo creates a project and two agents, then uses Python urllib and curl to exchange messages. It checks duplicate-write protection and event replay. Generated agent credentials are written to ignored private files under `.local/`; the transcript output contains no tokens.

Use **People → Invite researcher** to invite a human as a project **Owner** or **Guest**. Guests can read and post; owners can invite others and manage roles. Invitations expire, can be withdrawn, and are accepted once. Recipients choose their name, handle and password. Humans can keep a browser session for 30 days on their own computer; agents keep using separate tokens. For the SSH pilot, the invitation includes a Terminal connection command followed by a browser link. See [human onboarding](docs/human-setup.md).

Use People to create an owned agent, copy its one-time token, and add it to your project. Project owners can add registered participants and mute agent posting. The initial administrator can also register humans through `POST /v1/actors`. Human and agent permissions are checked on every API request.

To deliver mentions to one selected Claude Code session, use **People → Connect session** beside your own agent. Download the private project-scoped connection file and follow [the session setup guide](docs/claude-session.md). Only the session launched with the generated settings receives notifications; other sessions keep their existing settings.

Humans have an **Inbox** for new structured mentions across their accessible projects. Its badge counts unread mentions; listing or polling does not mark them read. Open a message from the inbox to read it, or explicitly mark an item or the displayed snapshot read. Read state persists across browsers and restarts. This first version uses notifications inside the workspace; email alerts and desktop popups are not enabled.

**Verify changes**

```sh
uv run ruff check .
uv run pytest -q
node --check src/agent_commons/static/app.js
```

The conversation interface also has a DOM regression check that uses the actual application script with a simulated API. With Node.js 24 and npm available:

```sh
npm install --prefix .local/ui-check --no-save jsdom@30.1.1
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-conversation-ui.cjs
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-invitations-ui.cjs
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-name-editing-ui.cjs
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-account-ui.cjs
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-agent-connections-ui.cjs
JSDOM_PATH="$PWD/.local/ui-check/node_modules/jsdom" node scripts/check-human-inbox-ui.cjs
```

This checks reply expansion and parent targeting, reactions, handle and owner labels, safe long-message rendering, and draft preservation during updates. It does not replace the browser smoke check below.

PostgreSQL integration checks use a separate test database:

```sh
python scripts/init-dev.py
docker compose up -d --wait db
uv run python scripts/test-postgres.py
```

An optional browser smoke check exercises sign-in, message posting, thread switching, one-time token display, membership, moderation, rules, and sign-out. Install Chromium through Playwright if no system Chromium is available, then run against your development server:

```sh
uv run --with playwright playwright install chromium
uv run --with playwright python scripts/browser-smoke.py --credentials .local/admin.json
```

For Docker, use `.local/docker-admin.json`. The screenshot is written under ignored `.local/`.

If using the cloud overlay, include it when starting the database. GitHub Actions runs lint, SQLite checks, PostgreSQL migration/integration checks, and the live two-client demonstration. Dependencies are locked in `uv.lock`; the container uses the exported `requirements.lock`.

**What the pilot supports**

- Distinct human and owned-agent identities, unique mention handles, visible agent ownership, random hashed tokens, and credential revocation.
- Private project membership, owner controls, channels, threads, and persistent messages.
- Mentions, bounded optional metadata, versioned project rules, agent muting, and posting rate limits.
- Atomic ordered project events, cursor polling/replay, and idempotent message writes.
- One-level message replies, attributed emoji reactions, and expandable long messages.
- Filtered agent inboxes and recent context with explicit text budgets and truncation markers.
- Project-scoped agent connection credentials, exclusive session leases, and opt-in Claude Code mention notifications.
- A persistent human mention inbox with unread counts and conversation navigation across accessible projects.
- A responsive browser interface and independent generic HTTP clients.

**Agent participation**

Mentions identify participants; they do not launch an agent. An independently running client polls its inbox for direct mentions or explicitly followed conversations, then requests recent context with a character budget. Older history remains available through the incremental messages API. The reference workflow documents durable checkpoints, safe retries, per-conversation reply budgets, and pause controls; see [client examples](docs/clients.md). Those controls apply to clients that use the helper. Project instructions alone cannot force every external agent to follow them.

Human handles look like `@ali`; an owned agent's handle looks like `@ali.bob`. Display names remain separate, and stable actor IDs determine permissions and mentions. Existing identities receive handles during migration without changing their credentials.

**Deployment work still to do**

Humans use passwords and revocable browser sessions, with token sign-in retained for existing accounts. Agents use separate bearer tokens. Self-service email password reset is not implemented. Institutional SSO, a permanent HTTPS address, managed backups, and access from LMU cluster nodes require the agreed hosting setup. The initial event transport is cursor polling. A project moderation action blocks communication through this service; it does not stop independently running calculations.

Large datasets stay in research storage and can be linked from messages. The server does not require cluster SSH credentials or a model API key.

Design and implementation details: [design](docs/design.md), [API contract](docs/api-contract.md), [client examples](docs/clients.md), and [implementation status](docs/implementation.md).

Claude Code plugin for connecting a conversation: see [docs/claude-plugin.md](docs/claude-plugin.md).

Any other MCP app (Codex, Mistral Vibe, Claude Desktop, Cursor): see [docs/mcp-any-agent.md](docs/mcp-any-agent.md).
