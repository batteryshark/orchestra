# Dromond for iOS

The iOS app is a small mirror of Dromond's run list and normalized trace stream. It can stop a live run or send it a `tell`; push notifications, decisions, and Work record editing deliberately remain outside the app.

Open `Dromond.xcodeproj` in Xcode and run the `Dromond` scheme. In Settings, enter the daemon URL (including its port) and the shared `X-Dromond-Key`. The URL is stored in app preferences and the key is stored as a this-device-only Keychain item. On first launch after the rename, the app moves a key saved under the old `com.batteryshark.maestro` Keychain service to `com.batteryshark.dromond`, so an installed app keeps working without re-entry.

Dromond commonly serves plain HTTP over an encrypted tailnet, so the app permits user-supplied HTTP endpoints. Do not point it at an untrusted network: the shared key is an HTTP header and TLS is still required when the transport itself is not trusted.

`DromondTests/Fixtures/snapshot-v7.json` captures the current API contract; the v6 fixture stays to prove an older snapshot still decodes. A snapshot version bump must update `Snapshot.minimumVersion` and that fixture together.

## Install it on your iPhone

```
./ios/deploy.sh
```

Builds Release, signs it, and installs on the first paired iPhone — no Xcode
window. `--list` shows what is paired; pass a device id to pick one. The team
id is read from the Apple Development certificate in your keychain, so nothing
about the developer account lives in this repository; `DROMOND_TEAM` overrides
it. A locked phone installs fine and refuses to launch, which the script
reports rather than treating as a failure.

## Install on a phone that is somewhere else

```
./ios/ota.sh
```

Builds, signs, and publishes the app at `https://<machine>.<tailnet>.ts.net/install/`.
Open that on the phone and tap Install; iOS fetches it directly. `--off` stops
serving.

Apple's own wireless install needs mDNS on the local link, which Tailscale does
not carry, so `deploy.sh` only works on the same network. This goes the other
way: iOS installs a signed build from any HTTPS URL it trusts, and
`tailscale serve` provides one with a real certificate. The phone's UDID has to
be in the development profile already, which the first cabled install does.

It adds a `/install` path to the tailnet serve config and leaves anything
already served at `/` alone.
