---
name: status
description: Show whether this conversation is connected to Research Workspace, tunnel health and pending mentions.
disable-model-invocation: true
allowed-tools: Bash
argument-hint: "[--check]"
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" status $ARGUMENTS` and summarise the JSON in one or two lines. `--check` also contacts the server.

Shell-quote every argument you pass (paths with spaces, `;`, `$` must not be interpreted). Do not add flags the user did not ask for or choose; never add `--ssh-option` on your own.
