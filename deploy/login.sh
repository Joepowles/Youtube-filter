#!/usr/bin/env bash
# One-time YouTube sign-in on a server with no screen.
# Opens Chrome on a virtual screen you can see from your own browser.
#
# The website's "Sign in" button is easier; this is the fallback.
#
#   1. On your computer:  ssh -L 6080:localhost:6080 root@<this-server>
#   2. In that ssh session:  /opt/yt-filter/login.sh <account-id>   (e.g. kid1; see data/accounts.json)
#   3. On your computer, open http://localhost:6080/vnc.html and click Connect.
#   4. Sign in to your child's account in the Chrome window, then press Enter in the ssh session.
set -euo pipefail
APP=/opt/yt-filter
DISPLAY_NUM=:99
ACCOUNT="${1:?Usage: login.sh <account-id>   (ids are in /opt/yt-filter/data/accounts.json)}"
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

systemctl stop yt-filter 2>/dev/null || true   # the profile can only be open once
cleanup() { kill $(jobs -p) 2>/dev/null || true; systemctl start yt-filter 2>/dev/null || true; }
trap cleanup EXIT

runuser -u ytfilter -- Xvfb $DISPLAY_NUM -screen 0 1280x900x24 &
sleep 1
runuser -u ytfilter -- x11vnc -display $DISPLAY_NUM -localhost -nopw -forever -quiet &
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/dev/null 2>&1 &
sleep 1
echo "Open http://localhost:6080/vnc.html on your computer (through the ssh tunnel) and click Connect."
cd "$APP"
runuser -u ytfilter -- env DISPLAY=$DISPLAY_NUM "$APP/.venv/bin/python" yt_channel_filter.py --profile "$APP/data/$ACCOUNT/profile" login
