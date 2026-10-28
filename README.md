# CIRL Gateway (mock)

An MCP server (JSON-RPC 2.0 over HTTP) that a Perfox AI agent calls during a WhatsApp eIA onboarding demo.
All CIRL behaviour is **mocked**. Nothing here calls real CIRL, WhatsApp or Perfox services.

## Run locally

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then set GATEWAY_API_KEY
uvicorn app.main:app --env-file .env --port 8000
```

Check it:

```powershell
Invoke-RestMethod http://localhost:8000/health
.\scripts\test_mcp.ps1 -BaseUrl http://localhost:8000 -ApiKey <your key>
```

Run the tests:

```powershell
python -m pytest
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_API_KEY` | *(none)* | Bearer token required on `/mcp` and `/admin/audit`. If unset, every request is rejected. |
| `SESSION_TTL_MINUTES` | `60` | Idle time before a conversation's session is dropped. |
| `CIRL_MODE_CHECK_EXISTING_EIA` | `mock` | `mock` or `real` for CIRL verifyeIAAccount. |
| `CIRL_MODE_SEND_OTP` | `mock` | `mock` or `real` for CIRL sendOtp. |
| `CIRL_MODE_VERIFY_OTP` | `mock` | `mock` or `real` for CIRL verifyOtp. |
| `CIRL_MODE_SEND_EMAIL_OTP` | `mock` | `mock` or `real` for the email OTP send and verify. |
| `MOCK_EMAIL_OTP` | `123456` | 6-digit code that the mock `verify_email_otp` accepts. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `PORT` | `8000` | Port to bind (Render sets this). |

`real` mode is not implemented yet. A tool in `real` mode returns `isError` with `GW-501` and makes no network call.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Health check |
| POST | `/mcp` | Bearer | MCP JSON-RPC: `initialize`, `ping`, `tools/list`, `tools/call` |
| GET | `/admin/audit` | Bearer | Latest 200 masked audit entries (demo only) |

### MCP contract

- The server accepts `Accept: application/json, text/event-stream` and always responds with `application/json`.
- `initialize` returns `protocolVersion: "2024-11-05"` and an `mcp-session-id` response header.
- Notifications (requests without an `id`, e.g. `notifications/initialized`) get HTTP 202 with no body.
- `tools/list` returns each tool's schema as `input_schema`, and also as `inputSchema` (the MCP-standard spelling).
- `tools/call` returns `{content: [{type: "text", text: "<JSON>"}], isError}`.
  - Success: `isError: false`, and the JSON has `ok: true` and a neutral `message_for_agent` (e.g. "Consent recorded.").
  - Business failure: `isError: true`, and the JSON has `ok: false`, `error_code`, `error_msg` and `message_for_agent` (what the customer must fix for this tool).
  - `message_for_agent` never names another tool or the next step, because the journey order is decided by Perfox Steps.
- JSON-RPC errors are used only for protocol problems: parse error (-32700), invalid request or batch (-32600), unknown method (-32601), unknown tool, bad params or a missing `X-Sa-Conversation-Id` header (-32602).

Headers read on every `tools/call`: `X-Sa-Conversation-Id` (required, the session key), `X-Sa-End-User-Phone`,
`X-Sa-End-User-Email`, `X-Sa-End-User-Name`, `X-Sa-Channel`. Only the phone (normalised to `+91XXXXXXXXXX`)
and the channel are stored.

## Tools and mock behaviour

Typical order (decided by Perfox Steps, not the gateway): `start_journey` → `record_consent` → `check_existing_eia` → `send_otp` → `verify_otp`. Each tool still enforces its own preconditions, such as consent.

| Tool | Arguments | Requires | Mock behaviour | Failures (`isError`) |
|---|---|---|---|---|
| `start_journey` | – | – | Creates a session for the conversation and saves the phone and channel (audit event `journey_started`). Idempotent: if a session already exists for this conversation id, it is returned unchanged (audit event `journey_already_started`). A new chat has a new conversation id, so it starts fresh. | – |
| `record_consent` | `consent: bool`, `notice_version: string` | session (created if missing) | Stores the consent time and notice version. Returns a `consent_id` (`CNS-…`). Idempotent: if consent is already recorded, the existing `consent_id`, timestamp and notice version are returned. | `GW-003` consent false (clears any earlier consent), `GW-004` invalid input |
| `check_existing_eia` | `pan` **or** `uid`, `email_id`, `mobile_no?` | consent | PAN `ABCPE1234E` → `status "true"`, `eia_no "2345678904567"`. Anything else → `status "false"`, `E-102 "eIA Account does not exist."`. Both are successful calls (`eia_exists` true/false). Generates `trxn_no` `C1-` + 17 digits and saves it as `client_trxn_id`. `mobile_no` defaults to the session phone. | `GW-004` bad PAN/UID/email/mobile, or both/neither of PAN and UID. `GW-002` no consent |
| `send_otp` | – | consent, `client_trxn_id` | Returns a masked email (`ra*****@gmail.com`), masked mobile (`98XXXXXX20`), `otp_start_date`, `otp_end_date`, `otp_expire_time: 300` and `trxn_no`. The OTP is always `123456`. A new OTP resets the wrong-attempt count. | `E-203` more than 5 sends, `GW-005` no eIA check yet |
| `verify_otp` | `otp_no: string` (6 digits) | `send_otp` | `123456` → `otp_verified: true`. | `E-302` wrong OTP, `E-303` more than 300 s after send, `E-304` locked after 3 wrong attempts, `GW-006` no OTP sent, `GW-004` not 6 digits |
| `send_email_otp` | `email_id: string` | consent | "Emails" the code `MOCK_EMAIL_OTP` (default `123456`), valid 120 s. Stores the email and the expiry time. Returns `{ok, email_id, expires_in_seconds: 120}`. | `GW-002` no consent, `GW-004` invalid email, `GW-010` more than 5 sends, `GW-013` locked |
| `verify_email_otp` | `otp_no: string` (6 digits) | `send_email_otp` | The correct code → `email_verified: true`. | `GW-011` wrong code, `GW-012` more than 120 s after send, `GW-013` locked after 3 wrong attempts in total (across resends, for the whole session), `GW-014` no email OTP sent, `GW-002` no consent, `GW-004` not 6 digits |

The email OTP tools are independent of the mobile OTP flow and only need consent.

Other gateway codes: `GW-001` no active journey (never started, or expired), `GW-501` CIRL `real` mode not implemented.

## Data minimisation and audit

- Full PAN, UID and OTP values (including the email OTP) are never stored. PAN/UID are used for the single CIRL request and then discarded. No passwords are handled.
- Every tool call writes one masked audit entry (`timestamp`, `conversation_id`, `tool`, `event`, `input`, `output`, `ok`, `error_code`). It goes to stdout as a JSON line and to an in-memory buffer read by `/admin/audit`.
- Masking: PAN `ABCXXXX34F`, UID `XXXXXXXX9012`, OTP `******`, phone `+91XXXXXX5820`, email `ra*****@gmail.com`, eIA number last 4 digits only.
  As a safety net, any PAN-shaped or 12-digit value in any other string field is also masked.
- Request bodies, headers and the bearer key are never logged.
- Sessions and audit entries are kept in memory only, so they are lost on restart. Sessions sit behind the `SessionStore` interface (`app/sessions.py`), so a database-backed store can replace the in-memory one later.

## Deploy to Render

1. Push this folder to a Git repo.
2. In Render, choose **New → Blueprint** and select the repo. `render.yaml` defines one web service with health check `/health`.
3. Fill in the env vars Render prompts for. `GATEWAY_API_KEY` is required. For the others, use the values from `.env.example`.
4. Python is pinned in `.python-version` (3.12). If your Render account needs a full version, add `PYTHON_VERSION` (e.g. `3.12.8`) as an env var.
5. After deploy, run: `.\scripts\test_mcp.ps1 -BaseUrl https://<service>.onrender.com -ApiKey <key>`.

The free plan sleeps when idle, and that clears all in-memory sessions and audit entries.

## Layout

```
app/
  main.py          FastAPI app: /health, /mcp, /admin/audit, bearer auth
  mcp.py           JSON-RPC 2.0 dispatch
  tools.py         7 tool schemas and handlers (CIRL access only via cirl_client)
  cirl_client.py   one function per CIRL API; mock or real (NotImplementedError)
  sessions.py      SessionStore interface + in-memory TTL store
  audit.py         masked audit log
  masking.py       PAN / UID / OTP / phone / email masking
  validation.py    input formats, phone normalisation
  config.py        env settings
  clock.py         time source (patched in tests)
tests/             pytest suite
scripts/test_mcp.ps1
```

## Not yet implemented

Real CIRL calls, reqToken logic, KYC, eIA creation, policy tools, databases and WhatsApp are out of scope.
`verify_otp` has a TODO for the Perfox auth-context callback (`X-Sa-Auth-Callback`, `X-Sa-Auth-Callback-Token`).
