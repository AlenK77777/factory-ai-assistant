# Create the local HTTPS certificate and the admin password file.
# Uses openssl inside a pinned Alpine image, so Windows does not need extra software.
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\generate_certs.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# The NGINX image has no openssl binary. This pinned Alpine image supplies it.
$OpenSslImage = "alpine:3.21.3"
$NginxImage = "nginx:1.28.0-alpine"
$CertDir = Join-Path $Root "nginx\certs"
$AuthDir = Join-Path $Root "nginx\auth"
New-Item -ItemType Directory -Force -Path $CertDir, $AuthDir | Out-Null

if (-not (Test-Path (Join-Path $CertDir "localhost.crt")) -or -not (Test-Path (Join-Path $CertDir "localhost.key"))) {
    Write-Host "Generating a self-signed certificate for localhost..."
    $conf = @"
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = localhost
[v3]
subjectAltName = DNS:localhost,IP:127.0.0.1
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"@
    $confFile = Join-Path $CertDir "openssl.cnf"
    Set-Content -Path $confFile -Value $conf -Encoding ascii
    docker run --rm -v "${CertDir}:/certs" $OpenSslImage sh -c "apk add --no-cache openssl >/dev/null && openssl req -x509 -nodes -newkey rsa:2048 -keyout /certs/localhost.key -out /certs/localhost.crt -days 825 -config /certs/openssl.cnf"
    if ($LASTEXITCODE -ne 0) { throw "openssl failed while creating the certificate." }
    Remove-Item $confFile -ErrorAction SilentlyContinue
    Write-Host "Wrote nginx\certs\localhost.crt and localhost.key"
} else {
    Write-Host "Certificate already exists."
}

$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { throw ".env is missing. Copy .env.example to .env first." }
$lines = Get-Content $envFile
function Read-Env([string]$Name) {
    foreach ($line in $lines) {
        if ($line -match "^\s*$Name=(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}

function Set-EnvLine([string]$Name, [string]$Value) {
    $found = $false
    $updated = foreach ($line in $script:lines) {
        if ($line -match "^\s*$Name=") { $found = $true; "$Name=$Value" } else { $line }
    }
    if (-not $found) { $updated = @($updated) + "$Name=$Value" }
    $script:lines = $updated
}

$user = Read-Env "ADMIN_USER"
if (-not $user) { $user = "admin"; Set-EnvLine "ADMIN_USER" $user }
$password = Read-Env "ADMIN_PASSWORD"
if (-not $password -or $password -eq "change-me-admin") {
    $password = -join ((48..57 + 65..90 + 97..122 | Get-Random -Count 24 | ForEach-Object { [char]$_ }))
    Set-EnvLine "ADMIN_PASSWORD" $password
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($envFile, $lines, $utf8)
    Write-Host "Saved a new ADMIN_PASSWORD in .env. Open that file to see it. It is not printed here."
}

$passFile = Join-Path $env:TEMP "factory-admin-password.txt"
Set-Content -Path $passFile -Value $password -Encoding ascii -NoNewline
try {
    # Hash with musl crypt inside the NGINX image. OpenSSL $apr1$ and shell-copied hashes fail auth.
    $writer = Join-Path $Root "scripts\write_htpasswd.py"
    docker run --rm -e "ADMIN_USER=$user" -v "${AuthDir}:/auth" -v "${passFile}:/tmp/password:ro" -v "${writer}:/tmp/write_htpasswd.py:ro" $NginxImage sh -c "apk add --no-cache python3 >/dev/null && python3 /tmp/write_htpasswd.py"
    if ($LASTEXITCODE -ne 0) { throw "Failed while creating the password file." }
} finally {
    Remove-Item $passFile -ErrorAction SilentlyContinue
}
Write-Host "Wrote nginx\auth\htpasswd for user $user"
