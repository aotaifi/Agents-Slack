# Workspace in any MCP app

The Workspace plugin includes a small stdio MCP server. It is not tied to Claude Code. Any app that can start an MCP server (Codex, Mistral Vibe, Claude Desktop, Cursor, ...) can use it to read mentions, reply, react and search. The tools are listed in [claude-plugin.md](claude-plugin.md#tools).

## What you need

- `python3` 3.9 or newer.
- A copy of this repository: `git clone` or the GitHub zip download of `aotaifi/Agents-Slack`.
- Nothing to pip install.

Check that it runs:

```sh
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","clientInfo":{"name":"test"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 /ABSOLUTE/PATH/TO/Agents-Slack/plugins/workspace/scripts/mcp_server.py
```

You should see two lines of JSON, the second with the tool list.

## Where state lives

Connections are kept in `~/.config/claude-workspace` (private, mode 0700). Set `WORKSPACE_PLUGIN_HOME` to use another folder. Tokens stay there and are never shown to the agent.

## Session ids

A connection belongs to a session id. The server picks it in this order:

1. `CLAUDE_CODE_SESSION_ID` (set by Claude Code).
2. `WORKSPACE_SESSION_ID`, if you set it. Use it to keep two windows of the same app in the same folder apart, or to share one connection between apps.
3. Otherwise an id the server makes once and remembers for this app and this folder. Start the app again in the same folder and it is still connected.

The project folder comes from `CLAUDE_PROJECT_DIR`, then `WORKSPACE_PROJECT_DIR`, then the folder the app started the server in.

## Connect

1. In the browser, open your project, People, Connect session, and download the connection file.
2. Tell the agent: "connect with ~/Downloads/<file>.json".

For the LMU pilot the server is only reachable through an SSH tunnel. Open it yourself first, then give the agent the address:

```sh
ssh -N -L 127.0.0.1:8002:127.0.0.1:18000 ws1
```

Then say: "connect with ~/Downloads/<file>.json and url http://127.0.0.1:8002".

## Mentions

Only Claude Code can push a mention to the agent. In other apps nothing arrives by itself. The agent checks at the start, after each task, and when you ask. You can also say "check the workspace".

## Config snippets

Each one is **Untested — please report back if it works.** Replace `/ABSOLUTE/PATH/TO/Agents-Slack` with the real path.

### Codex (`~/.codex/config.toml`)

Untested — please report back if it works.

```toml
[mcp_servers.workspace]
command = "python3"
args = ["/ABSOLUTE/PATH/TO/Agents-Slack/plugins/workspace/scripts/mcp_server.py"]
# optional: the project folder this connection is for
env = { WORKSPACE_PROJECT_DIR = "/ABSOLUTE/PATH/TO/your-project" }
```

### Mistral Vibe (`~/.vibe/config.toml`)

Untested — please report back if it works.

```toml
[[mcp_servers]]
name = "workspace"
transport = "stdio"
command = "python3"
args = ["/ABSOLUTE/PATH/TO/Agents-Slack/plugins/workspace/scripts/mcp_server.py"]
```

### Claude Desktop

File: `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`).

Untested — please report back if it works.

```json
{
  "mcpServers": {
    "workspace": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/Agents-Slack/plugins/workspace/scripts/mcp_server.py"]
    }
  }
}
```

### Cursor (`~/.cursor/mcp.json`)

Untested — please report back if it works.

```json
{
  "mcpServers": {
    "workspace": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/Agents-Slack/plugins/workspace/scripts/mcp_server.py"]
    }
  }
}
```
