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

Invoke-Tool "start_journey"
Invoke-Tool "record_consent" @{ consent = $true; notice_version = "v1.0" }

# Disabled by default (ENABLED_TOOLS): only run them if the server lists them.
if ($toolNames -contains "check_existing_eia") {
    Invoke-Tool "check_existing_eia" @{ pan = "ABCPE1234E"; email_id = "rahul@gmail.com" }
    Invoke-Tool "send_otp"
    Invoke-Tool "verify_otp" @{ otp_no = "123456" }
}

# Email OTP (mock code = MOCK_EMAIL_OTP, default 123456)
Invoke-Tool "send_email_otp" @{ email_id = "rahul@gmail.com" }
Invoke-Tool "verify_email_otp" @{ otp_no = "000000" }   # wrong code -> isError GW-011
Invoke-Tool "verify_email_otp" @{ otp_no = "123456" }

# Password setup: the customer sets the password in the browser, never in the chat.
Invoke-Tool "create_password_link"
Invoke-Tool "check_password_status"   # not set yet -> isError GW-021
Write-Host "Open the link above in a browser, set a password, then call check_password_status again." -ForegroundColor Cyan
