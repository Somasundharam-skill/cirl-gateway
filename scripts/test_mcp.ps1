<#
.SYNOPSIS
  Smoke test for the CIRL gateway MCP endpoint (Windows PowerShell 5.1+).

.EXAMPLE
  .\scripts\test_mcp.ps1 -BaseUrl http://localhost:8000 -ApiKey change-me
  .\scripts\test_mcp.ps1 -BaseUrl https://cirl-gateway.onrender.com   # uses $env:GATEWAY_API_KEY
#>
param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiKey = $env:GATEWAY_API_KEY,
    [string]$ConversationId = ("ps-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8)),
    [string]$Phone = "+919876545820"
)

$ErrorActionPreference = "Stop"
if (-not $ApiKey) { throw "Pass -ApiKey or set `$env:GATEWAY_API_KEY" }

$headers = @{
    "Authorization"         = "Bearer $ApiKey"
    "Accept"                = "application/json, text/event-stream"
    "X-Sa-Conversation-Id"  = $ConversationId
    "X-Sa-End-User-Phone"   = $Phone
    "X-Sa-Channel"          = "whatsapp"
}
$script:nextId = 1

function Invoke-Mcp([string]$Method, $Params) {
    $body = @{ jsonrpc = "2.0"; id = $script:nextId; method = $Method }
    if ($null -ne $Params) { $body.params = $Params }
    $script:nextId++
    Invoke-RestMethod -Method Post -Uri "$BaseUrl/mcp" -Headers $headers -ContentType "application/json" `
        -Body ($body | ConvertTo-Json -Depth 10 -Compress)
}

function Invoke-Tool([string]$Name, [hashtable]$Arguments = @{}) {
    $resp = Invoke-Mcp "tools/call" @{ name = $Name; arguments = $Arguments }
    if ($resp.error) {
        Write-Host ("{0,-20} JSON-RPC ERROR {1}: {2}" -f $Name, $resp.error.code, $resp.error.message) -ForegroundColor Red
        return
    }
    $payload = $resp.result.content[0].text | ConvertFrom-Json
    $colour = if ($resp.result.isError) { "Yellow" } else { "Green" }
    Write-Host ("{0,-20} isError={1}" -f $Name, $resp.result.isError) -ForegroundColor $colour
    Write-Host ("    " + ($payload | ConvertTo-Json -Depth 10 -Compress))
    return $payload
}

function Say([string]$Message) {
    Write-Host ("customer> " + $Message) -ForegroundColor Cyan
    $payload = Invoke-Tool "journey_turn" @{ customer_message = $Message }
    if ($payload) {
        Write-Host ("  [{0}, recognised={1}]" -f $payload.state, $payload.recognised) -ForegroundColor DarkGray
        Write-Host ("  " + ($payload.message_for_customer -replace "`n", "`n  "))
    }
    return $payload
}

Write-Host "Conversation id: $ConversationId" -ForegroundColor Cyan
Write-Host ("health: " + ((Invoke-RestMethod "$BaseUrl/health") | ConvertTo-Json -Compress))

$init = Invoke-Mcp "initialize" @{
    protocolVersion = "2024-11-05"; capabilities = @{}; clientInfo = @{ name = "test_mcp.ps1"; version = "1.0" }
}
Write-Host ("initialize: protocolVersion={0} server={1}" -f $init.result.protocolVersion, $init.result.serverInfo.name)

$tools = Invoke-Mcp "tools/list"
$toolNames = @($tools.result.tools | ForEach-Object { $_.name })
Write-Host ("tools/list: " + ($toolNames -join ", "))

if ($toolNames -contains "journey_turn") {
    # The whole journey through one tool (mock email code = MOCK_EMAIL_OTP, default 123456).
    $null = Say "Hi"
    $null = Say "What is this?"          # recognised=false
    $null = Say "yes"
    $null = Say "my email is rahul@gmail.com"
    $null = Say "000000"                 # wrong code
    $null = Say "RESEND"
    $null = Say "123456"                 # verified -> password link
    $null = Say "DONE"                   # not set yet
    Write-Host "Open the link above in a browser and set a password, then type DONE here (or press Enter to skip)." -ForegroundColor Cyan
    $reply = $null
    try { $reply = Read-Host "customer" } catch { Write-Host "(non-interactive: skipped)" }
    if ($reply) {
        $null = Say $reply
        $null = Say "2"                  # KYC later
        $null = Say "Hi"                 # back to the KYC question
    }
}

# Single-step tools: disabled by default (ENABLED_TOOLS); only run them if the server lists them.
$single = "single-" + $ConversationId
if ($toolNames -contains "start_journey") {
    $headers["X-Sa-Conversation-Id"] = $single
    Invoke-Tool "start_journey" | Out-Null
    Invoke-Tool "record_consent" @{ consent = $true; notice_version = "v1.0" } | Out-Null
    if ($toolNames -contains "check_existing_eia") {
        Invoke-Tool "check_existing_eia" @{ pan = "ABCPE1234E"; email_id = "rahul@gmail.com" } | Out-Null
        Invoke-Tool "send_otp" | Out-Null
        Invoke-Tool "verify_otp" @{ otp_no = "123456" } | Out-Null
    }
    Invoke-Tool "send_email_otp" @{ email_id = "rahul@gmail.com" } | Out-Null
    Invoke-Tool "send_email_otp" @{ resend = $true } | Out-Null
    Invoke-Tool "verify_email_otp" @{ otp_no = "123456" } | Out-Null
    Invoke-Tool "create_password_link" | Out-Null
    Invoke-Tool "check_password_status" | Out-Null   # not set yet -> isError GW-021
}
