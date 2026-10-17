# One-command update of InfoHub on the Windows host: pull, build, start, then confirm that the
# running site reports the new version. Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1
#
# The leading git pull only matters the first time (it brings this script); the script pulls
# again and stops before building if the pull did not work, so the order can no longer go wrong.
# The database is backed up before any schema upgrade by the migrate service, as always.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")

function Stop-Update($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    Write-Host "Nothing was restarted after this point. Send a photo of this window to Claude."
    exit 1
}

Write-Host "== 1/3 git pull"
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Stop-Update "git pull failed (see the lines above)" }
git fetch --quiet origin
$head = (git rev-parse --short=12 HEAD).Trim()
$main = (git rev-parse --short=12 origin/main).Trim()
if ($head -ne $main) { Stop-Update "this folder is at $head but GitHub main is $main" }

Write-Host "== 2/3 build and start $head (a schema upgrade can add a few minutes)"
$env:APP_VERSION = $head
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Stop-Update "docker compose up failed (see the lines above)" }

Write-Host "== 3/3 waiting for the site to report $head"
$deadline = (Get-Date).AddMinutes(15)
$last = ""
while ((Get-Date) -lt $deadline) {
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ready" -TimeoutSec 5
        if ($ready.ready -and $ready.version -eq $head -and $ready.worker.version -eq $head) {
            Write-Host ""
            Write-Host "DONE: InfoHub is running $head (site and worker)." -ForegroundColor Green
            exit 0
        }
        $last = "site $($ready.version), worker $($ready.worker.version), ready $($ready.ready)"
    } catch {
        # /api/ready answers 503 until ready, and Invoke-RestMethod throws on it; the body still
        # says what is missing.
        $last = "site not answering yet"
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try {
                $body = $_.ErrorDetails.Message | ConvertFrom-Json
                $last = "site $($body.version), worker $($body.worker.version), ready $($body.ready), issues: $($body.issues -join ', ')"
            } catch { }
        }
    }
    Start-Sleep -Seconds 10
}
Stop-Update "after 15 minutes the site still reports: $last"
