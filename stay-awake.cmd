@echo off
rem Keep this PC awake on mains power. From PowerShell: cd ~\Server\infohub; git pull; .\stay-awake.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\stay-awake.ps1"
