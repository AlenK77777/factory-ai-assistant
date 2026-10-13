# Check the NGINX proxy in front of the chat.
# From the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\smoke_proxy.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Read-Env([string]$Name) {
    foreach ($line in Get-Content (Join-Path $Root ".env")) {
        if ($line -match "^\s*$Name=(.*)$") { return $Matches[1].Trim() }
    }
    throw "$Name is missing from .env"
}

$user = Read-Env "ADMIN_USER"
$password = Read-Env "ADMIN_PASSWORD"
$failed = $false

function Check([string]$Name, [bool]$Ok, [string]$Detail) {
    if ($Ok) { Write-Host "PASS  $Name" }
    else { Write-Host "FAIL  $Name  $Detail"; $script:failed = $true }
}

$headers = curl.exe -k -sI --max-time 15 "http://127.0.0.1:8088/"
$redirect = ($headers | Select-String -Pattern "^Location:\s*https://localhost:8443/?\s*$" -Quiet)
$status = ($headers | Select-String -Pattern "^HTTP/.*\s301\s" -Quiet)
Check "HTTP redirects to HTTPS" ($status -and $redirect) (($headers | Select-Object -First 8) -join " | ")

$page = curl.exe -k -sS --max-time 20 -o "$env:TEMP\factory-chat.html" -w "%{http_code}" "https://localhost:8443/"
$html = Get-Content "$env:TEMP\factory-chat.html" -Raw -ErrorAction SilentlyContinue
Check "Chat page loads" ($page -eq "200" -and $html -match "Factory assistant") "HTTP $page"

$body = '{"message":"What is the status of order PO-10049?"}'
Set-Content -Path "$env:TEMP\factory-chat-q.json" -Value $body -Encoding ascii
$answerRaw = curl.exe -k -sS --max-time 180 -H "Content-Type: application/json" --data-binary "@$env:TEMP\factory-chat-q.json" "https://localhost:8443/api/chat"
try { $answer = $answerRaw | ConvertFrom-Json } catch { $answer = $null }
$toolOk = $false
if ($answer -and $answer.tools) { $toolOk = @($answer.tools | Where-Object { $_.name -eq "get_order_status" }).Count -gt 0 }
Check "Question is answered through the proxy" ($toolOk -and $answer.answer) ($(if ($answer) { $answer.answer.Substring(0, [Math]::Min(120, $answer.answer.Length)) } else { $answerRaw.Substring(0, [Math]::Min(120, $answerRaw.Length)) }))

$noAuth = curl.exe -k -sS -o NUL --max-time 15 -w "%{http_code}" "https://localhost:8443/api/settings"
Check "Admin API rejects a request without the password" ($noAuth -eq "401") "HTTP $noAuth"

$withAuth = curl.exe -k -sS --max-time 20 -u "${user}:${password}" "https://localhost:8443/api/settings"
try { $settings = $withAuth | ConvertFrom-Json } catch { $settings = $null }
Check "Admin API works with the password" ($null -ne $settings.version) "response did not include a settings version"

$directChat = curl.exe -sS -o NUL --max-time 15 -w "%{http_code}" "http://127.0.0.1:7777/"
Check "Direct chat page stays open" ($directChat -eq "200") "HTTP $directChat"

$directAdmin = curl.exe -sS -o NUL --max-time 15 -w "%{http_code}" "http://127.0.0.1:7777/api/settings"
Check "Direct admin API rejects a request without the password" ($directAdmin -eq "401") "HTTP $directAdmin"

$directOk = curl.exe -sS --max-time 20 -u "${user}:${password}" "http://127.0.0.1:7777/api/settings"
try { $directSettings = $directOk | ConvertFrom-Json } catch { $directSettings = $null }
Check "Direct admin API works with the password" ($null -ne $directSettings.version) "response did not include a settings version"

$log = Join-Path $Root "logs\nginx\access.log"
$logText = ""
if (Test-Path $log) { $logText = Get-Content $log -Raw -ErrorAction SilentlyContinue }
Check "NGINX wrote an access log line" ($logText -match "GET /") $log

if ($failed) { exit 1 }
Write-Host "Smoke test passed."
