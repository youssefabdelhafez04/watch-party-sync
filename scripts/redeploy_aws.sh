#!/usr/bin/env bash
set -euo pipefail

REQUESTED_APP_DOMAIN="${APP_DOMAIN:-}"
[[ -f .aws-stack.env ]] && source .aws-stack.env
APP_DOMAIN="${REQUESTED_APP_DOMAIN:-${DEFAULT_APP_DOMAIN:-}}"
: "${EC2_HOST:?Set EC2_HOST or run scripts/aws_bootstrap.sh}"
: "${CLOUDFRONT_DOMAIN:?Set CLOUDFRONT_DOMAIN or run scripts/aws_bootstrap.sh}"
: "${APP_DOMAIN:?Set APP_DOMAIN to the DNS name pointing at the EC2 IP}"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/watchparty-ohio.pem}"
SSH_USER="${SSH_USER:-ec2-user}"
[[ -f "$SSH_KEY" ]] || { echo "SSH key not found: $SSH_KEY" >&2; exit 1; }
[[ "$APP_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "Invalid APP_DOMAIN" >&2; exit 1; }
[[ "$CLOUDFRONT_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "Invalid CLOUDFRONT_DOMAIN" >&2; exit 1; }

ARCHIVE="$(mktemp -t watchparty.XXXXXX.tar.gz)"
trap 'rm -f "$ARCHIVE"' EXIT
COPYFILE_DISABLE=1 tar --no-xattrs \
  --exclude='.git' --exclude='.venv' --exclude='.pytest_cache' --exclude='__pycache__' \
  --exclude='results' --exclude='.aws-stack.env' \
  -czf "$ARCHIVE" .

SSH=(ssh -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" "$SSH_USER@$EC2_HOST")
SCP=(scp -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")
"${SCP[@]}" "$ARCHIVE" "$SSH_USER@$EC2_HOST:/tmp/watchparty.tar.gz"

"${SSH[@]}" \
  "APP_DOMAIN='$APP_DOMAIN' CLOUDFRONT_DOMAIN='$CLOUDFRONT_DOMAIN' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p /opt/watchparty
tar -xzf /tmp/watchparty.tar.gz -C /opt/watchparty
rm -f /tmp/watchparty.tar.gz
cd /opt/watchparty

docker build --build-arg INSTALL_FFMPEG=0 -t watchparty:latest .
docker network inspect watchparty >/dev/null 2>&1 || docker network create watchparty
docker rm -f watchparty-app watchparty-caddy >/dev/null 2>&1 || true
docker run -d --name watchparty-app --restart unless-stopped --network watchparty \
  --network-alias app \
  -e WATCHPARTY_DISABLE_TRANSCODING=1 \
  -e WATCHPARTY_BLOCK_PRIVATE_URLS=1 \
  -e WATCHPARTY_TELEMETRY_DB=/app/data/telemetry.sqlite3 \
  -e "WATCHPARTY_PUBLIC_URL=https://$APP_DOMAIN" \
  -e "WATCHPARTY_MEDIA_BASE_URL=https://$CLOUDFRONT_DOMAIN/media" \
  -v watchparty_telemetry:/app/data \
  watchparty:latest
docker run -d --name watchparty-caddy --restart unless-stopped --network watchparty \
  -e "APP_DOMAIN=$APP_DOMAIN" \
  -p 80:80 -p 443:443 \
  -v /opt/watchparty/deploy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v watchparty_caddy_data:/data -v watchparty_caddy_config:/config \
  caddy:2-alpine

for attempt in {1..30}; do
  if docker exec watchparty-app python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && exit 1
  sleep 2
done
REMOTE

echo "Redeployed https://$APP_DOMAIN"
