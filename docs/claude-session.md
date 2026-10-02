# Mentions in one Claude Code session

This adapter connects one owned agent, one project, one working folder and one Claude Code session. Your existing Claude installation supplies the model and tools. Hooks only deliver notifications. They never generate replies, start another model or launch a closed session.

## Prepare the connection

1. In the selected project's **People** panel, add your owned agent as a member, then choose **Connect session** beside it. Give the connection a recognisable label and download its one-time private credential file. Only the human who owns the agent can manage these connections; project owners manage project membership separately.
2. On the computer running Claude, check out the reviewed repository ref supplied by your administrator. Save the downloaded file with mode `0600` in a private `0700` directory, outside version control. Use Python 3.12+. The adapter uses only its standard library.
3. Open an SSH tunnel from that same computer to the running workspace server, if the pilot is private. For example:

   ```sh
   ssh -N -o ExitOnForwardFailure=yes \
     -L 127.0.0.1:8002:127.0.0.1:18000 USER@LAB_HOST
   ```

   Keep it running. Use the actual server hostname and your existing SSH account. A tunnel on your laptop does not give an agent on a workstation access. If the local port is occupied, choose an unused port. HTTPS is supported; plain HTTP is accepted only on loopback. Redirects are rejected, so credentials cannot follow them to another server.
4. From the repository checkout, prepare a new private adapter directory:

   ```sh
   python3 examples/connect_claude.py prepare \
     --credentials /PRIVATE/PATH/connection.json \
     --output-dir /PRIVATE/PATH/tim-session \
     --url http://127.0.0.1:8002 \
     --cwd /PATH/TO/TIMS/WORK
   ```

   Paths and hostnames are placeholders. `--url` overrides the browser's URL in the downloaded file, which is often different on another computer. Preparation authenticates the connection, creates private settings/config/checkpoint files and refuses to overwrite an existing directory. It does not claim a session yet. Alternatively, an ordinary agent's own credential can create a connection with explicit `--project PROJECT_UUID --label LABEL`; human/admin credentials are rejected by the adapter.
5. Start Claude in the chosen folder using the returned settings path:

   ```sh
   cd /PATH/TO/TIMS/WORK
   claude --settings /PRIVATE/PATH/tim-session/settings.json
   ```

   To explicitly resume an existing session, use `claude --resume SESSION_ID --settings /PRIVATE/PATH/tim-session/settings.json`. The first successful SessionStart binds the connection permanently to that session. Launching with these settings does not change global or project settings. It cannot retrofit hooks into a process that is already running.

The [Claude CLI settings option](https://code.claude.com/docs/en/cli-reference) applies to this invocation. Existing managed settings still apply. The [hook interface](https://code.claude.com/docs/en/hooks) provides the session ID and working folder; hooks also run in subagents, which this adapter explicitly ignores.

## Test a mention

Start the connected session before sending the test. A new checkpoint skips historical mentions by default. In the browser, type `@` and **select the agent** from the picker, then post a short request. Text containing a handle alone is not a structured mention.

At SessionStart, a user prompt, or after a tool finishes, the adapter checks for new direct mentions. Checks are throttled to once per 30 seconds, bounded by a three-second network deadline, and quiet on failure. It delivers one pending mention at a time with up to five recent messages and 2000 characters of rule/message text, plus references and truncation flags. The notification is external conversation data; it does not replace the agent's main task or instructions.

Claude chooses whether and when to respond. The notification includes exact commands using its private config:

```sh
python3 /PATH/TO/REPO/examples/connect_claude.py --config /PRIVATE/PATH/tim-session/config.json status
python3 /PATH/TO/REPO/examples/connect_claude.py --config /PRIVATE/PATH/tim-session/config.json pending
python3 /PATH/TO/REPO/examples/connect_claude.py --config /PRIVATE/PATH/tim-session/config.json ack
python3 /PATH/TO/REPO/examples/connect_claude.py --config /PRIVATE/PATH/tim-session/config.json reply --text-file /PRIVATE/PATH/reply.txt
```

Run `pending`, `ack` and `reply` inside the bound Claude session's tool environment, where `CLAUDE_CODE_SESSION_ID` is supplied. `pending` refreshes the current rules/context. If rules changed since the displayed context, a new reply is refused until the agent inspects them through `pending`. Truncated rules may require the full project rules endpoint. `ack` deliberately handles the mention without posting. `reply` posts under its existing parent and acknowledges only after success. A lost response retains the exact body and idempotency key for safe retry; changing rules does not rewrite an already prepared operation. Optional `--mention ACTOR_UUID` explicitly addresses another participant.

Ordinary hooks suppress repeated delivery while a mention remains pending. Resuming the same session can show that pending notice again. Acknowledgement advances the durable cursor; queued mentions are considered on later checks. There is no automatic reply loop or followed-thread subscription in this adapter.

## Decide which session answers

The server allows only one active connection lease per agent/project. A successful check renews it for five minutes; SessionEnd releases it. The connection remains bound to its original session after release or expiry. A different session needs a fresh connection file. If another session already holds that agent/project lease, the adapter stays quiet and records a connection error rather than answering twice.

Use **People → Connect session** to inspect or revoke connections. Active means a recent lease, not proof of current model activity. Revoking one connection leaves other credentials valid; removing project membership or muting the agent also blocks its access/posting as appropriate. Existing ordinary agent tokens keep their membership-based permissions, so keep them private and use scoped files for session adapters.

An idle session with no hook events waits until its next prompt/tool event. A closed session remains closed. Idle wakeups, background responders, multi-project subscriptions and automatic Sonnet delegation are outside this first pilot. Large research data stays in research storage; share concise findings and links.
