<#
.SYNOPSIS
  Smoke test for the CIRL gateway MCP endpoint (Windows PowerShell 5.1+): the policy inquiry flow.

.EXAMPLE
  .\scripts\test_mcp.ps1 -BaseUrl http://localhost:8000 -ApiKey change-me
  .\scripts\test_mcp.ps1 -BaseUrl https://cirl-gateway.onrender.com   # uses $env:GATEWAY_API_KEY
#>
param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiKey = $env:GATEWAY_API_KEY,
    [string]$ConversationId = ("ps-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8)),
    [string]$Phone = "+917373345820"   # Ramesh: two policies. +919876545820 (Priya) has none.
)

$ErrorActionPreference = "Stop"
if (-not $ApiKey) { throw "Pass -ApiKey or set `$env:GATEWAY_API_KEY" }

function New-Headers([string]$Conversation, [string]$UserPhone) {
    @{
        "Authorization"        = "Bearer $ApiKey"
        "Accept"               = "application/json, text/event-stream"
        "X-Sa-Conversation-Id" = $Conversation
        "X-Sa-End-User-Phone"  = $UserPhone
        "X-Sa-Channel"         = "whatsapp"
    }
}
$headers = New-Headers $ConversationId $Phone
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
        Write-Host ("{0} JSON-RPC ERROR {1}: {2}" -f $Name, $resp.error.code, $resp.error.message) -ForegroundColor Red
        return
    }
    $payload = $resp.result.content[0].text | ConvertFrom-Json
    $colour = if ($resp.result.isError) { "Yellow" } else { "Green" }
    $code = if ($payload.error_code) { " " + $payload.error_code } else { "" }
    Write-Host ("{0} {1} -> status={2}{3}" -f $Name, (($Arguments | ConvertTo-Json -Compress)), $payload.status, $code) -ForegroundColor $colour
    Write-Host ("  " + ($payload.message_for_customer -replace "`n", "`n  "))
}

Write-Host "Conversation id: $ConversationId" -ForegroundColor Cyan
Write-Host ("health: " + ((Invoke-RestMethod "$BaseUrl/health") | ConvertTo-Json -Compress))

$init = Invoke-Mcp "initialize" @{
    protocolVersion = "2024-11-05"; capabilities = @{}; clientInfo = @{ name = "test_mcp.ps1"; version = "1.0" }
}
Write-Host ("initialize: protocolVersion={0} server={1}" -f $init.result.protocolVersion, $init.result.serverInfo.name)
$tools = Invoke-Mcp "tools/list"
Write-Host ("tools/list: " + (($tools.result.tools | ForEach-Object { $_.name }) -join ", "))

# Policy inquiry (mock code: 123456)
Invoke-Tool "list_policies"                                # code sent
Invoke-Tool "list_policies"                                # code still valid: not sent again
Invoke-Tool "verify_identity_otp" @{ otp_no = "000000" }   # wrong code -> E-302
Invoke-Tool "verify_identity_otp" @{ otp_no = "123456" }   # verified -> policy list
Invoke-Tool "list_policies"                                # verified: list straight away

# Unknown WhatsApp number
$headers = New-Headers ("unknown-" + $ConversationId) "+919000000001"
Invoke-Tool "list_policies"                                # GW-040 not_registered
