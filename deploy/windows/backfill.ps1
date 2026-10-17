# One-command history backfill and audits on the Windows host (docs/WINDOWS_CUTOVER.md steps 7
# and 8). Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\backfill.ps1
#
# The worker is paused while the backfill runs (it competes for the database write lock) and is
# always started again at the end, also after a failure. The site stays up. Every step is safe to
# run twice: a second run skips what is done. Everything printed is also saved to
# infohub-backfill.txt on the Desktop. No model calls are made; one step reads SEC file headers.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")
$log = Join-Path ([Environment]::GetFolderPath("Desktop")) "infohub-backfill.txt"
"== infohub backfill $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8
$env:APP_VERSION = (git rev-parse --short=12 HEAD).Trim()

$steps = @(
    @("legacy-backfill", "1000"),
    @("legacy-topic-backfill", "1000"),
    @("legacy-event-project"),
    @("routine-filings-mark", "--apply"),
    @("sec-acceptance-times", "--apply"),
    @("db-verify"),
    @("raw-verify"),
    @("evidence-verify"),
    @("db-event-audit")
)
# Not here (D32): legacy-curation-run and projection-builders-run, with their audits. They serve
# read paths that are switched off and would grow the database from about 0.5 GB to 5 GB.

$failed = $null
Write-Host "== pausing the worker (collection resumes at the end)"
docker compose stop worker
try {
    $number = 0
    foreach ($step in $steps) {
        $number += 1
        $name = $step -join " "
        Write-Host ""
        Write-Host "== $number/$($steps.Count) $name  ($(Get-Date -Format HH:mm))"
        "== $number/$($steps.Count) $name  $(Get-Date -Format s)" | Out-File -FilePath $log -Append -Encoding utf8
        docker compose run --rm --no-deps -T migrate python cli.py @step 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -ne 0) { $failed = $name; break }
    }
} finally {
    Write-Host ""
    Write-Host "== starting the worker again"
    docker compose start worker
}

Write-Host ""
if ($failed) {
    Write-Host "STOPPED at: $failed (exit code $LASTEXITCODE). The worker is running again." -ForegroundColor Red
    Write-Host "Send Claude a photo of this window, or the file infohub-backfill.txt on the Desktop."
    exit 1
}
Write-Host "DONE: backfill and audits finished. The log is infohub-backfill.txt on the Desktop." -ForegroundColor Green
exit 0
