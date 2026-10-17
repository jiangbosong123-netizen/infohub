@echo off
rem History backfill and audits (docs/WINDOWS_CUTOVER.md steps 7 and 8). Run after update.cmd.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\backfill.ps1"
