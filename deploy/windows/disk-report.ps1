# Show what fills the C: drive, without changing anything. Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; .\disk-report.cmd
#
# C: had 31 GB free on 10-14 and 9.2 GB on 10-27, while InfoHub's database grew about 0.7 GB, and
# removing old images after each update (#287) did not stop it. This lists the usual suspects:
# Docker's disk files and its own accounting, Windows Update downloads, the hibernation and page
# files, the recycle bin and the big folders of this user. It deletes nothing. Send Claude a photo.
$ErrorActionPreference = "SilentlyContinue"

function GB($bytes) { return "{0,7:N1} GB" -f ($bytes / 1GB) }

function Folder-Bytes($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($sum) { return $sum } else { return 0 }
}

$drive = Get-PSDrive C
Write-Host "== C: $(GB $drive.Free) free of $(GB ($drive.Free + $drive.Used))"

Write-Host ""
Write-Host "== Docker's own accounting (images, containers, volumes, build cache)"
docker system df

Write-Host ""
Write-Host "== Disk files of Docker and other Linux systems (these only grow unless compacted)"
$disks = @()
$disks += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Docker") -Recurse -Force -Filter *.vhdx -ErrorAction SilentlyContinue
$disks += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Packages") -Recurse -Force -Filter ext4.vhdx -ErrorAction SilentlyContinue
foreach ($disk in $disks) { Write-Host "$(GB $disk.Length)  $($disk.FullName)" }
if ($disks.Count -eq 0) { Write-Host "   (none found under $env:LOCALAPPDATA)" }

Write-Host ""
Write-Host "== Windows"
foreach ($file in @("C:\hiberfil.sys", "C:\pagefile.sys", "C:\swapfile.sys")) {
    $item = Get-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    if ($item) { Write-Host "$(GB $item.Length)  $file" }
}
Write-Host "$(GB (Folder-Bytes 'C:\Windows\SoftwareDistribution\Download'))  Windows Update downloads"
Write-Host "$(GB (Folder-Bytes 'C:\$Recycle.Bin'))  recycle bin"
Write-Host "$(GB (Folder-Bytes $env:TEMP))  temporary files of this user"

Write-Host ""
Write-Host "== Biggest folders of this user ($env:USERPROFILE), top 10"
$rows = foreach ($folder in Get-ChildItem -LiteralPath $env:USERPROFILE -Directory -Force -ErrorAction SilentlyContinue) {
    [pscustomobject]@{ Bytes = (Folder-Bytes $folder.FullName); Path = $folder.FullName }
}
$rows | Sort-Object Bytes -Descending | Select-Object -First 10 | ForEach-Object { Write-Host "$(GB $_.Bytes)  $($_.Path)" }
foreach ($folder in @("AppData\Local\Docker", "AppData\Local\Temp", "AppData\Local\Packages")) {
    Write-Host "$(GB (Folder-Bytes (Join-Path $env:USERPROFILE $folder)))  $folder"
}

Write-Host ""
Write-Host "DONE: nothing was changed. Send a photo of this window to Claude." -ForegroundColor Green
