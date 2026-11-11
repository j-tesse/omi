#!/usr/bin/env bash
# Render deploy/nginx/omi.conf.template with the "Mobile access" settings from
# .env and print it to stdout (or write it to -o FILE).
# Usage: scripts/render_nginx_conf.sh [-o FILE]
# Install: docs/mobile-access.md ("Install the nginx vhost").
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
OUT=-
if [ "${1:-}" = "-o" ]; then OUT="${2:?-o needs a file}"; fi

: "${OMI_PUBLIC_HOST:?set OMI_PUBLIC_HOST in .env (private IP the phone uses, e.g. 192.168.5.53)}"
# No 172.16.0.0/12 by default: docker bridges live there, and the host's
# cloudflared reaches nginx from one (172.18.0.1).
OMI_ALLOWED_CIDRS="${OMI_ALLOWED_CIDRS:-192.168.0.0/16 10.0.0.0/8 100.64.0.0/10}"
OMI_BACKEND_UPSTREAM="${OMI_BACKEND_UPSTREAM:-host.docker.internal:8109}"
OMI_AUTH_UPSTREAM="${OMI_AUTH_UPSTREAM:-host.docker.internal:8104}"
OMI_MAX_BODY_SIZE="${OMI_MAX_BODY_SIZE:-500m}"

# The app rejects anything but a private IP literal or a *.ts.net name.
case "$OMI_PUBLIC_HOST" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|*.ts.net) ;;
  100.*) o2=$(echo "$OMI_PUBLIC_HOST" | cut -d. -f2)
         [ "$o2" -ge 64 ] && [ "$o2" -le 127 ] || { echo "OMI_PUBLIC_HOST $OMI_PUBLIC_HOST is not in 100.64.0.0/10" >&2; exit 1; } ;;
  *) echo "OMI_PUBLIC_HOST must be a private IP (LAN/Tailscale) or *.ts.net name; the app's local_dev profile refuses anything else" >&2
     exit 1 ;;
esac

allow_lines=""
for cidr in $OMI_ALLOWED_CIDRS; do
  [[ "$cidr" =~ ^[0-9a-fA-F.:/]+$ ]] || { echo "bad CIDR in OMI_ALLOWED_CIDRS: $cidr" >&2; exit 1; }
  allow_lines+="    allow ${cidr};"$'\n'
done
export OMI_PUBLIC_HOST OMI_BACKEND_UPSTREAM OMI_AUTH_UPSTREAM OMI_MAX_BODY_SIZE
export OMI_ALLOWED_CIDRS="${allow_lines%$'\n'}"

# Only substitute our variables; nginx's own $vars stay untouched.
vars='${OMI_PUBLIC_HOST} ${OMI_ALLOWED_CIDRS} ${OMI_BACKEND_UPSTREAM} ${OMI_AUTH_UPSTREAM} ${OMI_MAX_BODY_SIZE}'
if [ "$OUT" = - ]; then
  envsubst "$vars" < deploy/nginx/omi.conf.template
else
  envsubst "$vars" < deploy/nginx/omi.conf.template > "$OUT"
  echo "wrote $OUT" >&2
fi
