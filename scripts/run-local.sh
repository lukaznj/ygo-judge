#!/bin/sh
# Runs the judge in Apple's `container` and exposes it through Cloudflare, then prints the URL to
# add as a Claude connector. Usage: scripts/run-local.sh [--build]
#
# By default this is a quick tunnel: a random https://*.trycloudflare.com address, new on every run.
# For a fixed address, create a Cloudflare tunnel whose public hostname points at the "origin"
# printed below, save its token in state/tunnel_token and its address (e.g.
# https://ygo-judge.example.com) in state/tunnel_url.
set -eu
cd "$(dirname "$0")/.."
mkdir -p state
container system status >/dev/null 2>&1 || container system start --enable-kernel-install
if [ "${1:-}" = "--build" ]; then
	container build -t ygo-judge:latest -f Containerfile .
fi

# The container network's gateway is this Mac's address on that network. Publishing the port there
# makes the judge reachable at one fixed address from the Mac and from the tunnel container.
GATEWAY=$(container network inspect default | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["status"]["ipv4Gateway"])')
ORIGIN="http://$GATEWAY:8000"

container rm -f ygo-judge ygo-judge-tunnel >/dev/null 2>&1 || true
container run -d --name ygo-judge -p "$GATEWAY:8000:8000" -v "$PWD/state:/data" ygo-judge:latest >/dev/null
for _ in $(seq 30); do
	curl -fsS -m 2 "$ORIGIN/health" >/dev/null 2>&1 && break
	sleep 1
done

if [ -s state/tunnel_token ] && [ -s state/tunnel_url ]; then
	container run -d --name ygo-judge-tunnel -e TUNNEL_TOKEN="$(cat state/tunnel_token)" \
		cloudflare/cloudflared:latest tunnel --no-autoupdate run >/dev/null
	URL=$(cat state/tunnel_url)
else
	container run -d --name ygo-judge-tunnel cloudflare/cloudflared:latest tunnel --no-autoupdate --url "$ORIGIN" >/dev/null
	URL=""
	for _ in $(seq 60); do
		URL=$(container logs ygo-judge-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1 || true)
		[ -n "$URL" ] && break
		sleep 1
	done
	[ -n "$URL" ] || { echo "The tunnel did not start; see: container logs ygo-judge-tunnel"; exit 1; }
fi
URL=${URL%/}
echo "$URL" > state/public_url
KEY=$(cat state/access_key)  # created by the server on its first start
echo "Origin (this Mac): $ORIGIN"
echo "Local MCP URL:     $ORIGIN/$KEY/mcp"
echo "Claude connector URL: $URL/$KEY/mcp"
