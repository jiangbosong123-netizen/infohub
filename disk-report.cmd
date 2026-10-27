@echo off
rem Show what fills the C: drive, changing nothing. From PowerShell: cd ~\Server\infohub; git pull; .\disk-report.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\disk-report.ps1"
