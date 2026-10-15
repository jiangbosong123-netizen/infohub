#!/bin/bash
# Install the daily off-site pull on this Mac (D25, docs/RUNBOOK.md "异地备份到 Mac"):
#
#   deploy/macos/offsite-install.sh https://windows-server.<tailnet>.ts.net PATH/TO/TOKEN-FILE
#
# The token file comes from `python cli.py offsite-key` on the Windows host. It is copied to
# ~/.infohub/offsite.token (mode 600) together with offsite_pull.py. A LaunchAgent then runs the
# pull at 12:30 and 20:30 (launchd catches up after sleep) into ~/InfohubBackups, keeping one
# verified bundle (about 5 GB). Run it again to replace the token or the script; remove it with:
#
#   launchctl bootout gui/$(id -u)/com.infohub.offsite-pull
#   rm ~/Library/LaunchAgents/com.infohub.offsite-pull.plist
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 https://windows-server.<tailnet>.ts.net TOKEN-FILE" >&2
  exit 2
fi
ORIGIN="${1%/}"
TOKEN_SOURCE="$2"
case "$ORIGIN" in
  https://*.ts.net) ;;
  *) echo "the origin must be the host's private https://...ts.net address" >&2; exit 2 ;;
esac

LABEL="com.infohub.offsite-pull"
STATE="$HOME/.infohub"
DEST="$HOME/InfohubBackups"
LOG="$HOME/Library/Logs/infohub-offsite.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$STATE" "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$STATE"
install -m 600 "$TOKEN_SOURCE" "$STATE/offsite.token"
install -m 644 "$HERE/offsite_pull.py" "$STATE/offsite_pull.py"
# Refuses an unreadable or wrong token before anything is scheduled.
/usr/bin/python3 -I -c "import sys; sys.path.insert(0, sys.argv[1]); import offsite_pull; offsite_pull.read_token(__import__('pathlib').Path(sys.argv[2]))" \
  "$STATE" "$STATE/offsite.token"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string><string>-I</string><string>$STATE/offsite_pull.py</string>
    <string>--origin</string><string>$ORIGIN</string>
    <string>--token-file</string><string>$STATE/offsite.token</string>
    <string>--dest</string><string>$DEST</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL: pulls into $DEST at 12:30 and 20:30; log: $LOG"
echo "pull now:  launchctl kickstart gui/$(id -u)/$LABEL"
