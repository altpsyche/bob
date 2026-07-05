# Agent HTTP server

`bob agent serve` runs the agent tool-loop as a small HTTP service (FastAPI + uvicorn), so
WebUIs, n8n, or other clients can drive Bob over REST or SSE.

```powershell
bob agent serve            # binds agent.serveHost:agent.agentPort (default 127.0.0.1:8084)
```

The registry and session store are built once at startup and shared across requests.

The server is pure Python (FastAPI + uvicorn), so it runs unchanged on Windows and Linux; `bob agent serve` resolves to `python -m bob agent serve` on any OS (contract C1). On either OS, `scripts/smoke.ps1` exercises `/health` + an owner-scoped session turn + an SSE stream as the end-to-end gate (the shared cross-OS smoke the ND2 CI matrix runs).

## Security

- **Bind:** loopback (`127.0.0.1`) by default. Set `agent.serveHost = '0.0.0.0'` in `config/bob.psd1`
  to expose on the LAN, and **only** with `agent.allowPrivateFetch = $false` (the default), so a
  remote caller can't use `web_fetch` for SSRF against your private network.
- **Auth:** every endpoint except `/health` requires `Authorization: Bearer <token>`, where `<token>`
  is the litellm key (`litellmKey`, default `sk-local`) or an `agent.apiTokens` entry.
- **Identity + ownership (N1):** each token maps to an owner id: `agent.apiTokens` entries are
  `@{ token = 'sk-alice-…'; owner = 'alice' }` records (bare strings still work, mapping the token
  to itself), and the litellm key maps to `agent.defaultOwner` (default `local`). Sessions are
  owner-scoped: a token can only read/delete/continue sessions its owner created; any other
  `session_id` returns **404**, indistinguishable from an unknown id. Revoke a token by removing it
  from config and restarting `bob agent serve`. See [SECURITY.md](SECURITY.md).

## Config

All under the `agent` block of `config/bob.psd1` (see [TUNING.md](TUNING.md#agent-behavior-configbobpsd1)):
`serveHost`, `agentPort`, `apiTokens`, `defaultOwner`, `sessionDbPath`, `maxSessionTokens`,
`gitAllowedRoots`, `logMaxBytes`/`logBackupCount`, `mcpEnabled`.

## Endpoints

### `GET /health` (no auth)
```json
{ "status": "ok", "tools_loaded": 10, "tools_failed": 0 }
```

### `POST /v1/agent/completions`
Run the agent to a final answer (blocking).

Request:
```json
{ "goal": "summarise README.md", "agency": "silent", "role": null, "session_id": null }
```
- `agency`: `silent` | `show` | `confirm` (default `silent`; `confirm` is unusable server-side, no stdin).
- `role`: model role override (default `routing.agentRole`).
- `session_id`: optional; continue a session created via `POST /v1/sessions`. Prior turns are seeded
  into the loop; the new turn (goal + result) is appended and its token estimate charged to the budget.

Response `200`:
```json
{ "result": "…", "session_id": "…", "error": null }
```
Errors: `401` bad/missing bearer; `402` session over budget; `404` unknown `session_id`;
`422` agent hit `maxSteps` with no final answer; `503` not initialized / config missing; `500` other.

### `POST /v1/agent/completions/stream`: Server-Sent Events (M15)
Same request body. Response is `text/event-stream`; each line is `data: {json}`. Event types:

| type | fields | when |
|------|--------|------|
| `token` | `text` | final-answer deltas as they generate (tool-call markup is suppressed) |
| `tool_call` | `name`, `arguments` | the model requested a tool |
| `tool_result` | `name`, `result` | a tool returned |
| `final` | `result`, `exit_requested`, `reason`, `session_id?` | terminal: `reason` ∈ `answer`/`max_steps`/`interrupted`/`aborted` |
| `error` | `message` | terminal: pre-flight or LLM failure |

A `final` or `error` is always the last event. The session turn is recorded when the stream ends.

## Examples

```bash
# one-shot
curl -s http://127.0.0.1:8084/v1/agent/completions \
  -H "Authorization: Bearer sk-local" -H "Content-Type: application/json" \
  -d '{"goal":"what is 2+2","agency":"silent"}'

# multi-turn
SID=$(curl -s http://127.0.0.1:8084/v1/sessions -H "Authorization: Bearer sk-local" \
      -H "Content-Type: application/json" -d '{}' | jq -r .session_id)
curl -s http://127.0.0.1:8084/v1/agent/completions \
  -H "Authorization: Bearer sk-local" -H "Content-Type: application/json" \
  -d "{\"goal\":\"remember my name is Siva\",\"session_id\":\"$SID\"}"

# streaming
curl -N http://127.0.0.1:8084/v1/agent/completions/stream \
  -H "Authorization: Bearer sk-local" -H "Content-Type: application/json" \
  -d '{"goal":"say hi in 3 words"}'
```

### Sessions

| Method | Path | Body / result |
|--------|------|---------------|
| `POST` | `/v1/sessions` | `{ "token_budget": 0 }` → `{ "session_id", "token_budget" }` |
| `GET` | `/v1/sessions/{id}` | full session (history, `token_budget`, `tokens_spent`) |
| `DELETE` | `/v1/sessions/{id}` | `{ "deleted": true }` |

## n8n

```
URL:    http://host.docker.internal:8084/v1/agent/completions
Header: Authorization: Bearer <litellm key or an agent.apiTokens entry>
Body:   { "goal": "{{ $json.goal }}" }
```
