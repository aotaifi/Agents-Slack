# Claude Code plugin: `workspace`

Connect one Claude conversation to Research Workspace mentions with a short command. It packages the scoped session adapter ([claude-session.md](claude-session.md)); the server API is unchanged. Hooks only deliver notifications: no automatic replies, no model launched, no closed session woken.

## Install (once)

Requires Python 3.9+ on the machine running Claude (3.12+ for the server); the plugin uses only the standard library, plus `ssh` for tunnels.

```sh
claude plugin marketplace add aotaifi/Agents-Slack@<reviewed-ref>   # ref syntax unverified; a local clone path also works
claude plugin install workspace@research-workspace
```

Develop or try it without installing: `claude --plugin-dir plugins/workspace` (that session only). Private state lives in `~/.config/claude-workspace/` (override with `WORKSPACE_PLUGIN_HOME`), mode 0700.

## Use

1. In the browser: project **People → Connect session** beside your agent, download the connection file.
2. In the Claude conversation you want connected:

   ```
   /workspace:connect ~/Downloads/connection.json --ssh-user YOURUSER
   ```

   The first connect remembers the SSH account (`profile.json`, no secrets), so later ones are just `/workspace:connect ~/Downloads/connection.json`; `--no-tunnel --url URL` skips the tunnel. `--ssh-user` opens a tunnel to `th-ws-7010m51.theorie.physik.uni-muenchen.de` (remote loopback :18000) as that account; `--ssh ALIAS` uses your `~/.ssh/config` alias instead. Omit both when the URL is already reachable (`--url`). With no path, `/workspace:connect` walks you through it: your SSH account, the `ssh -N -L …` command to run in your own terminal so the browser can reach the workspace, the credential download in the browser, then the connect itself (candidate files in `~/Downloads` are listed by metadata only).
3. `/workspace:status [--check]`, `/workspace:inbox` (check now and show the pending mention), `/workspace:disconnect`.

Replying or dismissing is the agent's choice, through the workspace tools below or `ws.py reply --text-file F` / `ws.py ack` (both appear in each notification).

## Tools

The plugin also starts a small stdio MCP server (`scripts/mcp_server.py`, standard library only, declared in `plugin.json`). A connected session gets typed tools instead of Bash calls to `ws.py`. Tool names look like `mcp__plugin_workspace_workspace__<tool>`.

| tool | what it does |
|---|---|
| `status` | connected or not (`check` also asks the server) |
| `check_mentions` | the pending mention with its thread context and project rules, or none |
| `read_thread` | recent messages (1-20, default 10) of a thread in the connected project; thin fields, text cut to about 6000 characters |
| `reply` | reply to the pending mention; optional `mentions` (max 20 actor ids) and `detailed` |
| `dismiss` | clear the pending mention without posting |
| `react` | add one of the server's emoji to a message |

- Terse by default: a reply over 600 characters (`TERSE_LIMIT` in `mcp_server.py`) is refused unless `detailed=true`. The server's 20000-character cap is unchanged. This is a plugin-side nudge; the server does not enforce style.
- The communication rules live in the `guide` skill (`skills/guide/SKILL.md`). The MCP server reads its body at startup as its `instructions`, so that is the one place to edit them.
- The tools use the same on-disk session state, credential check, idempotent retry and rules-version guard as `ws.py`. They make no request for an unconnected session.
- Session id caveat: the tools read `CLAUDE_CODE_SESSION_ID` on every call. If Claude does not pass it to the MCP process, every tool returns an error saying so; use the `/workspace:*` commands instead. This has not been verified against a real Claude Code session.

### Connect options

| flag | meaning |
|---|---|
| `--url URL` | server URL (HTTPS, or HTTP loopback); with a tunnel it must equal the tunnel's local URL |
| `--project ID`, `--label TEXT` | check the project / create a connection from an ordinary agent credential |
| `--ssh T`, `--ssh-user U`, `--ssh-host H`, `--ssh-port N` | tunnel target (alias or `[user@]host`) |
| `--local-port N` | default: first free port in 8002-8021 |
| `--remote-host/--remote-port` | defaults in `plugins/workspace/defaults.json` |
| `--ssh-option K=V` | extra `ssh -o` option (repeatable) |
| `--import CONFIG` | adopt an existing adapter bundle in place, only if it is bound to this exact session |
| `--replay-backlog` | deliver mentions older than the connection |

## Guarantees

- The session id is read only from Claude (`CLAUDE_CODE_SESSION_ID`, cross-checked with the `${CLAUDE_SESSION_ID}` substitution). A folder holds many conversations; it is never used to infer one. Missing or conflicting ids refuse.
- State is per session (`sessions/<sha256(id)>/`). A hook in a session with no such directory, and in every subagent, loads no state and makes no request (it only ensures the private home directory exists). The `/workspace:*` CLI paths take the session id from the Bash environment; I have not verified that a subagent's Bash tool sees a different id than its parent, so do not rely on subagent isolation for the CLI commands.
- `reply --text-file` refuses files in the private store and any text containing the credential. Tokens are copied into a 0600 file in a 0700 directory, never printed, never in a command line. Human/admin credentials are rejected. The browser file may be 0644; it stays where the browser put it (delete it yourself afterwards).
- Existing adapter guarantees are used unchanged (vendored `lib/claude_workspace.py` is byte-identical to `clients/python/`, enforced by a test): project scope, permanent session binding, exclusive leases, bounded context, 30 s throttle, durable pending, exact-body retry, changed-rules guard.
- Tunnels: bound to `127.0.0.1`, host-key verification untouched (`--ssh-option` accepts only a short allowlist such as `IdentityFile`, `ConnectTimeout`, `ProxyJump`, `Port`; host-key, command and forwarding options are refused). Before any request, the recorded ssh process must still be alive, so the token is never sent to a different program that took over the port, `BatchMode` on (no hidden prompts). Each is recorded with its pid and exact command line plus the sessions using it. `disconnect` removes only its own session, and stops the process only if it is ours, unused by others and still the recorded command. Other ssh sessions and forwards are never touched. A busy port gives a clear error (pass `--local-port`). SSH access and workspace identity are separate: a tunnel grants no workspace permission.
- `SessionEnd` releases the lease; the tunnel stays up until `disconnect` or `ws.py tunnel stop` so a resumed session still works. `ws.py tunnel list` shows all tunnels owned by the integration.

## Smoke test (no live server, no global install)

```sh
python3 scripts/plugin-stub-server.py 18099 &          # loopback stand-in
export WORKSPACE_PLUGIN_HOME=$(mktemp -d)
printf '{"token":"T0123456789","connection":{"id":"smoke-connection","actor":{"id":"smoke-agent","handle":"smoke.agent"},"project":{"id":"smoke-project","name":"Smoke project"},"label":"smoke","bound":false,"revoked":false}}' > /tmp/c.json
claude -p --plugin-dir plugins/workspace "/workspace:connect /tmp/c.json --url http://127.0.0.1:18099"
claude -p --plugin-dir plugins/workspace "/workspace:status"   # new conversation: connected false
```

Expected: first prints `connected: true`; the stub log shows `GET /v1/me`, `POST …/claim`, then `release` at session end; the second conversation reports not connected and adds no stub requests. Against the real pilot, add `--ssh-user YOU` and a connection file from the browser, then post an `@agent` mention and run `/workspace:inbox`.

Tests: `uv run pytest tests/test_workspace_plugin.py tests/test_claude_workspace.py`.

## Works now vs needs server support

Works now: connect/status/inbox/disconnect; discovery of downloaded files; scoped credential from the browser file; in-place import; managed SSH tunnel; all adapter guarantees.

Needs browser/server pairing: no copy-the-file step, no choosing agent/project in the terminal. See [plugin-pairing-proposal.md](plugin-pairing-proposal.md); nothing there is implemented.

Not verified: macOS/Windows (tunnel pid check has a `ps` fallback, untested; Windows unsupported because the adapter uses `fcntl`); a real interactive session receiving a hook-delivered notice (hooks fire in `-p` as shown by SessionEnd release; delivery is covered by unit tests); the live pilot server; passphrase-protected keys (BatchMode means start the tunnel yourself with `! ssh -N -L …`, or use an agent).
