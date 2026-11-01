#!/usr/bin/env bash
# Creates the Cloudflare Tunnel for the site from the command line and installs
# it as a service. Run as root in the container after install.sh.
#
#   HOSTNAME_FQDN=yt.example.com bash /root/yt/deploy/tunnel.sh
set -euo pipefail
NAME="${TUNNEL_NAME:-yt-filter}"
HOST="${HOSTNAME_FQDN:?Set HOSTNAME_FQDN to the address for the site, e.g. HOSTNAME_FQDN=yt.example.com}"
PORT="${PORT:-8765}"
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
command -v cloudflared >/dev/null || { echo "cloudflared missing; run install.sh first"; exit 1; }

if [ ! -f /root/.cloudflared/cert.pem ]; then
  echo "== Authorise cloudflared: open the link below, sign in to Cloudflare, and pick ${HOST#*.}"
  cloudflared tunnel login
fi

if ! cloudflared tunnel info "$NAME" >/dev/null 2>&1; then
  cloudflared tunnel create "$NAME"
fi
ID=$(cloudflared tunnel list --name "$NAME" --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
echo "Tunnel $NAME = $ID"

cloudflared tunnel route dns --overwrite-dns "$NAME" "$HOST"

mkdir -p /etc/cloudflared
cp "/root/.cloudflared/$ID.json" /etc/cloudflared/
chmod 600 "/etc/cloudflared/$ID.json"
cat > /etc/cloudflared/config.yml <<EOF
tunnel: $ID
credentials-file: /etc/cloudflared/$ID.json
ingress:
  - hostname: $HOST
    service: http://127.0.0.1:$PORT
  - service: http_status:404
EOF

if systemctl list-unit-files | grep -q '^cloudflared.service'; then
  systemctl restart cloudflared
else
  cloudflared service install
fi
systemctl --no-pager --lines=0 status cloudflared || true
echo
echo "Tunnel is up: https://$HOST -> localhost:$PORT"
echo "Before using the site, add the Cloudflare Access application (WEBSITE.md step 4)."
