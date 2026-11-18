#!/usr/bin/env bash
# Publish the Android builds in results/app/ for download on the LAN:
# writes results/app/latest.apk (symlink to the newest build) and
# results/app/index.html, which the apk-server container serves and the
# mobile-access vhost exposes at http://<OMI_PUBLIC_HOST>/app/.
# scripts/build_app.sh runs this after every build; run it by hand after
# deleting old builds.
#
# Usage: scripts/publish_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

DIR=results/app
mkdir -p "$DIR"
PORT="${OMI_PUBLIC_PORT:-80}"
HOST="${OMI_PUBLIC_HOST:-localhost}"
if [ "$PORT" = 80 ]; then BASE="http://$HOST/app/"; else BASE="http://$HOST:$PORT/app/"; fi

mapfile -t apks < <(cd "$DIR" && ls -1 */*.apk 2>/dev/null | sort -r)
if [ "${#apks[@]}" -eq 0 ]; then
  echo "no APKs in $DIR (run scripts/build_app.sh)" >&2
  exit 1
fi
latest="${apks[0]}"
ln -sfn "$latest" "$DIR/latest.apk"

info_field() {  # info_field <build dir> <key>
  sed -n "s/^$2=//p" "$DIR/$1/build-info.txt" 2>/dev/null | head -1
}
esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

rows=""
for apk in "${apks[@]}"; do
  d="${apk%%/*}"
  sha=$(info_field "$d" upstream_sha); built=$(info_field "$d" built)
  size=$(du -m "$DIR/$apk" | cut -f1)
  rows+="<li><a href=\"$apk\">$(basename "$apk" | esc)</a><br><small>${built:-$d} · upstream ${sha:0:12} · ${size} MB · <a href=\"$d/build-info.txt\">build info</a></small></li>"$'\n'
done

ld="${latest%%/*}"
cat > "$DIR/index.html" <<EOF
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omi local app</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5}
 .dl{display:block;text-align:center;padding:1rem;margin:1rem 0;background:#222;color:#fff;border-radius:.6rem;text-decoration:none;font-size:1.2rem}
 small{color:#666} li{margin:.6rem 0} code{background:#eee;padding:0 .2rem}
</style></head><body>
<h1>Omi local app (Android)</h1>
<a class="dl" href="latest.apk" download="$(basename "$latest" | esc)">Download latest APK</a>
<p><small>$(info_field "$ld" built) · upstream $(info_field "$ld" upstream_sha | cut -c1-12) ·
API $(info_field "$ld" api_base_url | esc) · package <code>com.friend.ios.dev</code></small></p>
<p>Open the downloaded file and allow installs from this browser if Android asks.
It updates the existing Omi dev app in place (same signing key) and keeps you signed in.
Then sign in with <b>Sign in (local dev)</b>.</p>
<h2>All builds</h2>
<ul>
$rows</ul>
</body></html>
EOF
echo "published $latest"
echo "download: ${BASE}  (direct: ${BASE}latest.apk)"
