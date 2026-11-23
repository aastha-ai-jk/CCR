#!/bin/bash
# CCR advisor kiosk — Linux edge device (Ubuntu/Debian/Raspberry Pi OS, x86 or ARM).
# Display only: opens the advisor from the GPU server full-screen and keeps it there.
#
# Run ONCE as the kiosk user (the account that auto-logs-in to the desktop):
#   bash setup-kiosk.sh                                   # advisor on the GPU box
#   bash setup-kiosk.sh http://10.53.2.10:8010/advisor
#   bash setup-kiosk.sh --remove
# Needs a graphical session (X11 or Wayland desktop with auto-login).
# Installs chromium + unclutter with sudo if missing (or ask IT to install them).
set -euo pipefail
URL="${1:-http://10.53.2.10:8010/advisor}"
UNIT="$HOME/.config/systemd/user/ccr-kiosk.service"

if [ "$URL" = "--remove" ]; then
  systemctl --user disable --now ccr-kiosk.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl --user daemon-reload
  echo "Kiosk removed."; exit 0
fi

# 1. reachability
HOSTPORT="$(echo "$URL" | sed -E 's#^https?://([^/]+).*#\1#')"
HOST="${HOSTPORT%%:*}"; PORT="${HOSTPORT##*:}"; [ "$PORT" = "$HOST" ] && PORT=80
if timeout 5 bash -c "</dev/tcp/$HOST/$PORT" 2>/dev/null; then
  echo "Reach $HOST:$PORT -> OK"
else
  echo "Reach $HOST:$PORT -> NOT REACHABLE - ask IT to open this port from the CCR network"
fi

# 2. browser
BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  sudo apt-get update && (sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser)
  BROWSER="$(command -v chromium || command -v chromium-browser)"
fi
command -v unclutter >/dev/null || sudo apt-get install -y unclutter || true

# 3. systemd user service: start with the session, relaunch if closed/crashed
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<EOF
[Unit]
Description=CCR advisor kiosk
After=graphical-session.target

[Service]
Environment=DISPLAY=:0
ExecStartPre=-/usr/bin/xset s off
ExecStartPre=-/usr/bin/xset -dpms
ExecStartPre=-/usr/bin/xset s noblank
ExecStartPre=-/bin/sh -c 'pgrep unclutter >/dev/null || (unclutter -idle 3 &)'
ExecStart=$BROWSER --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble --check-for-update-interval=31536000 --user-data-dir=%h/.ccr-kiosk $URL
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now ccr-kiosk.service
loginctl enable-linger "$USER" 2>/dev/null || true
echo "Kiosk running (systemctl --user status ccr-kiosk). Remove with: bash setup-kiosk.sh --remove"
