---
name: connect
description: Connect THIS Claude conversation to Research Workspace mentions (guided: SSH tunnel, credential download, connect).
disable-model-invocation: true
allowed-tools: Bash
argument-hint: "[credential.json] [--ssh-user USER | --ssh ALIAS] [--no-tunnel --url URL] [--import CONFIG]"
---
Connect this conversation. The session id comes from Claude, never from the folder.

**If `$ARGUMENTS` contains a credential file path, go straight to step 4.**

Otherwise guide the user one step at a time, wait for them to confirm each step, and never skip ahead. First run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" discover` (lists downloaded connection files, never tokens, plus `saved_ssh` and `default_ssh_host`). If it lists a file the user clearly just downloaded, offer it and go to step 4.

1. **Ask for their SSH account** unless `saved_ssh` is set: their cluster username (or an alias from `~/.ssh/config`). Host is `default_ssh_host`.
2. **Tunnel for the browser.** The workspace web page is only reachable through SSH. Tell the user to run this in their own terminal and leave it open (host = `default_ssh_host`, USER = their account; it prints nothing while working):
   `ssh -N -L 127.0.0.1:8002:127.0.0.1:18000 USER@HOST`
   If the port is busy, use another local port and the same port in the browser address. Then they open `http://127.0.0.1:8002`, sign in as themselves, and stop this ssh with Ctrl+C when done with step 3.
3. **Download the credential.** In the browser: the project's **People** panel → add their own agent as a member if it is not there → **Connect session** beside it → label it → download the file. Ask for the path of the downloaded file.
4. **Connect.** Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ws.py" --session "${CLAUDE_SESSION_ID}" connect $ARGUMENTS` with the path and `--ssh-user USER` (or `--ssh ALIAS`) unless `saved_ssh` is set or the user wants `--no-tunnel --url URL`. Use only flags listed in `connect --help`.
5. Report only the JSON result (project, URL, tunnel port). Never print, echo or pass a token. If SSH needs a passphrase or an unknown host key, tell the user to run `ssh USER@HOST` themselves with the `!` prefix (accept the host key or finish the prompt), then retry step 4. Finish by suggesting `/workspace:status --check`.

Shell-quote every argument you pass (paths with spaces, `;`, `$` must not be interpreted). Do not add flags the user did not ask for or choose; never add `--ssh-option` on your own.
