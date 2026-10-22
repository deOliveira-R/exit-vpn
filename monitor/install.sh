#!/bin/sh
# Installs the connection monitor as a launchd agent of this user (starts now and at login,
# restarted if it exits); `install.sh --remove` stops and removes it. Logs: ~/Library/Logs/exit-vpn.
set -eu
LABEL=org.exit-vpn.connection-monitor
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-/opt/homebrew/bin/python3}
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = --remove ]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi
mkdir -p "$HOME/Library/Logs/exit-vpn"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$HERE/connection_monitor.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/exit-vpn/connection-monitor.err</string>
</dict>
</plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; logs in $HOME/Library/Logs/exit-vpn"
