# Orchestra for iOS

The iOS app is a small mirror of Orchestra's run list and normalized trace
stream. It can stop a live run or send it a `tell`; push notifications,
decisions, and Work record editing deliberately remain outside the app.

Open `Dromond.xcodeproj` in Xcode and run the `Dromond` scheme. In Settings,
enter the daemon URL (including its port) and the shared `X-Orchestra-Key`.
The URL is stored in app preferences and the key is stored as a
this-device-only Keychain item. The Xcode project, target, bundle identifier,
and Keychain service retain their Dromond names so an installed app upgrades in
place; the product shown on the phone is Orchestra. The app also moves a key
saved under the older `com.batteryshark.maestro` service into the retained
`com.batteryshark.dromond` service without asking for it again.

Orchestra commonly serves plain HTTP over an encrypted tailnet, so the app
permits user-supplied HTTP endpoints. Do not point it at an untrusted network:
the shared key is an HTTP header and TLS is still required when the transport
itself is not trusted.

`DromondTests/Fixtures/snapshot-v6.json` pins the oldest supported contract;
the v7 fixture proves that a newer snapshot still decodes. The daemon's current
snapshot may be newer still. Raise `Snapshot.minimumVersion` only when the app
intentionally drops compatibility with older snapshots.

## Install it on your iPhone

```
./ios/deploy.sh
```

Builds Release, signs it, and installs on the first paired iPhone — no Xcode
window. `--list` shows what is paired; pass a device id to pick one. The team
id is read from the Apple Development certificate in your keychain, so nothing
about the developer account lives in this repository; `ORCHESTRA_TEAM`
overrides it. The older `DROMOND_TEAM` name remains accepted for existing
automation. A locked phone installs fine and refuses to launch, which the
script reports rather than treating as a failure.

## Install on a phone that is somewhere else

```
./ios/ota.sh
```

Builds, signs, and publishes the app at
`https://<machine>.<tailnet>.ts.net/ios-installer/dromond/`. The retained leaf
keeps existing installer links and Tailscale configuration working;
`ORCHESTRA_OTA_PATH` overrides it. Open the URL on the phone and tap Install;
iOS fetches it directly. `--off` stops serving.

Apple's own wireless install needs mDNS on the local link, which Tailscale does
not carry, so `deploy.sh` only works on the same network. This goes the other
way: iOS installs a signed build from any HTTPS URL it trusts, and
`tailscale serve` provides one with a real certificate. The phone's UDID has to
be in the development profile already, which the first cabled install does.

It adds the installer path to the tailnet serve config and leaves anything
already served at `/` alone. `ORCHESTRA_OTA_PORT` changes the local serving
port. The older `DROMOND_OTA_PATH` and `DROMOND_OTA_PORT` names remain accepted
for existing automation.
