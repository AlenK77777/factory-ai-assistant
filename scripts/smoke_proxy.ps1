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

$body = '{"message":"What is the status of order PO-10049?","source":"smoke"}'
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

$protected = @(
    "https://localhost:8443/logs",
    "https://localhost:8443/dashboards",
    "https://localhost:8443/api/logs",
    "https://localhost:8443/api/logs/download",
    "https://localhost:8443/api/dashboards",
    "https://localhost:8443/api/dashboards/excel",
    "http://127.0.0.1:7777/logs",
    "http://127.0.0.1:7777/dashboards",
    "http://127.0.0.1:7777/api/logs",
    "http://127.0.0.1:7777/api/logs/download",
    "http://127.0.0.1:7777/api/dashboards",
    "http://127.0.0.1:7777/api/dashboards/excel"
)
foreach ($url in $protected) {
    $code = curl.exe -k -sS -o NUL --max-time 20 -w "%{http_code}" $url
    Check "Password required for $url" ($code -eq "401") "HTTP $code"
}

function Save-Authed([string]$Url, [string]$OutFile) {
    curl.exe -k -sS --max-time 40 -u "${user}:${password}" -o $OutFile -w "%{http_code}" $Url
}

$logsPage = Save-Authed "https://localhost:8443/logs" "$env:TEMP\factory-logs.html"
$logsHtml = Get-Content "$env:TEMP\factory-logs.html" -Raw -ErrorAction SilentlyContinue
Check "Logs page opens with the password" ($logsPage -eq "200" -and $logsHtml -match "Logs") "HTTP $logsPage"

$directLogsPage = Save-Authed "http://127.0.0.1:7777/dashboards" "$env:TEMP\factory-dash-page.html"
$directLogsHtml = Get-Content "$env:TEMP\factory-dash-page.html" -Raw -ErrorAction SilentlyContinue
Check "Direct dashboards page opens with the password" ($directLogsPage -eq "200" -and $directLogsHtml -match "Dashboards") "HTTP $directLogsPage"

$logsApi = Save-Authed "https://localhost:8443/api/logs" "$env:TEMP\factory-logs.json"
try { $logsJson = Get-Content "$env:TEMP\factory-logs.json" -Raw | ConvertFrom-Json } catch { $logsJson = $null }
Check "Logs API works with the password" ($logsApi -eq "200" -and $null -ne $logsJson.events) "HTTP $logsApi"

$directLogsApi = Save-Authed "http://127.0.0.1:7777/api/logs" "$env:TEMP\factory-logs-direct.json"
try { $directLogsJson = Get-Content "$env:TEMP\factory-logs-direct.json" -Raw | ConvertFrom-Json } catch { $directLogsJson = $null }
Check "Direct logs API works with the password" ($directLogsApi -eq "200" -and $null -ne $directLogsJson.events) "HTTP $directLogsApi"

$download = Save-Authed "https://localhost:8443/api/logs/download" "$env:TEMP\factory-events.txt"
Check "Log download works with the password" ($download -eq "200") "HTTP $download"

$dash = Save-Authed "https://localhost:8443/api/dashboards" "$env:TEMP\factory-dash.json"
try { $dashJson = Get-Content "$env:TEMP\factory-dash.json" -Raw | ConvertFrom-Json } catch { $dashJson = $null }
$dashHtmlOk = $dashJson -and $dashJson.generated_at -and ($dashJson.html -match "<svg") -and ($dashJson.html -match "Generated at")
Check "Dashboard API works with the password" ($dash -eq "200" -and $dashHtmlOk) "HTTP $dash"

$directDash = Save-Authed "http://127.0.0.1:7777/api/dashboards" "$env:TEMP\factory-dash-direct.json"
try { $directDashJson = Get-Content "$env:TEMP\factory-dash-direct.json" -Raw | ConvertFrom-Json } catch { $directDashJson = $null }
Check "Direct dashboard API works with the password" ($directDash -eq "200" -and $directDashJson.generated_at) "HTTP $directDash"

$excel = Save-Authed "https://localhost:8443/api/dashboards/excel" "$env:TEMP\factory-dash.xlsx"
$excelBytes = @()
if (Test-Path "$env:TEMP\factory-dash.xlsx") { $excelBytes = [System.IO.File]::ReadAllBytes("$env:TEMP\factory-dash.xlsx") }
$isZip = $excelBytes.Length -gt 2 -and $excelBytes[0] -eq 80 -and $excelBytes[1] -eq 75
Check "Dashboard Excel download works with the password" ($excel -eq "200" -and $isZip) "HTTP $excel"

$envPasswords = @()
$postgresPassword = $null
$missingSecrets = @()
foreach ($name in @("POSTGRES_PASSWORD", "MCP_DB_PASSWORD", "ADMIN_PASSWORD", "INDEX_DB_PASSWORD")) {
    $value = $null
    foreach ($line in Get-Content (Join-Path $Root ".env")) {
        if ($line -match "^\s*$name=(.*)$") { $value = $Matches[1].Trim() }
    }
    if ($name -eq "POSTGRES_PASSWORD") { $postgresPassword = $value }
    if ($value) { $envPasswords += $value } else { $missingSecrets += $name }
}
Check "Database password is not the placeholder" ($postgresPassword -and $postgresPassword -ne "change-me") "POSTGRES_PASSWORD is still change-me"

$secretHits = 0
$unreadable = @()
$logRoot = Join-Path $Root "logs"
if (Test-Path $logRoot) {
    Get-ChildItem $logRoot -Recurse -File -Force | ForEach-Object {
        $stream = $null
        $reader = $null
        try {
            # The chat process keeps its log files open. ReadWrite still lets us scan them.
            $stream = [System.IO.File]::Open($_.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $reader = New-Object System.IO.StreamReader($stream)
            $text = $reader.ReadToEnd()
        } catch {
            $script:unreadable += $_.FullName
            return
        } finally {
            if ($reader) { $reader.Dispose() }
            elseif ($stream) { $stream.Dispose() }
        }
        foreach ($secret in $envPasswords) {
            $from = 0
            while (($at = $text.IndexOf($secret, $from, [System.StringComparison]::Ordinal)) -ge 0) {
                $script:secretHits++
                $from = $at + $secret.Length
            }
        }
    }
}
$secretDetail = "matches=$secretHits"
if ($missingSecrets.Count -gt 0) { $secretDetail = "missing=$($missingSecrets -join ',') $secretDetail" }
if ($unreadable.Count -gt 0) { $secretDetail = "unreadable=$($unreadable.Count) $secretDetail" }
Check "Logs do not contain passwords from .env" ($secretHits -eq 0 -and $unreadable.Count -eq 0 -and $missingSecrets.Count -eq 0) $secretDetail

$published = docker compose ps --all --format "{{.Name}} {{.Ports}}" | Out-String
$openToAll = $published.Contains("0.0.0.0:") -or $published.Contains("[::]:")
Check "Published ports are not on all interfaces" (-not $openToAll) ($published.Trim() -replace '\s+', ' ')

if ($failed) { exit 1 }
Write-Host "Smoke test passed."
