@echo off
rem Open the news signals API for the AI research system and send it a key. From PowerShell: cd ~\Server\infohub; git pull; .\signals.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\signals.ps1"
