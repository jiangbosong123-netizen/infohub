# Show what fills the C: drive, without changing anything. Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; .\disk-report.cmd
#
# C: had 31 GB free on 10-14 and 9.2 GB on 10-27, while InfoHub's database grew about 0.7 GB, and
# removing old images after each update (#287) did not stop it. On 10-31 it fell from 10 GB to
# under 1 GB in 30 hours. This lists the usual suspects: Docker's disk files and its own accounting,
# Windows Update downloads, the hibernation and page files, the recycle bin, the big folders of
# this user and of C:, the big files written in the last 3 days, and how fast C: is filling while
# the report runs. It deletes nothing. Send Claude a photo.
$ErrorActionPreference = "SilentlyContinue"

function GB($bytes) { return "{0,7:N1} GB" -f ($bytes / 1GB) }

function Folder-Bytes($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($sum) { return $sum } else { return 0 }
}

# The size in a folder listing can be hours old for a file another program keeps open, such as
# Docker's disk; this asks the file system for the space the file takes on C: right now.
Add-Type -Namespace InfoHub -Name Disk -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern uint GetCompressedFileSizeW(string lpFileName, out uint lpFileSizeHigh);
'@

function On-Disk($path) {
    try {
        $high = [uint32]0
        $low = [InfoHub.Disk]::GetCompressedFileSizeW($path, [ref]$high)
        if ($low -eq [uint32]::MaxValue -and $high -eq 0) { return $null }  # could not ask
        return ([uint64]$high -shl 32) + $low
    } catch { return $null }
}

$drive = Get-PSDrive C
$startFree = $drive.Free
$startTime = Get-Date
Write-Host "== C: $(GB $drive.Free) free of $(GB ($drive.Free + $drive.Used))   ($($startTime.ToString('yyyy-MM-dd HH:mm')))"

Write-Host ""
Write-Host "== Docker's own accounting (images, containers, volumes, build cache)"
docker system df

Write-Host ""
Write-Host "== Disk files of Docker and other Linux systems (these only grow unless compacted)"
$disks = @()
$disks += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Docker") -Recurse -Force -Filter *.vhdx -ErrorAction SilentlyContinue
$disks += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Packages") -Recurse -Force -Filter ext4.vhdx -ErrorAction SilentlyContinue
foreach ($disk in $disks) {
    $used = On-Disk $disk.FullName
    $sparse = if ($disk.Attributes -band [IO.FileAttributes]::SparseFile) { "sparse" } else { "not sparse" }
    Write-Host "$(GB $disk.Length)  $($disk.FullName)"
    if ($null -ne $used) { Write-Host "$(GB $used)    really on C: now, $sparse, written $($disk.LastWriteTime.ToString('MM-dd HH:mm'))" }
}
if ($disks.Count -eq 0) { Write-Host "   (none found under $env:LOCALAPPDATA)" }
$desktop = Get-Item -LiteralPath (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe")
if ($desktop) { Write-Host "   Docker Desktop $($desktop.VersionInfo.ProductVersion)" }

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
Write-Host "== Folders of C:, and big files written in the last 3 days (takes a few minutes, please wait)"
$cutoff = (Get-Date).ToUniversalTime().AddDays(-3)
$recent = New-Object System.Collections.Generic.List[System.IO.FileInfo]

function Scan-Folder([System.IO.DirectoryInfo]$root) {
    $bytes = [int64]0
    $stack = New-Object System.Collections.Generic.Stack[System.IO.DirectoryInfo]
    $stack.Push($root)
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        try {
            foreach ($entry in $dir.EnumerateFileSystemInfos()) {
                if ($entry -is [System.IO.DirectoryInfo]) {
                    # A link points into a folder that is counted where it really is.
                    if (-not ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { $stack.Push($entry) }
                } else {
                    $bytes += $entry.Length
                    if ($entry.Length -ge 50MB -and $entry.LastWriteTimeUtc -ge $cutoff) { $recent.Add($entry) }
                }
            }
        } catch {}
    }
    return $bytes
}

$seen = [int64]0
foreach ($file in (Get-Item -LiteralPath "C:\" -Force).GetFiles()) { $seen += $file.Length }
$tops = foreach ($folder in (Get-Item -LiteralPath "C:\" -Force).GetDirectories()) {
    if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
    Write-Progress -Activity "Reading C:" -Status $folder.FullName
    $bytes = Scan-Folder $folder
    $seen += $bytes
    [pscustomobject]@{ Bytes = $bytes; Path = $folder.FullName }
}
Write-Progress -Activity "Reading C:" -Completed
$tops | Sort-Object Bytes -Descending | Select-Object -First 12 | ForEach-Object { Write-Host "$(GB $_.Bytes)  $($_.Path)" }
$drive = Get-PSDrive C
$hidden = $drive.Used - $seen
if ($hidden -gt 0) { Write-Host "$(GB $hidden)  not readable by this report (restore points, file table, protected folders)" }
Write-Host ""
Write-Host "   big files written in the last 3 days:"
$recent | Sort-Object Length -Descending | Select-Object -First 15 | ForEach-Object {
    Write-Host "$(GB $_.Length)  $($_.LastWriteTime.ToString('MM-dd HH:mm'))  $($_.FullName)"
}
if ($recent.Count -eq 0) { Write-Host "   (none over 50 MB)" }

Write-Host ""
$minutes = ((Get-Date) - $startTime).TotalMinutes
$lost = ($startFree - $drive.Free) / 1MB
Write-Host ("== C: {0} free now: {1:N0} MB filled during the {2:N0} minutes of this report" -f (GB $drive.Free), $lost, $minutes)

Write-Host ""
Write-Host "DONE: nothing was changed. Send a photo of this window to Claude." -ForegroundColor Green
