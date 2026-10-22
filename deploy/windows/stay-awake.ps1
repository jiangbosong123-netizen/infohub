# Keep this PC awake on mains power, so InfoHub keeps collecting. Paste this one line into PowerShell:
#
#   cd ~\Server\infohub; git pull; .\stay-awake.cmd
#
# On 2026-10-22 the PC went to sleep for about 9 hours and nothing was collected. While plugged in,
# for the active power plan, this sets "sleep" and "hibernate" to never and makes closing the lid do
# nothing (it is a laptop). The screen may still turn off; that does not stop anything. Undo it in
# Settings > System > Power, or Control Panel > Power Options > Choose what closing the lid does.
$ErrorActionPreference = "Continue"

function Stop-Script($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    Write-Host "Send a photo of this window to Claude."
    exit 1
}

function Ac-Value($group, $setting) {
    # The query is localized; its last two hex numbers are the AC and DC values.
    $values = @(powercfg /query SCHEME_CURRENT $group $setting | Select-String -Pattern "0x[0-9a-fA-F]{8}" -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Value })
    if ($values.Count -lt 2) { return $null }
    return [Convert]::ToInt64($values[$values.Count - 2], 16)
}

function Show($when) {
    $sleep = Ac-Value SUB_SLEEP STANDBYIDLE
    $hibernate = Ac-Value SUB_SLEEP HIBERNATEIDLE
    $lid = Ac-Value SUB_BUTTONS LIDACTION
    Write-Host "== ${when}: sleep after $($sleep / 60) min, hibernate after $($hibernate / 60) min (0 = never), lid action $lid (0 = do nothing)"
    return @($sleep, $hibernate, $lid)
}

$null = Show "before"
powercfg /change standby-timeout-ac 0
if ($LASTEXITCODE -ne 0) { Stop-Script "powercfg could not change the sleep setting" }
powercfg /change hibernate-timeout-ac 0
if ($LASTEXITCODE -ne 0) { Stop-Script "powercfg could not change the hibernate setting" }
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
if ($LASTEXITCODE -ne 0) { Stop-Script "powercfg could not change what closing the lid does" }
powercfg /setactive SCHEME_CURRENT

$after = Show "after"
# Hibernate or the lid may not exist on this PC (no value): only a value that is not 0 is a failure.
if ($after[0] -ne 0 -or ($null -ne $after[1] -and $after[1] -ne 0) -or ($null -ne $after[2] -and $after[2] -ne 0)) {
    Stop-Script "the settings did not stay as set"
}
Write-Host ""
Write-Host "DONE: while plugged in this PC no longer sleeps, also with the lid closed (the screen can still turn off)." -ForegroundColor Green
