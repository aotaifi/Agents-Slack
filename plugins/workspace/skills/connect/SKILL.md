---
name: connect
description: Connect THIS Claude conversation to Research Workspace mentions (scoped credential, optional SSH tunnel).
disable-model-invocation: true
allowed-tools: Bash
argument-hint: "[credential.json] [--ssh ALIAS | --ssh-user USER] [--local-port N] [--import CONFIG]"
---
Connect this conversation. The session id comes from Claude, never from the folder.

1. If no credential path was given in `$ARGUMENTS`, run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" discover` (lists downloaded connection files, never tokens) and ask the user which one. The output also shows `saved_ssh`: if set, the tunnel reuses that SSH account automatically, so ask nothing more about SSH. If it is null, ask once for their cluster username (or an SSH alias from `~/.ssh/config`) and pass `--ssh-user USER` (or `--ssh ALIAS`); it is remembered for later connects. Ask for a workspace URL only if they do not want a tunnel (`--no-tunnel --url URL`).
2. Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" connect $ARGUMENTS` (add the user's choices as flags, using only flags listed in `connect --help`).
3. Report only the JSON result (project, URL, tunnel port). Never print, echo or pass a token. If SSH needs a passphrase or an unknown host key, tell the user to run `ssh TARGET` themselves with the `!` prefix (accept the host key or finish the prompt), then retry.

Shell-quote every argument you pass (paths with spaces, `;`, `$` must not be interpreted). Do not add flags the user did not ask for or choose; never add `--ssh-option` on your own.
