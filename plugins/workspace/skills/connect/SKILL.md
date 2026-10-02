---
name: connect
description: Connect THIS Claude conversation to Research Workspace mentions (scoped credential, optional SSH tunnel).
disable-model-invocation: true
allowed-tools: Bash
argument-hint: "[credential.json] [--ssh ALIAS | --ssh-user USER] [--local-port N] [--import CONFIG]"
---
Connect this conversation. The session id comes from Claude, never from the folder.

1. If no credential path was given in `$ARGUMENTS`, run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" discover` (lists downloaded connection files, never tokens) and ask the user which one, whether to open an SSH tunnel (their SSH alias or account) and the workspace URL if no tunnel is used.
2. Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" connect $ARGUMENTS` (add the user's choices as flags; see `connect --help`).
3. Report only the JSON result (project, URL, tunnel port). Never print, echo or pass a token. If SSH needs a passphrase or an unknown host key, tell the user to run the printed ssh command themselves with the `!` prefix, then retry.
