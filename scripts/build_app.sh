#!/usr/bin/env bash
# Build the Omi Android app (upstream/omi/app, "dev" flavor, local_dev profile)
# in Docker, pointed at this server: API + Firebase Auth emulator both behind
# the nginx vhost from deploy/nginx/ (see docs/mobile-access.md).
#
# Usage: scripts/build_app.sh
# Settings come from .env ("Mobile access" in .env.example):
#   OMI_PUBLIC_HOST (required), OMI_PUBLIC_PORT (80), OMI_APP_API_BASE_URL,
#   OMI_APP_AUTH_EMULATOR_HOST, OMI_APP_AUTH_EMULATOR_PORT,
#   OMI_APP_BUILD_MODE (release|profile|debug, default release),
#   OMI_APP_TARGET_PLATFORMS (default android-arm64; comma list, "" = all ABIs)
#
# Upstream code is never modified: the app is exported from the submodule's
# HEAD into .cache/app-build/src/ and configured there the way upstream's
# app/setup.sh does for `setup.sh android` (local emulator Firebase configs,
# .dev.env, prebuilt debug keystore), then built with `flutter build apk`.
# Output: results/app/<timestamp>/omi-dev-<sha>.apk (+ build-info.txt).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
[ -f .env ] && set -a && . ./.env && set +a

: "${OMI_PUBLIC_HOST:?set OMI_PUBLIC_HOST in .env (private IP the phone uses, e.g. 192.168.5.53)}"
PORT="${OMI_PUBLIC_PORT:-80}"
if [ "$PORT" = 80 ]; then default_url="http://${OMI_PUBLIC_HOST}/"; else default_url="http://${OMI_PUBLIC_HOST}:${PORT}/"; fi
API_URL="${OMI_APP_API_BASE_URL:-$default_url}"
AUTH_HOST="${OMI_APP_AUTH_EMULATOR_HOST:-$OMI_PUBLIC_HOST}"
AUTH_PORT="${OMI_APP_AUTH_EMULATOR_PORT:-$PORT}"
MODE="${OMI_APP_BUILD_MODE:-release}"
PLATFORMS="${OMI_APP_TARGET_PLATFORMS-android-arm64}"
IMAGE=omi-local-app-builder:latest

case "$API_URL" in */) ;; *) echo "OMI_APP_API_BASE_URL must end with '/' (upstream requirement)" >&2; exit 1 ;; esac
case "$MODE" in release|profile|debug) ;; *) echo "OMI_APP_BUILD_MODE must be release, profile or debug" >&2; exit 1 ;; esac

[ -f upstream/omi/app/pubspec.yaml ] || { echo "upstream/omi is missing: git submodule update --init" >&2; exit 1; }
SHA=$(git -C upstream/omi rev-parse HEAD)

echo "== builder image"
docker build -q --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -t "$IMAGE" scripts/build_app >/dev/null

BUILD="$ROOT/.cache/app-build"
SRC="$BUILD/src"
mkdir -p "$BUILD/pub-cache" "$BUILD/gradle"
echo "== export upstream app @ ${SHA:0:12} to $SRC"
rm -rf "$SRC"
mkdir -p "$SRC"
git -C upstream/omi archive --format=tar HEAD app | tar -x -C "$SRC"
APP="$SRC/app"

# --- What `setup.sh android` (dev) does, minus building/running ---
# setup_app_env local_dev: the dev flavor reads .dev.env (envied); the prod
# env class still needs its .env file to exist for code generation.
cat > "$APP/.dev.env" <<EOF
API_BASE_URL=$API_URL
USE_WEB_AUTH=true
USE_AUTH_CUSTOM_TOKEN=true
EOF
: > "$APP/.env"
# setup_keystore_android: well-known debug keystore shipped by upstream.
cp "$APP/setup/prebuilt/key.properties" "$APP/android/"
# setup_firebase: demo-omi-local emulator configs (fake, non-secret).
mkdir -p "$APP/android/app/src/dev" "$APP/android/app/src/prod"
cp "$APP/lib/firebase_options_local.dart" "$APP/lib/firebase_options_dev.dart"
cp "$APP/lib/firebase_options_local.dart" "$APP/lib/firebase_options_prod.dart"
cp "$APP/setup/prebuilt/google-services-local.json" "$APP/android/app/src/dev/google-services.json"
cp "$APP/setup/prebuilt/google-services-local.json" "$APP/android/app/src/prod/google-services.json"
printf 'flutter.sdk=/opt/flutter\nsdk.dir=/opt/android-sdk\n' > "$APP/android/local.properties"

DEFINES=(
  "--dart-define=OMI_APP_PROFILE=local_dev"
  "--dart-define=OMI_API_BASE_URL=$API_URL"
  "--dart-define=OMI_FIREBASE_AUTH_EMULATOR_HOST=$AUTH_HOST"
  "--dart-define=OMI_FIREBASE_AUTH_EMULATOR_PORT=$AUTH_PORT"
  "--dart-define=OMI_GIT_SHA=$SHA"
  "--dart-define=OMI_GIT_DIRTY=false"
  "--dart-define=OMI_BUILD_NUMBER=local"
)
BUILD_ARGS=(apk --flavor dev "--$MODE" "${DEFINES[@]}")
[ -n "$PLATFORMS" ] && BUILD_ARGS+=(--target-platform "$PLATFORMS")

echo "== flutter build (API $API_URL, Auth emulator $AUTH_HOST:$AUTH_PORT, $MODE, ${PLATFORMS:-all ABIs})"
docker run --rm \
  -v "$SRC:/src" \
  -v "$BUILD/pub-cache:/home/builder/.pub-cache" \
  -v "$BUILD/gradle:/home/builder/.gradle" \
  -w /src/app \
  "$IMAGE" bash -euo pipefail -c '
    scripts/validate_mobile_build_config.sh --flavor dev --profile local_dev
    flutter pub get
    dart run build_runner build --delete-conflicting-outputs
    flutter build "$@"
  ' _ "${BUILD_ARGS[@]}"

APK="$APP/build/app/outputs/flutter-apk/app-dev-$MODE.apk"
[ -f "$APK" ] || { echo "APK not found at $APK" >&2; ls "$APP/build/app/outputs/flutter-apk/" >&2 || true; exit 1; }
OUT="$ROOT/results/app/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
cp "$APK" "$OUT/omi-dev-${SHA:0:12}-$MODE.apk"
cat > "$OUT/build-info.txt" <<EOF
upstream_sha=$SHA
flavor=dev profile=local_dev mode=$MODE platforms=${PLATFORMS:-all}
package=com.friend.ios.dev (installs next to the store app)
api_base_url=$API_URL
auth_emulator=$AUTH_HOST:$AUTH_PORT
built=$(date -Is)
EOF
echo "== done: $OUT/omi-dev-${SHA:0:12}-$MODE.apk"
echo "Install: adb install -r <apk>, or copy it to the phone and open it (allow unknown sources)."
