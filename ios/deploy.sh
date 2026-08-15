#!/bin/bash
# Build, sign, and install Dromond on a paired iPhone. No Xcode window.
#
#   ./ios/deploy.sh                 # first paired iPhone
#   ./ios/deploy.sh --list          # what is paired
#   ./ios/deploy.sh <device-id>     # a specific one
#
# The team id is read from the Apple Development certificate in the keychain,
# so nothing about the developer account is committed here. Override with
# DROMOND_TEAM=... if the machine holds more than one.
set -euo pipefail

cd "$(dirname "$0")"
DD="${TMPDIR:-/tmp}/dromond-device-build"

devices() { xcrun devicectl list devices 2>/dev/null | grep -i iphone; }

if [ "${1:-}" = "--list" ]; then devices; exit 0; fi

DEVICE="${1:-$(devices | grep -i available | head -1 | grep -oE '[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}')}"
if [ -z "$DEVICE" ]; then
  echo "deploy: no available iPhone. Unlock it, plug it in, or pair it in Xcode." >&2
  echo "deploy: paired devices:" >&2; devices >&2 || true
  exit 1
fi

TEAM="${DROMOND_TEAM:-$(security find-certificate -c "Apple Development" -p 2>/dev/null \
  | openssl x509 -noout -subject 2>/dev/null | grep -oE 'OU=[A-Z0-9]+' | cut -d= -f2)}"
if [ -z "$TEAM" ]; then
  echo "deploy: no Apple Development certificate in the keychain." >&2
  echo "deploy: open Xcode once and sign in, or set DROMOND_TEAM=<team-id>." >&2
  exit 1
fi

echo "deploy: device $DEVICE, team $TEAM"

# -allowProvisioningUpdates lets Xcode mint the profile for com.batteryshark.dromond
# on first run; after that it is cached and this is offline.
xcodebuild -project Dromond.xcodeproj -scheme Dromond -configuration Release \
  -destination "platform=iOS,id=$DEVICE" -derivedDataPath "$DD" \
  DEVELOPMENT_TEAM="$TEAM" -allowProvisioningUpdates build \
  | tail -3

APP="$DD/Build/Products/Release-iphoneos/Dromond.app"
[ -d "$APP" ] || { echo "deploy: build produced no app at $APP" >&2; exit 1; }

xcrun devicectl device install app --device "$DEVICE" "$APP" | grep -E "App installed|bundleID"

# A locked phone installs fine but refuses to launch, which is not a failure.
xcrun devicectl device process launch --device "$DEVICE" com.batteryshark.dromond 2>/dev/null \
  | grep -qE "launched|running" \
  && echo "deploy: launched" \
  || echo "deploy: installed. Unlock the phone and tap Dromond."
