#!/usr/bin/env bash
# Installs the YouTube channel filter web app on Debian 12 (run as root inside
# a Proxmox LXC or VM). Safe to re-run to update the app.
set -euo pipefail
APP=/opt/yt-filter
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
[ "$(dpkg --print-architecture)" = amd64 ] || echo "Warning: Google Chrome needs amd64; will fall back to Chromium."

echo "== Packages"
apt-get update
apt-get install -y python3 python3-venv curl gnupg ca-certificates xvfb x11vnc novnc websockify fonts-noto-color-emoji

if ! command -v google-chrome >/dev/null && [ "$(dpkg --print-architecture)" = amd64 ]; then
  echo "== Google Chrome"
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb
fi

if ! command -v cloudflared >/dev/null; then
  echo "== cloudflared"
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update && apt-get install -y cloudflared
fi

echo "== App files"
id ytfilter >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/ytfilter --shell /bin/bash ytfilter
mkdir -p "$APP/static"
install -m 0644 "$SRC/yt_channel_filter.py" "$SRC/webapp.py" "$SRC/requirements.txt" "$APP/"
install -m 0644 "$SRC/static/index.html" "$APP/static/"
install -m 0755 "$SRC/deploy/login.sh" "$APP/"
NEW_ENV=0
[ -f "$APP/.env" ] || { install -m 0600 "$SRC/.env.example" "$APP/.env"; NEW_ENV=1; }
chown -R ytfilter:ytfilter "$APP"

echo "== Python environment"
sudo_u() { runuser -u ytfilter -- "$@"; }
[ -d "$APP/.venv" ] || sudo_u python3 -m venv "$APP/.venv"
sudo_u "$APP/.venv/bin/pip" install -q --upgrade pip
sudo_u "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
"$APP/.venv/bin/python" -m playwright install-deps chromium
sudo_u "$APP/.venv/bin/python" -m playwright install chromium

echo "== Service"
install -m 0644 "$SRC/deploy/yt-filter.service" /etc/systemd/system/yt-filter.service
if [ -d "$SRC/.git" ]; then
  # Auto-update: pull new commits from GitHub every 5 minutes and reinstall.
  sed "s#__REPO__#$SRC#" "$SRC/deploy/yt-filter-update.service" > /etc/systemd/system/yt-filter-update.service
  install -m 0644 "$SRC/deploy/yt-filter-update.timer" /etc/systemd/system/yt-filter-update.timer
fi
systemctl daemon-reload
systemctl enable yt-filter >/dev/null
[ -d "$SRC/.git" ] && systemctl enable --now yt-filter-update.timer >/dev/null

if [ "$NEW_ENV" = 1 ]; then
  echo
  echo "Installed. Next: edit $APP/.env (AUTH_MODE and APP_PASSWORD, see WEBSITE.md), then"
  echo "  $APP/login.sh            # sign in to your child's YouTube account once"
  echo "  systemctl restart yt-filter"
else
  systemctl restart yt-filter
  echo "Installed and restarted. Status: systemctl status yt-filter"
fi
