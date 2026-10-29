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
| `ENABLED_TOOLS` | `start_journey,record_consent,send_email_otp,verify_email_otp,create_password_link,check_password_status` | Comma-separated tools that `tools/list` shows and `tools/call` accepts. Empty means the default. A tool that isn't listed is rejected exactly like an unknown tool (-32602). An unknown name stops the server at startup. |
| `CIRL_MODE_CHECK_EXISTING_EIA` | `mock` | `mock` or `real` for CIRL verifyeIAAccount. |
| `CIRL_MODE_SEND_OTP` | `mock` | `mock` or `real` for CIRL sendOtp. |
| `CIRL_MODE_VERIFY_OTP` | `mock` | `mock` or `real` for CIRL verifyOtp. |
| `CIRL_MODE_SEND_EMAIL_OTP` | `mock` | `mock` or `real` for the email OTP send and verify. |
| `MOCK_EMAIL_OTP` | `123456` | 6-digit code that the mock `verify_email_otp` accepts. |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL`, else `http://localhost:8000` | Public address of this server, used to build the password link (`{PUBLIC_BASE_URL}/set-password?t=…`). On Render, `RENDER_EXTERNAL_URL` is set automatically. |
| `WHATSAPP_NUMBER` | *(none)* | WhatsApp business number, e.g. `+912269986221`. The password success page links to `https://wa.me/<digits>?text=DONE`. If unset, the page has no WhatsApp button. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `PORT` | `8000` | Port to bind (Render sets this). |

`real` mode is not implemented yet. A tool in `real` mode returns `isError` with `GW-501` and makes no network call.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Health check |
| POST | `/mcp` | Bearer | MCP JSON-RPC: `initialize`, `ping`, `tools/list`, `tools/call` |
| GET | `/admin/audit` | Bearer | Latest 200 masked audit entries (demo only) |
| GET | `/set-password?t=<token>` | link token | Password-setup page for the customer |
| POST | `/set-password` | link token (form field) | Sets the password |
| GET | `/set-password.js` | none | Live criteria checks and auto-redirect for the page |

### MCP contract

- The server accepts `Accept: application/json, text/event-stream` and always responds with `application/json`.
- `initialize` returns `protocolVersion: "2024-11-05"` and an `mcp-session-id` response header.
- Notifications (requests without an `id`, e.g. `notifications/initialized`) get HTTP 202 with no body.
- `tools/list` returns each tool's schema as `input_schema`, and also as `inputSchema` (the MCP-standard spelling).
- `tools/call` returns `{content: [{type: "text", text: "<JSON>"}], isError}`.
  - Success: `isError: false`, and the JSON has `ok: true` and a neutral `message_for_agent` (e.g. "Consent recorded.").
  - Business failure: `isError: true`, and the JSON has `ok: false`, `error_code`, `error_msg` and `message_for_agent` (what the customer must fix for this tool).
  - `message_for_agent` never names another tool or the next step, because the journey order is decided by Perfox Steps.
  - A tool called before its step is reached (`GW-002`, `GW-005`, `GW-006`, `GW-014`, `GW-020`, `GW-023`, below) returns `message_for_agent: "Not available yet. Continue with the current step."` and no `message_for_customer`, so the agent has nothing to relay to the customer.
- JSON-RPC errors are used only for protocol problems: parse error (-32700), invalid request or batch (-32600), unknown method (-32601), unknown tool, bad params or a missing `X-Sa-Conversation-Id` header (-32602).

Headers read on every `tools/call`: `X-Sa-Conversation-Id` (required, the session key), `X-Sa-End-User-Phone`,
`X-Sa-End-User-Email`, `X-Sa-End-User-Name`, `X-Sa-Channel`. Only the phone (normalised to `+91XXXXXXXXXX`)
and the channel are stored.

## Tools and mock behaviour

**For now the journey is registration only:** `start_journey` → `record_consent` → `send_email_otp` → `verify_email_otp` → `create_password_link` → `check_password_status` (consent, email, email OTP, verified, password). These six are the default `ENABLED_TOOLS`. The order is decided by Perfox Steps, not the gateway. Each tool still enforces its own preconditions, such as consent.

`check_existing_eia`, `send_otp` and `verify_otp` are **disabled by default**. Their code and tests are kept, and they can be switched back on by adding them to `ENABLED_TOOLS`. While disabled, they are not listed and `tools/call` answers `Unknown tool`.

| Tool | Arguments | Requires | Mock behaviour | Failures (`isError`) |
|---|---|---|---|---|
| `start_journey` | – | – | Creates a session for the conversation and saves the phone and channel (audit event `journey_started`). Idempotent: if a session already exists for this conversation id, it is returned unchanged (audit event `journey_already_started`). A new chat has a new conversation id, so it starts fresh. | – |
| `record_consent` | `consent: bool`, `notice_version: string` | session (created if missing) | Stores the consent time and notice version. Returns a `consent_id` (`CNS-…`). Idempotent: if consent is already recorded, the existing `consent_id`, timestamp and notice version are returned. | `GW-003` consent false (clears any earlier consent), `GW-004` invalid input |
| `check_existing_eia` *(disabled)* | `pan` **or** `uid`, `email_id`, `mobile_no?` | consent | PAN `ABCPE1234E` → `status "true"`, `eia_no "2345678904567"`. Anything else → `status "false"`, `E-102 "eIA Account does not exist."`. Both are successful calls (`eia_exists` true/false). Generates `trxn_no` `C1-` + 17 digits and saves it as `client_trxn_id`. `mobile_no` defaults to the session phone. | `GW-004` bad PAN/UID/email/mobile, or both/neither of PAN and UID. `GW-002` no consent |
| `send_otp` *(disabled)* | – | consent, `client_trxn_id` | Returns a masked email (`ra*****@gmail.com`), masked mobile (`98XXXXXX20`), `otp_start_date`, `otp_end_date`, `otp_expire_time: 300` and `trxn_no`. The OTP is always `123456`. A new OTP resets the wrong-attempt count. | `E-203` more than 5 sends, `GW-005` no eIA check yet |
| `verify_otp` *(disabled)* | `otp_no: string` (6 digits) | `send_otp` | `123456` → `otp_verified: true`. | `E-302` wrong OTP, `E-303` more than 300 s after send, `E-304` locked after 3 wrong attempts, `GW-006` no OTP sent, `GW-004` not 6 digits |
| `send_email_otp` | `email_id?: string` | consent | "Emails" the code `MOCK_EMAIL_OTP` (default `123456`), valid 120 s. Stores the email and the expiry time. Returns `{ok, email_id, expires_in_seconds: 120, message_for_customer}`. On a resend, `email_id` can be omitted and the stored email is reused. A repeat call for the same email within 30 s of a send, while that code is still valid, sends nothing and returns the same success result; it doesn't count towards the 5-send limit and is audited as `email_otp_duplicate_ignored`. | `GW-002` no consent, `GW-004` invalid email (or omitted with none stored), `GW-010` more than 5 sends, `GW-013` locked |
| `verify_email_otp` | `otp_no: string` (6 digits) | `send_email_otp` | The correct code → `email_verified: true`. | `GW-011` wrong code, `GW-012` more than 120 s after send, `GW-013` locked after 3 wrong attempts in total (across resends, for the whole session), `GW-014` no email OTP sent, `GW-002` no consent, `GW-004` not 6 digits |
| `create_password_link` | – | `email_verified` | Creates a one-time link `{PUBLIC_BASE_URL}/set-password?t=<token>`, valid 10 minutes. Only the SHA-256 of the token is stored. A new link invalidates any older unused one. Returns `{ok, url, expires_in_seconds: 600, message_for_customer}`. | `GW-020` email not verified, `GW-002` no consent |
| `check_password_status` | – | session | Password set on the page → `{ok, password_set: true}`. | `GW-021` not set yet (link still valid), `GW-022` link expired or used up, `GW-023` no link created yet |

The email OTP tools are independent of the mobile OTP flow and only need consent.

### `message_for_customer`

`send_email_otp`, `verify_email_otp`, `create_password_link` and `check_password_status` also return `message_for_customer`: the exact text the agent sends to the customer.
`isError`, `error_code` and `message_for_agent` are unchanged.

| Tool | Case | `message_for_customer` |
|---|---|---|
| `send_email_otp` | success | Great! We've sent an Email Verification Code to {email_id}. Please enter the 6-digit OTP below (Valid for 120 seconds): |
| `send_email_otp` | `GW-004` invalid or missing email | That doesn't look like a valid email address. Please enter your valid Email ID: |
| `send_email_otp` | `GW-010` send limit | You've reached the limit for verification codes. Please try again later. |
| `verify_email_otp` | success (also when already verified) | Email verified! |
| `verify_email_otp` | `GW-011` wrong code, `GW-004` not 6 digits | That code is incorrect. Please re-enter the 6-digit OTP: |
| `verify_email_otp` | `GW-012` expired | That code has expired. Reply RESEND and I'll send you a new one. |
| both email tools | `GW-013` locked | Too many incorrect attempts. Please start again later by sending Hi. |
| `create_password_link` | success | Let's set up your portal password. For your security, please set it on this secure page (link valid for 10 minutes):<br>{url}<br><br>\*Criteria:\*<br>- At least 8 characters<br>- Mixed case & 1 special character<br><br>Once done, reply DONE here. |
| `check_password_status` | success | Your password has been set successfully. |
| `check_password_status` | `GW-021` not set yet | We haven't received your password yet. Please set it using the secure link, then reply DONE. |
| `check_password_status` | `GW-022` link expired | Your secure link has expired. Reply NEW LINK and I'll send you a fresh one. |

`GW-002`, `GW-005`, `GW-006`, `GW-014`, `GW-020`, `GW-023` (called too early) and `GW-501` have no customer text, so the field is left out. The agent should rely on `message_for_agent` for those.

Other gateway codes: `GW-001` no active journey (never started, or expired), `GW-501` CIRL `real` mode not implemented.

## Password setup page

The password never passes through the chat, Perfox or any log. The customer types it only on `/set-password`.

- **Link:** `create_password_link` makes a token with `secrets.token_urlsafe(32)` and stores only its SHA-256 hash on the session. The link is valid for 10 minutes (still valid at 9:59, expired at 10:00) and allows 5 POST attempts. It stops working once the password is set or a newer link is created.
- **Page (`GET /set-password?t=…`):** mobile-friendly, with a CIRL heading, password and confirm fields (`type=password`, `autocomplete=new-password`), live criteria checks and a submit button. The token is posted back in a hidden form field, not in the URL. An invalid, used or expired token gets HTTP 410 and: *"This link has expired or was already used. Please go back to the chat and reply NEW LINK."*
- **Server-side rules (`POST /set-password`):** at least 8 characters (at most 128), an uppercase letter, a lowercase letter, a special character (anything that is not a letter, digit or space), and both fields match. The password is hashed with argon2id (`argon2-cffi`), and only the hash is stored on the session.
- **Success page:** *"Password set successfully."*, a **Return to WhatsApp** button linking to `https://wa.me/<WHATSAPP_NUMBER digits>?text=DONE`, and *"If you're on a computer, return to the chat and reply DONE."* If the session channel (`X-Sa-Channel`) is `whatsapp`, the page also redirects to that link after 2 seconds (meta refresh + script).
- **Headers on every page:** `Cache-Control: no-store`, `Referrer-Policy: no-referrer` (so the token is never sent to wa.me or anywhere else), `X-Robots-Tag: noindex`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and a Content-Security-Policy that allows only same-origin resources and scripts, plus inline styles.
- **Logging:** the uvicorn access log prints `/set-password` without its query string. The audit records `password_link_created`, `password_set` and `password_link_expired` with no values, and any `?t=<token>` in an audit entry becomes `?t=[REDACTED]`. Note that the token still reaches Perfox inside the link text the agent sends to the customer.

## Data minimisation and audit

- Full PAN, UID and OTP values (including the email OTP) are never stored. PAN/UID are used for the single CIRL request and then discarded.
- Passwords are stored only as an argon2id hash. Password-link tokens are stored only as a SHA-256 hash. Neither the password nor the token is logged or audited.
- Every tool call writes one masked audit entry (`timestamp`, `conversation_id`, `tool`, `event`, `input`, `output`, `ok`, `error_code`). It goes to stdout as a JSON line and to an in-memory buffer read by `/admin/audit`.
- Masking: PAN `ABCXXXX34F`, UID `XXXXXXXX9012`, OTP `******`, phone `+91XXXXXX5820`, email `ra*****@gmail.com`, eIA number last 4 digits only.
  As a safety net, any PAN-shaped value, 12-digit value, email address or `?t=<token>` in any other string field (e.g. `message_for_customer`) is also masked.
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
  main.py          FastAPI app: /health, /mcp, /admin/audit, bearer auth, access-log redaction
  password_page.py /set-password page: token handling, password rules, argon2, security headers
  mcp.py           JSON-RPC 2.0 dispatch
  tools.py         9 tool schemas and handlers (CIRL access only via cirl_client); ENABLED_TOOLS filter
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
