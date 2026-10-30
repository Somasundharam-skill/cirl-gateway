# CIRL Gateway (mock)

An MCP server (JSON-RPC 2.0 over HTTP) that a Perfox AI agent calls on WhatsApp for one use case: **policy inquiry**.
An employee asks about their insurance policies; the gateway finds their eIA from their WhatsApp number, checks
who they are with an OTP from CIRL, and returns their policy list as ready-to-send WhatsApp text.

All CIRL behaviour is **mocked**. Nothing here calls real CIRL, WhatsApp or Perfox services.

## The flow

1. The customer asks "show my policies". The agent calls `list_policies`.
2. The gateway reads `X-Sa-End-User-Phone`, looks it up in the employee directory and gets the eIA number.
   Unknown number → *"We couldn't find an eIA linked to this WhatsApp number. Please write to cirthelpdesk@cirl.co.in for help."*
3. Not verified yet in this conversation → CIRL `sendOtp` sends a code to the mobile and email registered on the eIA,
   and the customer gets *"To protect your details, we've sent a code to your registered mobile 73XXXXXX20 and email ra\*\*\*\*\*@example.com. Please enter it here."*
4. The customer replies with the code. The agent calls `verify_identity_otp`. On success the customer gets their policy list straight away.
5. For the rest of the conversation, `list_policies` returns the list without asking for a code again.

## Run locally

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then set GATEWAY_API_KEY
uvicorn app.main:app --env-file .env --port 8000
```

Check it, and run the tests:

```powershell
Invoke-RestMethod http://localhost:8000/health
.\scripts\test_mcp.ps1 -BaseUrl http://localhost:8000 -ApiKey <your key>
python -m pytest
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_API_KEY` | *(none)* | Bearer token required on `/mcp` and `/admin/audit`. If unset, every request is rejected. |
| `EMPLOYEE_DIRECTORY_PATH` | `data/employees.csv` | CSV with the columns `phone,eia_no,name`. A relative path is resolved against the project folder. A bad row stops the server at startup. |
| `TEST_PHONE` | *(none)* | **Web-chat testing only; never set it in production.** If a request has no (or an empty) `X-Sa-End-User-Phone`, this number is used instead. A real header always wins. The server logs a warning at startup while it is set. |
| `SESSION_TTL_MINUTES` | `60` | Idle time before a conversation's session (and its verified status) is dropped. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `CIRL_MODE_SEND_OTP` | `mock` | `mock` or `real` for CIRL sendOtp. |
| `CIRL_MODE_VERIFY_OTP` | `mock` | `mock` or `real` for CIRL verifyOtp. |
| `CIRL_MODE_GET_LIST_OF_POLICY` | `mock` | `mock` or `real` for CIRL getListOfPolicy. |
| `PORT` | `8000` | Port to bind (Render sets this). |

`real` mode is not implemented yet. A call in `real` mode returns `isError` with `GW-501` and makes no network call.

## Employee directory

`data/employees.csv` maps WhatsApp numbers to eIA numbers. Phones in any common format are normalised to `+91XXXXXXXXXX`.

| Phone | eIA | Name | Mock data |
|---|---|---|---|
| `+917373345820` | `1921008101111` | Ramesh | two policies: one Demat, one In Process |
| `+919876545820` | `1921008102222` | Priya | no policies |

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
- `tools/call` returns `{content: [{type: "text", text: "<JSON>"}], isError}`. The JSON is always
  `{ok, status, message_for_customer, message_for_agent}`; failures (`isError: true`, `ok: false`) also have `error_code` and `error_msg`.
  `message_for_customer` is the exact text to send to the customer.
- JSON-RPC errors are used only for protocol problems: parse error (-32700), invalid request or batch (-32600),
  unknown method (-32601), unknown tool, bad params or a missing `X-Sa-Conversation-Id` header (-32602).

Headers read on every `tools/call`: `X-Sa-Conversation-Id` (required, the session key), `X-Sa-End-User-Phone`
(identifies the employee) and `X-Sa-Channel`.

## Tools

### `list_policies {}`

| Situation | `status` | `message_for_customer` |
|---|---|---|
| Not verified in this conversation; a code is sent (CIRL `sendOtp`, valid 300 s) | `otp_sent` | To protect your details, we've sent a code to your registered mobile {masked mobile} and email {masked email}. Please enter it here. |
| Not verified, and a code sent earlier is still valid: **no new code is sent** | `otp_sent` | the same text |
| Verified: CIRL `getListOfPolicy` | `policies` | the policy list (below) |
| Verified, no policies | `no_policies` | You don't have any policies in your eIA yet. |
| Unknown number (`GW-040`, isError) | `not_registered` | We couldn't find an eIA linked to this WhatsApp number. Please write to cirthelpdesk@cirl.co.in for help. |
| 6th code within 24 hours (`E-203`, isError) | `otp_limit` | You've reached the limit for verification codes. Please try again later. |
| Locked (`E-304`, isError) | `otp_locked` | Too many incorrect attempts. Please try again after 24 hours. |

### `verify_identity_otp {otp_no}`

| Situation | `status` | `message_for_customer` |
|---|---|---|
| Correct code (`123456` in the mock): marks the conversation verified and returns the list at once | `policies` / `no_policies` | the policy list |
| Wrong code (`E-302`, isError) | `otp_incorrect` | That code is incorrect. Please re-enter it. |
| Not 6 digits (`GW-004`, isError; no attempt used) | `otp_incorrect` | That code is incorrect. Please re-enter it. |
| More than 300 s after the send (`E-303`, isError) | `otp_expired` | That code has expired. Ask me to show your policies again and I'll send a new one. |
| 3rd wrong code (`E-304`, isError): locked for 24 hours, for the eIA, across conversations | `otp_locked` | Too many incorrect attempts. Please try again after 24 hours. |
| No code sent yet (`GW-006`, isError) | `no_otp` | Please ask me to show your policies first, and I'll send you a code. |
| Unknown number (`GW-040`, isError) | `not_registered` | as above |

Other codes: `GW-501` CIRL `real` mode not implemented, or any unexpected CIRL error → `status: unavailable`,
*"Sorry, this service is temporarily unavailable. Please try again later."*

### Policy list format

One block per policy, separated by a blank line, in WhatsApp formatting:

```
Here are the policies in your eIA:

*LIC's Jeevan Anand*
Policy No: 36521478
Status: Demat
Sum Assured: Rs 5,00,000
Next Premium Due: 15-01-2027

Policy No: 2310987654
Status: In Process
```

- The bold line is the policy name, or the insurer name if the policy name is empty.
- Sum assured (in Indian digit grouping) and next premium due date are shown only when present.
- *In Process* policies show only the policy number and status.

## CIRL client (mock)

`app/cirl_client.py` is the only module that talks to CIRL, one function per API. The mock keeps the OTP state CIRL
would keep, per eIA: max 5 sends in 24 hours (`E-203`), 300 s validity (`E-303`), and a 24-hour lock after 3 wrong
codes (`E-304`). The code is always `123456`.

> **The `getListOfPolicy` response shape and the two sample policies are placeholders.** CIRL's API document was not
> available when this was written. The field names are defined once at the top of `cirl_client.py` (`POLICY_*`),
> so swapping in the exact shape is a change in that file only.

## Data minimisation and audit

- The OTP is never stored or logged. Sessions keep only the CIRL transaction number, the expiry time, the masked
  mobile and email, and whether the conversation is verified.
- Every tool call writes one masked audit entry (`timestamp`, `conversation_id`, `tool`, `event`, `input`, `output`,
  `ok`, `error_code`) to stdout as a JSON line and to an in-memory buffer read by `/admin/audit`.
- Masking: OTP `******`, eIA number and policy numbers last 4 only (`XXXX1478`), phone `+91XXXXXX5820`,
  email `ra*****@example.com`. Policy numbers are masked inside the customer text too; the customer still gets them in full.
- Audit events: `otp_sent`, `otp_already_sent`, `identity_verified`, `policies_listed`, `otp_locked`, `not_registered`, `tool_failed`.
- Request bodies, headers and the bearer key are never logged.
- Sessions and audit entries are in memory only, so they are lost on restart. Sessions sit behind the `SessionStore`
  interface (`app/sessions.py`), so a database-backed store can replace the in-memory one later.

## Deploy to Render

1. Push this folder to a Git repo.
2. In Render, choose **New → Blueprint** and select the repo. `render.yaml` defines one web service with health check `/health`.
3. Fill in the env vars Render prompts for. `GATEWAY_API_KEY` is required; the others have defaults in `render.yaml`.
   **Leave `TEST_PHONE` empty in production.**
4. Python is pinned in `.python-version` (3.12). If your Render account needs a full version, add `PYTHON_VERSION` (e.g. `3.12.8`).
5. After deploy, run: `.\scripts\test_mcp.ps1 -BaseUrl https://<service>.onrender.com -ApiKey <key>`.

The free plan sleeps when idle, and that clears all in-memory sessions, audit entries and mock OTP state.

## Layout

```
app/
  main.py          FastAPI app: /health, /mcp, /admin/audit, bearer auth
  mcp.py           JSON-RPC 2.0 dispatch
  tools.py         list_policies, verify_identity_otp, policy text formatting
  directory.py     employee directory (phone -> eIA) from CSV
  cirl_client.py   one function per CIRL API; mock or real (NotImplementedError)
  sessions.py      SessionStore interface + in-memory TTL store
  audit.py         masked audit log
  masking.py       OTP / eIA / policy / phone / email masking
  validation.py    phone normalisation, OTP format
  config.py        env settings
  clock.py         time source (patched in tests)
data/employees.csv
tests/             pytest suite
scripts/test_mcp.ps1
```
