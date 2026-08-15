# Dromond — visual identity

Three hulls in echelon, the middle one leading. Many hulls, one direction, one
shore.

The fleet is the picture, not the vocabulary. Do not write captain, admiral, or
voyage anywhere in the product.

## Palette

Five values. The three mark colours come straight from the dashboard's own
tokens, so the mark sits in that world instead of fighting it.

| Name | Hex | Use | Dashboard token |
|---|---|---|---|
| Hull Ink | `#161B23` | the mark's ground tile | between `--bg` and `--panel` |
| Fleet Blue | `#61A5E8` | the lead (middle) hull | `--live` |
| Signal Teal | `#4FB3C4` | the two flanking hulls | `--tool` |
| Deep Slate | `#1B2430` | wordmark letters on a light page | — |
| Paper | `#DFE3EA` | wordmark letters on a dark page | `--text` |

The mark itself uses three colours and no more. Deep Slate and Paper belong to
the lettering only.

## Which asset goes where

| Asset | Goes | How |
|---|---|---|
| `dromond-mark.svg` | anywhere a square is wanted: docs, README hero, social card, 32px and up | plain `<img>`; it carries its own ground, so no background needed |
| `dromond-favicon.svg` | browser tab | `<link rel="icon" type="image/svg+xml" href="assets/dromond-favicon.svg">` |
| `dromond-wordmark.svg` | README and any light page | `<img>` |
| `dromond-wordmark-dark.svg` | dashboard header and any dark page | `<img>`, or inline it and set the `.name` group's `stroke` to `currentColor` so it tracks the theme |
| `dromond-icon-1024.png` | iOS app icon, the 1024×1024 App Store slot | drop into the `AppIcon` asset set |

Two copies live outside this directory and have to be edited with it:
`dromond/dashboard.html` inlines the favicon as a data URI and draws the mark as
inline SVG in its header, and `ios/Dromond/Assets.xcassets/AppIcon.appiconset/`
holds the 1024px PNG. Change a hull here and both need the same change.

### Light and dark

The mark and the favicon carry their own ground, so one file covers both
backgrounds. The wordmark cannot — its letters have no ground — so it ships as
two files that differ only in the letter colour. A `prefers-color-scheme` rule
inside the SVG was tried and dropped: browsers honour it, rasterisers and
several markdown viewers do not, so that file rendered invisible ink in exactly
the places nobody checks.

### Regenerating the PNG

```
rsvg-convert -w 1024 -h 1024 -b '#161B23' -o assets/dromond-icon-1024.png assets/dromond-mark.svg
```

The `-b` flag floods the tile's rounded corners with the same ink. The result is
a full-bleed opaque square, which is what iOS wants — iOS applies its own corner
mask, and an iOS icon may not carry transparency.

## Clear space

Keep clear space on all four sides equal to the tile's corner radius: **22% of
the mark's width**. At 512px that is 112px. Nothing — no text, no rule, no other
logo — enters that band.

For the wordmark, measure the same 22% from the mark tile's height.

## Minimum sizes

- `dromond-mark.svg` — 32px. It holds there, but the tapered bows go soft.
- `dromond-favicon.svg` — 16px. At 16px the taper is gone and it reads as three
  stacked bars; the three-hull structure and the gaps survive, which is the
  part that has to.
- Below 16px, use nothing.

## Never

1. Never recolour the hulls or the ground.
2. Never rotate, skew, or stretch it. Scale uniformly only.
3. Never add a mast, sails, oars, a wake, or any text inside the mark.
4. Never remove the ground tile and set the hulls loose on a page.
5. Never use the mark below 32px. Use the favicon there.
