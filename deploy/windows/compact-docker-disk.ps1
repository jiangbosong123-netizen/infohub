# Give C: back the space that Docker's disk file no longer uses. Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; .\compact-docker-disk.cmd
#
# InfoHub's data lives in Docker's virtual disk (docker_data.vhdx). The file grows when the Linux
# side writes new blocks, such as the nightly backup's 3.5 GB of fresh files, but never shrinks when
# files are deleted: on 10-31 C: had under 1 GB left. This stops InfoHub (a few minutes; the gap is
# recorded and caught up), marks the free space inside the disk, quits Docker Desktop, compacts the
# file with diskpart (read-only attach), then starts Docker and InfoHub again and waits until the
# site answers. Volumes, images and data are never removed. Windows asks once to allow changes.
$ErrorActionPreference = "Continue"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # diskpart needs an administrator window; the rest runs in the new window, which stays open.
    Write-Host "Windows will ask to allow changes: click Yes. The rest runs in the new window."
    try {
        Start-Process powershell -Verb RunAs -ErrorAction Stop -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"")
    } catch {
        Write-Host ""
        Write-Host "STOPPED: Windows did not allow it (No was clicked); nothing was changed." -ForegroundColor Red
        exit 1
    }
    exit 0
}
Set-Location (Join-Path $env:USERPROFILE "Server\infohub")
$containers = @("infohub", "infohub-worker")
$stopped = $false

function Free-GB { return [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Disk-Released($path, $minutes) {
    # Nobody else may hold the file: not Docker's virtual machine, not diskpart.
    $deadline = (Get-Date).AddMinutes($minutes)
    while ($true) {
        try {
            $handle = [IO.File]::Open($path, "Open", "Read", "None")
            $handle.Close()
            return $true
        } catch {
            if ((Get-Date) -ge $deadline) { return $false }
            Start-Sleep -Seconds 5
        }
    }
}

function Start-InfoHub {
    Write-Host "== starting Docker Desktop and InfoHub again"
    docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Docker must never start while its data disk is still attached elsewhere (diskpart).
        if ($script:disk -and -not (Disk-Released $script:disk.FullName 1)) {
            return "Docker's disk file is still in use. Restart Windows; InfoHub then starts with Docker by itself"
        }
        # Through Explorer, so Docker Desktop runs as the normal user, not as administrator.
        Start-Process explorer.exe -ArgumentList "`"$env:ProgramFiles\Docker\Docker\Docker Desktop.exe`""
        $deadline = (Get-Date).AddMinutes(10)
        do {
            Start-Sleep -Seconds 5
            docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
        } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
        if ($LASTEXITCODE -ne 0) { return "Docker Desktop did not start within 10 minutes. Start it from the Start menu (or restart Windows); InfoHub then starts by itself" }
    }
    if ($script:stopped) {
        docker start $containers | Out-Null  # nothing happens to a container already running
        if ($LASTEXITCODE -ne 0) { return "docker start failed (see the lines above)" }
    }
    # The site checks the whole database before it answers: a few minutes on a multi-GB database.
    $deadline = (Get-Date).AddMinutes(15)
    while ((Get-Date) -lt $deadline) {
        try {
            $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/ready" -TimeoutSec 5
            if ($ready.ready) {
                docker update --restart unless-stopped $containers | Out-Null  # as compose.yaml has it
                return $null
            }
        } catch { }
        Start-Sleep -Seconds 10
    }
    return "the site did not report ready within 15 minutes"
}

function Stop-Compact($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    $problem = Start-InfoHub
    if ($problem) { Write-Host "InfoHub is NOT running again: $problem" -ForegroundColor Red }
    else { Write-Host "InfoHub is running again." }
    Write-Host "Send a photo of this window to Claude."
    exit 1
}

Write-Host "== 1/5 find Docker's disk file (do not open Docker Desktop until this window says DONE)"
$disk = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Docker\wsl") -Recurse -Force -Filter *.vhdx -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending | Select-Object -First 1
if (-not $disk) { Stop-Compact "no .vhdx file under $env:LOCALAPPDATA\Docker\wsl; nothing was changed" }
$before = Free-GB
Write-Host "   $($disk.FullName), $([math]::Round($disk.Length / 1GB, 1)) GB; C: free $before GB"
$image = (docker inspect --format "{{.Config.Image}}" infohub-worker 2>$null | Out-String).Trim()
if (-not $image) { Stop-Compact "the container infohub-worker was not found; nothing was changed" }
$sparse = [bool]($disk.Attributes -band [IO.FileAttributes]::SparseFile)
if ($disk.Attributes -band ([IO.FileAttributes]::Compressed -bor [IO.FileAttributes]::Encrypted)) {
    Stop-Compact "the disk file is compressed or encrypted, which diskpart cannot compact; nothing was changed"
}
try {
    $running = @((Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/pipeline" -TimeoutSec 10).jobs.running |
        ForEach-Object { $_.kind })
} catch { $running = @() }
if ($running -contains "backup" -or $running -contains "prune") {
    Stop-Compact "the nightly $($running -join ', ') job is running; try again in an hour. Nothing was changed"
}

# Sync, then trim: the Linux file system tells the virtual disk which blocks it no longer uses, and
# only those can be cut out of the file. Not fatal: diskpart may still find some.
$trim = @("run", "--rm", "--privileged", "--entrypoint", "sh", "-v", "infohub-data:/data", $image,
    "-c", "sync; fstrim -v /data; sync")
if ($sparse) {
    # A sparse file gives trimmed blocks back to C: at once; InfoHub keeps running.
    Write-Host "== 2/2 the disk file is sparse: trimming is enough, InfoHub keeps running"
    docker @trim
    if ($LASTEXITCODE -ne 0) { Stop-Compact "fstrim did not run (see the lines above); nothing else was changed" }
    Write-Host ""
    Write-Host "DONE: C: free $before GB -> $(Free-GB) GB; InfoHub kept running." -ForegroundColor Green
    exit 0
}

Write-Host "== 2/5 stop InfoHub (the worker finishes its current job first, up to 3 minutes)"
# 'always' starts the containers again whenever Docker starts, also after a manual stop: if this
# window is closed early, restarting Windows or opening Docker Desktop brings InfoHub back.
docker update --restart always $containers | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Compact "docker update failed (see the lines above); nothing was changed" }
$stopped = $true
docker stop -t 180 infohub-worker infohub | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Compact "docker stop failed (see the lines above)" }

Write-Host "== 3/5 mark the free space inside Docker's disk"
docker @trim
if ($LASTEXITCODE -ne 0) { Write-Host "   fstrim did not run; continuing" -ForegroundColor Yellow }

Write-Host "== 4/5 quit Docker Desktop and compact the disk file"
docker desktop stop --timeout 120 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    foreach ($name in @("Docker Desktop", "com.docker.backend", "com.docker.build")) {
        Stop-Process -Name $name -Force -ErrorAction SilentlyContinue
    }
}
$shutdown = Start-Process wsl.exe -ArgumentList "--shutdown" -NoNewWindow -PassThru
if (-not $shutdown.WaitForExit(120000)) { Stop-Compact "wsl --shutdown did not finish within 2 minutes; nothing was compacted" }
if (-not (Disk-Released $disk.FullName 2)) { Stop-Compact "Docker still had its disk file open after 2 minutes; nothing was compacted" }

$steps = Join-Path $env:TEMP "infohub-compact.txt"
Set-Content -LiteralPath $steps -Encoding ascii -Value @(
    "select vdisk file=`"$($disk.FullName)`"", "attach vdisk readonly", "compact vdisk", "detach vdisk")
diskpart /s $steps
$compacted = ($LASTEXITCODE -eq 0)
if (-not $compacted) {
    # diskpart stops at the failing line: make sure the file is not left attached for Docker.
    Set-Content -LiteralPath $steps -Encoding ascii -Value @("select vdisk file=`"$($disk.FullName)`"", "detach vdisk")
    diskpart /s $steps
}
$after = Free-GB
if (-not $compacted) { Stop-Compact "diskpart could not compact the file (see the lines above); C: free $before GB -> $after GB" }

Write-Host "== 5/5 start InfoHub again"
$problem = Start-InfoHub
if ($problem) {
    Write-Host ""
    Write-Host "STOPPED: the disk was compacted (C: free $before GB -> $after GB), but $problem" -ForegroundColor Red
    Write-Host "Send a photo of this window to Claude."
    exit 1
}
Write-Host ""
Write-Host "DONE: C: free $before GB -> $after GB; InfoHub is running again." -ForegroundColor Green
