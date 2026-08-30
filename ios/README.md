# Orchestra for Apple devices

The shared SwiftUI client targets iPhone, iPad, and native macOS against the
Orchestra v2 API. It is an operator client for one selected standalone
instance, not a work tracker or cross-node scheduler.

## Information architecture

iPhone exposes Runs, Inbox, Groups, Runway, and More. iPad and macOS use a
sidebar with Runs, Inbox, Groups, Profiles, Runway, Fleet, and Settings. Runs
open on their Thread, with Activity, Overview, Artifacts, Changes, Raw Log,
Lineage, Facts/Usage, and Observer detail available as supporting evidence.

The client can:

- show instance identity, daemon health, pause/capacity, visible queue holds,
  profiles and runway freshness;
- start a titled run with a group, worker profile, one executable context body,
  and an optional write-only working-directory override;
- filter/search by group, profile, and status;
- show contextual and fleet-wide run/usage/cost statistics;
- create and edit profiles/runtimes, inspect host-side model discovery without
  automatic mutation, and configure the full Observer policy;
- configure fleet capacity/delegation bounds and create, edit, enable, refresh,
  or archive named runway sources without reading private argv/config back;
- configure a group's optional default working directory without ever reading
  the private host path back;
- follow run threads and bounded raw-log tails, collapse reasoning/tool detail,
  reveal machine receipts on demand, and explicitly open/share the full log;
- Tell, Interrupt, Stop, Stop Tree, Check, Retry, and Continue;
- answer blocking attention and approve/reject profile proposals;
- inspect the fleet Inbox/Outbox ledger with pending, delivered, and
  undeliverable receipts, then open the owning run thread;
- preview common text, Markdown, image, PDF, audio, and video artifacts and
  download other immutable outputs; and
- inspect Git branch/checkpoint/patch/diff evidence without offering write-back.

SSE supplies live invalidations/evidence. A slow refresh reconciles the
snapshot and current resource pages after suspension, network loss, or proxy
interruption. Streams are never treated as durable delivery.

## Pair a device

In an authenticated operator client, create a short-lived pairing code and
pairing URI. Paste the URI or enter its code on the new device.
The one-time exchange returns a revocable
device token stored as a this-device-only Keychain item; the code and token
must never be logged or placed in app preferences.

The client pins `instance_id` beside the endpoint. If it changes, stop and ask
the operator to pair with the reset/replaced instance. Do not silently treat it
as the same fleet.

Orchestra expects a trusted private network plus TLS/reverse proxy when the
transport is not already encrypted. The app may permit an explicitly chosen
HTTP endpoint on a trusted encrypted tailnet, with a clear warning. Orchestra
does not provide relay, public ingress, or APNs.

Active clients use local notifications and badge counts. Guaranteed background
push belongs to an external callback adapter.

## Contract tests

Fixtures pin v2 envelope, snapshot, paged run/feed, Inbox, run detail,
artifact, and control shapes. Decoder tests must cover unknown additive fields,
all run states and hold/wait values, missing optional evidence, and an unexpected
`instance_id`. The contract source is `/api/v2/openapi.json`.

## Build the native apps

The Xcode project contains an iPhone/iPad target and a separate native macOS
target. Both compile the same SwiftUI source set; the Mac app is not Catalyst.

```sh
xcodebuild -project ios/Orchestra.xcodeproj -scheme Orchestra \
  -destination 'generic/platform=iOS' build
xcodebuild -project ios/Orchestra.xcodeproj -scheme 'Orchestra macOS' build
```

## Install on a paired iPhone

```sh
./ios/deploy.sh
```

The script builds Release, signs it, and installs on the first paired iPhone.
`--list` shows devices; pass a device id to choose one.

## Install remotely through a tailnet

```sh
./ios/ota.sh
```

This builds, signs, and publishes an install page through the operator's
existing encrypted network. The phone must already be provisioned. OTA
distribution does not alter Orchestra's authentication or network boundary.
