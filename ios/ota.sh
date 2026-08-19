#!/bin/bash
# Install Dromond on a phone that is on the tailnet but not on this network.
#
#   ./ios/ota.sh          # build, publish, print the link
#   ./ios/ota.sh --off    # stop serving and remove the tailnet path
#
# Apple's own wireless install needs mDNS on the local link, which Tailscale
# does not carry — so `devicectl` cannot reach a phone in another state. This
# goes the other way: iOS will install any signed build from an HTTPS URL it
# trusts, and `tailscale serve` provides exactly that on the tailnet, with a
# real certificate. The phone's UDID is already in the development profile
# from the first cabled install, which is what makes the build installable.
set -euo pipefail

cd "$(dirname "$0")"
TEAM="${DROMOND_TEAM:-$(security find-certificate -c "Apple Development" -p 2>/dev/null \
  | openssl x509 -noout -subject 2>/dev/null | grep -oE 'OU=[A-Z0-9]+' | cut -d= -f2)}"
PORT="${DROMOND_OTA_PORT:-8791}"
# Every app installs from one namespace, one app per leaf, so publishing one
# never unmounts another and the tailnet has a single obvious place to look.
WEBPATH="${DROMOND_OTA_PATH:-/ios-installer/dromond}"
OUT="${TMPDIR:-/tmp}/dromond-ota"
HOST="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

if [ "${1:-}" = "--off" ]; then
  tailscale serve --set-path "$WEBPATH" off 2>/dev/null || true
  pkill -f "http.server $PORT" 2>/dev/null || true
  echo "ota: stopped serving; the tailnet path is removed"
  exit 0
fi

[ -n "$HOST" ] || { echo "ota: tailscale is not reporting a hostname" >&2; exit 1; }
[ -n "$TEAM" ] || { echo "ota: no Apple Development certificate; set DROMOND_TEAM" >&2; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT/serve"
echo "ota: archiving…"
xcodebuild -project Dromond.xcodeproj -scheme Dromond -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$OUT/Dromond.xcarchive" \
  DEVELOPMENT_TEAM="$TEAM" -allowProvisioningUpdates archive | tail -1

cat > "$OUT/export.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>debugging</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>automatic</string>
  <key>thinning</key><string>&lt;none&gt;</string>
</dict></plist>
PLIST

echo "ota: exporting…"
xcodebuild -exportArchive -archivePath "$OUT/Dromond.xcarchive" \
  -exportOptionsPlist "$OUT/export.plist" -exportPath "$OUT/export" \
  -allowProvisioningUpdates | tail -1
cp "$OUT/export/Dromond.ipa" "$OUT/serve/"

APP="$OUT/Dromond.xcarchive/Products/Applications/Dromond.app"
VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Info.plist")
BUILD=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$APP/Info.plist")

cat > "$OUT/serve/manifest.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>items</key><array><dict>
  <key>assets</key><array><dict>
    <key>kind</key><string>software-package</string>
    <key>url</key><string>https://$HOST$WEBPATH/Dromond.ipa</string>
  </dict></array>
  <key>metadata</key><dict>
    <key>bundle-identifier</key><string>com.batteryshark.dromond</string>
    <key>bundle-version</key><string>$VER</string>
    <key>kind</key><string>software</string>
    <key>title</key><string>Dromond</string>
  </dict>
</dict></array></dict></plist>
PLIST
plutil -lint "$OUT/serve/manifest.plist" > /dev/null

cat > "$OUT/serve/index.html" <<HTML
<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install Dromond</title>
<style>body{font:17px -apple-system,system-ui;margin:0;min-height:100vh;display:grid;
place-items:center;background:#161B23;color:#DFE3EA}
a{display:block;padding:18px 34px;background:#61A5E8;color:#0b0f14;text-decoration:none;
border-radius:14px;font-weight:700}
p{color:#8b95a5;font-size:14px;text-align:center;max-width:22rem;line-height:1.5}</style>
<div style="display:grid;gap:22px;justify-items:center">
<svg width="76" height="76" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#161B23"/>
<path transform="translate(78,96)" fill="#4FB3C4" d="M0,10 L296,0 C238,40 176,74 100,82 C58,86 16,54 0,10 Z"/>
<path transform="translate(118,214)" fill="#61A5E8" d="M0,10 L296,0 C238,40 176,74 100,82 C58,86 16,54 0,10 Z"/>
<path transform="translate(78,332)" fill="#4FB3C4" d="M0,10 L296,0 C238,40 176,74 100,82 C58,86 16,54 0,10 Z"/></svg>
<a href="itms-services://?action=download-manifest&url=https://$HOST$WEBPATH/manifest.plist">Install Dromond $VER ($BUILD)</a>
<p>Tap install, then find Dromond on the home screen. The phone has to be on the tailnet.</p>
</div>
HTML

pkill -f "http.server $PORT" 2>/dev/null || true
( cd "$OUT/serve" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 & )
sleep 2
# A PATH mapping, never the root: whatever is already served at / stays there.
tailscale serve --bg --set-path "$WEBPATH" "http://127.0.0.1:$PORT" >/dev/null

echo
echo "  Open this on the phone:  https://$HOST$WEBPATH/"
echo "  Dromond $VER ($BUILD) · $(du -h "$OUT/serve/Dromond.ipa" | cut -f1)"
echo "  Stop serving with:       ./ios/ota.sh --off"
