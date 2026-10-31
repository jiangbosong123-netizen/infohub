@echo off
rem Give C: back the space Docker's disk no longer uses. From PowerShell: cd ~\Server\infohub; git pull; .\compact-docker-disk.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\compact-docker-disk.ps1"
