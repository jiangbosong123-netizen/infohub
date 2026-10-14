# infohub read-only host check: docs/WINDOWS_CUTOVER.md steps 3 and 3b in one run.
#
# Reads data\ and .env (key names only) through read-only mounts, then measures the disk in a
# temporary folder and a temporary Docker volume and deletes both. Changes nothing else and does
# not touch the running containers. Everything printed is also saved to infohub-inspect.txt on
# the Desktop. Run from any folder:
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\inspect.ps1
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")
$image = "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
$report = Join-Path ([Environment]::GetFolderPath("Desktop")) "infohub-inspect.txt"
$inspect = Join-Path $env:TEMP "inspect_host.py"
$probe = Join-Path $env:TEMP "probe_storage.py"
$folder = Join-Path (Get-Location) "probe-tmp"

# Written through cmd: PowerShell 5.1 '>' saves UTF-16, which Python cannot read.
cmd /c "git show origin/main:deploy/windows/inspect_host.py > `"$inspect`""
cmd /c "git show origin/main:deploy/windows/probe_storage.py > `"$probe`""

& {
    "== git"
    git rev-parse HEAD
    git status --short
    "== docker compose ps"
    docker compose ps
    "== inspect_host (read-only)"
    docker run --rm --network none -v "$(Get-Location)\data:/data:ro" -v "$(Get-Location)\.env:/env/.env:ro" -v "${inspect}:/inspect_host.py:ro" $image python /inspect_host.py
    "== storage probe: Windows folder mounted into a container"
    New-Item -ItemType Directory -Force $folder | Out-Null
    docker run --rm -v "${folder}:/probe" -v "${probe}:/probe_storage.py:ro" $image python /probe_storage.py /probe
    Remove-Item -Recurse -Force $folder
    "== storage probe: Docker named volume"
    docker volume create infohub-probe | Out-Null
    docker run --rm -v infohub-probe:/probe -v "${probe}:/probe_storage.py:ro" $image python /probe_storage.py /probe
    docker volume rm infohub-probe | Out-Null
    "== done"
} *>&1 | Tee-Object -FilePath $report

"Saved to $report"
