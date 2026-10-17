@echo off
rem Update InfoHub on this Windows host. From PowerShell: cd ~\Server\infohub; git pull; .\update.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\update.ps1"
