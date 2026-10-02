# Proposal: short-lived browser pairing (not implemented)

Status: proposal only; no shared backend file is changed. Needs review by the mention-inbox/email developer before any server change.

Goal: replace "download a file, pass its path" with a code typed into `/workspace:connect`, while keeping the scoped-connection model (one agent, one project, one session, no human/admin credential).

## Flow

1. Browser, signed in as the agent's owning human: **People → Connect session → Pair**. Server creates a pairing for (agent, project, label) and shows a code like `K7QD-4M2X` (≥ 50 bits), valid 10 minutes, single use.
2. Terminal: `/workspace:connect --pair K7QD-4M2X --url …`. The plugin generates a random 256-bit `claim_secret` locally (same pattern as invitation acceptance) and redeems the code.
3. Server returns the existing scoped credential `{connection, token}` once. The plugin stores it exactly as today and claims the lease with the real session id.

## Endpoints

- `POST /v1/agent-connections/pairings` (human owner of the agent; body `{agent_id, project_id, label}`) → `{pairing_id, code, expires_at}`. Stores only a hash of the code. At most one open pairing per agent/project.
- `POST /v1/agent-connections/pairings/redeem` (no auth; body `{code, claim_secret}`) → 201 `{connection:CONNECTION, token}`. Rate limited per IP and per code before any hashing work; identical 404 for unknown, expired or used codes. A retry with the same `claim_secret` returns the same credential (as invitations do); a different secret fails.
- `DELETE /v1/agent-connections/pairings/{id}` withdraws an unused code.

## Security notes

Code alone cannot recover the token (needs `claim_secret`); the token is HMAC-keyed by the code over the secret and only hashes are stored. Redeeming creates the same connection record `POST /v1/agent-connections` does, so revocation, binding and leases are unchanged. The endpoint is reachable only through the same SSH tunnel as the API, so SSH access stays a separate gate. Audit event `agent_connection.paired`. Needs Alembic migration for a `connection_pairings` table; tests for expiry, single use, replay, rate limit and wrong secret.

Open question for the PI: should pairing also let the terminal pick the agent/project from a list? That needs a human credential in the terminal, which this design deliberately avoids; the owner picks them in the browser.
