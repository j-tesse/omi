#!/usr/bin/env bash
# Check the mobile-access nginx vhost (deploy/nginx/omi.conf.template) without
# touching the shared webserver_nginx container.
#
# Usage: scripts/test_nginx_conf.sh [--live]
#   1. `nginx -t` on the rendered vhost alone (throwaway nginx:alpine).
#   2. `nginx -t` on a temporary copy of the real webserver config with the
#      vhost added (WEBSERVER_DIR, default ~/src/webserver; skipped if absent).
#   --live: also starts a throwaway nginx on the omi_default network (upstreams
#      backend:8080 and firebase:8104) and walks the app's flow through it:
#      local-dev custom token → Auth emulator sign-in + token refresh →
#      authenticated REST call → /v4/listen websocket upgrade, plus the
#      /emulator/ block and the IP allowlist. Needs backend + firebase running.
#      Creates (and deletes) the emulator user "smoke-mobile-access".
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1
WEBSERVER_DIR="${WEBSERVER_DIR:-$HOME/src/webserver}"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:alpine}"
export OMI_PUBLIC_HOST="${OMI_PUBLIC_HOST:-192.168.5.53}"
TMP=$(mktemp -d)
chmod 700 "$TMP"
CTR=""
cleanup() { [ -n "$CTR" ] && docker rm -f "$CTR" >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT

echo "== nginx -t: vhost alone"
scripts/render_nginx_conf.sh -o "$TMP/omi.conf" 2>/dev/null
docker run --rm --add-host host.docker.internal:host-gateway \
  -v "$TMP/omi.conf:/etc/nginx/conf.d/omi.conf:ro" --entrypoint nginx "$NGINX_IMAGE" -t 2>&1 | grep -v "listen \.\.\. http2. directive is deprecated"

if [ -f "$WEBSERVER_DIR/nginx/nginx.conf" ]; then
  echo "== nginx -t: with the webserver config ($WEBSERVER_DIR, temporary copy)"
  cp -a "$WEBSERVER_DIR/nginx/conf.d" "$TMP/conf.d"
  cp "$TMP/omi.conf" "$TMP/conf.d/omi.conf"
  # Same mounts as webserver/docker-compose.yml, all read-only, conf.d swapped.
  docker run --rm --add-host host.docker.internal:host-gateway \
    -v "$WEBSERVER_DIR/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
    -v "$WEBSERVER_DIR/nginx/njs:/etc/nginx/njs:ro" \
    -v "$TMP/conf.d:/etc/nginx/conf.d:ro" \
    -v "$WEBSERVER_DIR/nginx/certs:/etc/nginx/certs:ro" \
    -v /etc/letsencrypt:/etc/letsencrypt:ro \
    --tmpfs /var/log/nginx \
    --entrypoint nginx "$NGINX_IMAGE" -t 2>&1 | grep -v "listen \.\.\. http2. directive is deprecated"
else
  echo "== skip webserver check ($WEBSERVER_DIR/nginx/nginx.conf not found)"
fi

[ "$LIVE" = 1 ] || exit 0

echo "== live: throwaway nginx on omi_default"
NET=omi_default
GW=$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Gateway}}')
PORT="${TEST_PORT:-18180}"
# Host-loopback clients reach the container from the network gateway
# (docker-proxy), so allow only that address; the in-container check below
# (source 127.0.0.1) must then be denied.
OMI_ALLOWED_CIDRS="$GW" OMI_BACKEND_UPSTREAM=backend:8080 OMI_AUTH_UPSTREAM=firebase:8104 \
  scripts/render_nginx_conf.sh -o "$TMP/live.conf" 2>/dev/null
chmod 644 "$TMP/live.conf"
CTR=$(docker run -d --network "$NET" -p "127.0.0.1:$PORT:80" \
  -v "$TMP/live.conf:/etc/nginx/conf.d/omi.conf:ro" "$NGINX_IMAGE")
sleep 1
BASE="http://127.0.0.1:$PORT"
H=(-H "Host: $OMI_PUBLIC_HOST")
UID_="smoke-mobile-access"
field() { python3 -c 'import json,sys; print(json.load(sys.stdin)'"$1"')'; }
fail() { echo "FAIL: $*" >&2; docker logs "$CTR" 2>&1 | tail -5 >&2; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" "$BASE/")
[ "$code" != 502 ] && [ "$code" != 000 ] || fail "backend unreachable through nginx (HTTP $code)"
echo "backend reachable (GET / → $code)"

custom=$(curl -fsS "${H[@]}" -X POST "$BASE/v1/auth/local-dev/custom-token" -d "uid=$UID_" | field '["custom_token"]') \
  || fail "local-dev custom token"
echo "custom token: ok"

IDT="$BASE/identitytoolkit.googleapis.com/v1"
resp=$(curl -fsS "${H[@]}" "$IDT/accounts:signInWithCustomToken?key=fake" -H 'Content-Type: application/json' \
  -d "{\"token\":\"$custom\",\"returnSecureToken\":true}") || fail "signInWithCustomToken via nginx"
idtok=$(echo "$resp" | field '["idToken"]')
refresh=$(echo "$resp" | field '["refreshToken"]')
echo "auth emulator sign-in: ok"

idtok2=$(curl -fsS "${H[@]}" "$BASE/securetoken.googleapis.com/v1/token?key=fake" \
  -d "grant_type=refresh_token&refresh_token=$refresh" | field '["id_token"]') || fail "token refresh via nginx"
[ -n "$idtok2" ] && echo "token refresh: ok"

code=$(curl -s -o "$TMP/profile.json" -w '%{http_code}' "${H[@]}" -H "Authorization: Bearer $idtok" "$BASE/v1/users/profile")
[ "$code" = 200 ] || fail "GET /v1/users/profile → $code $(head -c 200 "$TMP/profile.json")"
echo "authenticated REST (GET /v1/users/profile): 200"

status=$(curl -s -i -N --http1.1 --max-time 5 "${H[@]}" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H "Authorization: Bearer $idtok" \
  "$BASE/v4/listen?uid=$UID_&language=en&sample_rate=16000&codec=pcm16&channels=1&source=phone" 2>/dev/null | head -1 | tr -d '\r') || true
case "$status" in *" 101"*) echo "websocket /v4/listen: $status" ;; *) fail "websocket upgrade: '${status:-no response}'" ;; esac

code=$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" "$BASE/emulator/v1/projects/${FIREBASE_PROJECT_ID:-demo-omi-local}/config")
[ "$code" = 403 ] || fail "/emulator/ should be blocked, got $code"
echo "/emulator/ blocked: 403"

denied=$(docker exec "$CTR" wget -q -O /dev/null -S --header "Host: $OMI_PUBLIC_HOST" http://127.0.0.1/ 2>&1 | awk '/HTTP\//{print $2; exit}') || true
[ "$denied" = 403 ] || fail "client outside OMI_ALLOWED_CIDRS should get 403, got '${denied:-?}'"
echo "allowlist: non-listed client → 403"

# Clean up the smoke user (emulator admin API, directly; "Bearer owner" = admin).
curl -fsS -X POST "http://127.0.0.1:8104/identitytoolkit.googleapis.com/v1/projects/${FIREBASE_PROJECT_ID:-demo-omi-local}/accounts:delete" \
  -H 'Authorization: Bearer owner' -H 'Content-Type: application/json' -d "{\"localId\":\"$UID_\"}" >/dev/null \
  && echo "smoke user deleted"
echo "== live: all ok"
