#!/bin/sh
# Start the Firestore + Auth emulators (the UI is enabled in firebase.json).
# Data is imported from /data/export if a previous export exists and exported
# back on a clean stop (SIGTERM/SIGINT). The export replaces its target
# directory, so it must be a subdirectory of the /data mount, not the mount.
set -eu
PROJECT="${FIREBASE_PROJECT_ID:-demo-omi-local}"
case "$PROJECT" in demo-*) ;; *) echo "FIREBASE_PROJECT_ID must start with demo- (got $PROJECT)" >&2; exit 1 ;; esac

set -- emulators:start --config /srv/firebase.json --only firestore,auth \
  --project "$PROJECT" --export-on-exit /data/export
if [ -f /data/export/firebase-export-metadata.json ]; then
  set -- "$@" --import /data/export
  echo "importing previous emulator data from /data/export"
else
  echo "no previous export in /data/export; starting empty"
fi
exec firebase "$@"
