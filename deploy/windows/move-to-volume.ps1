# One-time move of InfoHub's data from the .\data folder into the Docker named volume
# infohub-data (docs/WINDOWS_CUTOVER.md section 6b, decision D29). Run from any folder:
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\move-to-volume.ps1
#
# 1 checks, 2 stops InfoHub, 3 copies .\data into a new volume and proves the copy file by file
# (deploy/windows/copy_data.py), 4 updates the code to the compose file that uses the volume,
# 5 keeps the old folder as data-before-volume-<time>, 6 starts InfoHub on the volume.
# Any failed check before step 5 stops with .\data untouched, so the old setup can start again.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")
$image = "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
$volume = "infohub-data"
$copier = Join-Path $env:TEMP "copy_data.py"

function Stop-Move($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    Write-Host "Send a photo of this window to Claude."
    exit 1
}

"== 1/6 checks"
if (-not (Test-Path -PathType Leaf "data\app.db")) { Stop-Move "data\app.db not found" }
docker volume inspect $volume *> $null
if ($LASTEXITCODE -eq 0) { Stop-Move "the volume $volume already exists; nothing was changed" }
git fetch origin
if ($LASTEXITCODE -ne 0) { Stop-Move "git fetch failed; nothing was changed" }
# Written through cmd: PowerShell 5.1 '>' saves UTF-16, which Python cannot read.
cmd /c "git show origin/main:deploy/windows/copy_data.py > `"$copier`""
if ($LASTEXITCODE -ne 0) { Stop-Move "could not read copy_data.py from origin/main; nothing was changed" }
$needed = (Get-ChildItem -Recurse -File data | Measure-Object -Property Length -Sum).Sum
$free = (Get-PSDrive C).Free
"data folder {0:N0} MB, free on C: {1:N0} MB" -f ($needed / 1MB), ($free / 1MB)
if ($free -lt $needed + 2GB) { Stop-Move "not enough free disk space; nothing was changed" }

"== 2/6 stop InfoHub (the worker can take up to 3 minutes to finish its current job)"
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose down
if ($LASTEXITCODE -ne 0) { Stop-Move "docker compose down failed" }
$running = docker ps -q --filter "name=infohub"
if ($running) { Stop-Move "InfoHub containers are still running" }

"== 3/6 copy .\data into the volume and check every file"
docker volume create $volume | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Move "docker volume create failed; .\data is unchanged" }
docker run --rm --network none -v "$(Get-Location)\data:/from:ro" -v "${volume}:/to" -v "${copier}:/copy_data.py:ro" $image python /copy_data.py /from /to
if ($LASTEXITCODE -ne 0) {
    docker volume rm $volume | Out-Null
    Stop-Move "the copy did not verify; the new volume was removed and .\data is unchanged"
}

"== 4/6 update the code"
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { Stop-Move "git pull failed; .\data is unchanged and its copy is in the volume" }
if (-not (Select-String -Quiet -Path compose.yaml -Pattern $volume)) {
    Stop-Move "compose.yaml does not use the volume yet; .\data is unchanged"
}

"== 5/6 keep the old folder"
$archive = "data-before-volume-" + (Get-Date -Format yyyyMMdd-HHmm)
Rename-Item data $archive
if (-not $?) { Stop-Move "could not rename .\data; its copy is in the volume" }
"the old folder is kept as $archive"

"== 6/6 start InfoHub on the volume"
$env:APP_VERSION = (git rev-parse --short=12 HEAD)
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Stop-Move "docker compose up failed; the data is in the volume" }
docker compose ps
"== done"
