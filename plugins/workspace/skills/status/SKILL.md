---
name: status
description: Show whether this conversation is connected to Research Workspace, tunnel health and pending mentions.
disable-model-invocation: true
allowed-tools: Bash
argument-hint: "[--check]"
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" status $ARGUMENTS` and summarise the JSON in one or two lines. `--check` also contacts the server.
