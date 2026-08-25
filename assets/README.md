# Orchestra — visual identity

Three vertical bars at different heights, like section levels on a mixing
desk: many voices, one piece. This is the original Orchestra mark, restored
2026-08-24.

## Palette

| Name | Hex | Use |
|---|---|---|
| Pit Black | `#0b0d10` | the mark's ground tile |
| Bar Gray | `#f3f4f2` | the bars and the wordmark letters |

Two colours only. The three bars differ by opacity on the same gray:
`.45`, `1`, `.7`, left to right.

## Which asset goes where

| Asset | Goes | How |
|---|---|---|
| `orchestra-mark.svg` | anywhere a square is wanted: docs, README hero, social card | plain `<img>`; it carries its own ground, so no background needed |
| `orchestra-logo.svg` | README and any page wanting mark + name | `<img>`; the tile is its own ground, one file covers light and dark |
| `orchestra-favicon.svg` | browser tab | `<link rel="icon" type="image/svg+xml" href="assets/orchestra-favicon.svg">` |
| `orchestra-icon-1024.png` | iOS app icon, the 1024×1024 App Store slot | drop into the `AppIcon` asset set |

Three copies live outside this directory and have to be edited with it:
`orchestra/dashboard.html` inlines the favicon as a data URI and draws the
bars (no tile) as inline SVG in its header,
`ios/Orchestra/Assets.xcassets/AppIcon.appiconset/` holds a copy of the
1024px PNG, and `ios/Orchestra/Components.swift` draws the mark as SwiftUI
shapes. Change a bar here and all three need the same change.

The logo names its type as Inter with a system-sans fallback; no font file
ships with it, so a machine without Inter renders the fallback.

### Regenerating the PNG

```
rsvg-convert -w 1024 -h 1024 -b '#0b0d10' -o assets/orchestra-icon-1024.png assets/orchestra-mark.svg
```

The `-b` flag floods the tile's rounded corners with the same ink. iOS wants a
full-bleed opaque square and applies its own corner mask.

## Rules

1. Never recolour the bars or the ground; the opacities are the design.
2. Never rotate, skew, or stretch it. Scale uniformly only.
3. Never add anything inside the tile.
4. The bars hold at 16px; below that, use nothing.
