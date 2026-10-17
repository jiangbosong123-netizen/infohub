# Open /api/v1/signals/news for the AI research system and send it a key (D31, PLATFORM_DESIGN 5.1).
# From PowerShell:
#
#   cd ~\Server\infohub; git pull; .\signals.cmd
#
# 1. sets INFOHUB_API_SIGNALS_ENABLED=true in .env (other lines are kept as they are);
# 2. restarts the site so it reads .env, and waits until it answers;
# 3. issues a read:signals key for the consumer ai-research (an older key stops working);
# 4. sends the key to the Mac with Tailscale (it lands in Downloads) and deletes the local copy.
# The key is never printed.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")

function Stop-Signals($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    Write-Host "Send a photo of this window to Claude."
    exit 1
}

Write-Host "== 1/4 .env"
$envPath = Join-Path (Get-Location) ".env"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$text = [IO.File]::ReadAllText($envPath, $utf8)
if ($text -match "(?m)^INFOHUB_API_SIGNALS_ENABLED=") {
    $text = [regex]::Replace($text, "(?m)^INFOHUB_API_SIGNALS_ENABLED=.*$", "INFOHUB_API_SIGNALS_ENABLED=true")
} else {
    if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += "`n" }
    $text += "INFOHUB_API_SIGNALS_ENABLED=true`n"
}
[IO.File]::WriteAllText($envPath, $text, $utf8)

Write-Host "== 2/4 restart the site"
$env:APP_VERSION = (git rev-parse --short=12 HEAD).Trim()
docker compose up -d
if ($LASTEXITCODE -ne 0) { Stop-Signals "docker compose up failed (see the lines above)" }
$deadline = (Get-Date).AddMinutes(10)
$ready = $false
while ((Get-Date) -lt $deadline -and -not $ready) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 5
        $ready = $health.runtime.api_signals_enabled -eq $true -and $health.version -eq $env:APP_VERSION
    } catch { }
    if (-not $ready) { Start-Sleep -Seconds 10 }
}
if (-not $ready) { Stop-Signals "the site did not come back with the signals API on within 10 minutes" }

Write-Host "== 3/4 issue the key"
$output = docker compose run --rm --no-deps -T migrate python cli.py signals-key ai-research 2>$null
$token = @($output | Where-Object { $_ -match "^ih1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$" })
if ($token.Count -ne 1) { Stop-Signals "the key could not be issued" }
$file = Join-Path $env:TEMP "infohub-signals.token"
[IO.File]::WriteAllText($file, $token[0] + "`n", (New-Object System.Text.ASCIIEncoding))

Write-Host "== 4/4 send it to the Mac"
tailscale file cp $file macbook-pro:
$sent = $LASTEXITCODE
Remove-Item $file
if ($sent -ne 0) { Stop-Signals "tailscale could not send the key to macbook-pro (is the Mac awake?)" }
Write-Host ""
Write-Host "DONE: the signals API is on and the key for ai-research is in the Mac's Downloads folder." -ForegroundColor Green
exit 0
