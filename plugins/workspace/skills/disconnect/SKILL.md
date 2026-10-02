---
name: disconnect
description: Disconnect this conversation from Research Workspace and stop only the tunnel this plugin created for it.
disable-model-invocation: true
allowed-tools: Bash
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" disconnect` and report the JSON. Unrelated SSH sessions and forwards are not touched; the server-side connection stays bound to this session and can be revoked in the browser.
