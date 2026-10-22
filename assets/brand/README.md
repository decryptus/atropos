# Atropos · Brand assets

Editable vector edition of the approved gold, ivory and pale-blue woven emblem, with a transparent rounded triangular counter.

## Files

- `svg/atropos-logo.svg`: main stacked logo, transparent; intended for dark surfaces.
- `svg/atropos-logo-horizontal.svg`: horizontal lockup, transparent.
- `svg/atropos-symbol.svg`: emblem, transparent square canvas.
- `svg/atropos-wordmark.svg`: custom lettering converted to paths; no font dependency.
- `svg/atropos-logo-for-light.svg`: dark final letters for light surfaces.
- `*-black.svg` / `*-white.svg`: monochrome transparent versions.
- `*-on-black.svg`: colour versions on an explicitly solid black background.
- `png/`: matching rendered PNG exports, including 256–2048 px main logo and symbol.
- `favicon/`: SVG, multi-resolution ICO, PNG sizes 16–256, Apple touch icon 180, Android icons 192/512 and web manifest. Apple/Android icons have a black background.
- `preview.png`: contact sheet on white and black; this is a preview, not a logo export.
- `source/approved-raster-reference.png`: preserved original design reference. Its brown background is part of the historical reference only; it is not a production asset.

All SVGs contain real paths and gradients: no embedded raster images, external fonts or external resources. The ribbon geometry was reconstructed and the original lettering traced. The vector edition smooths the raster's photographic texture; it is not a pixel-identical raster conversion. Edit the SVG masters directly in Inkscape, Illustrator or another vector editor.

## Palette

| Role | Colour |
| --- | --- |
| Gold highlight | `#FFD675` |
| Gold midtone | `#EDA43D` |
| Sand | `#DFBB94` |
| Ivory | `#FFF6E8` |
| Pale blue | `#9BCEF0` |
| Blue highlight | `#C1E6FF` |
| Dark lettering | `#243747` |
| Optional background | `#000000` |

Maintain the aspect ratio and transparent triangular counter. Use the symbol alone below 160 px width. The square symbol canvas includes approximately 50 units of clear space around the original artwork; keep that margin wherever practical. The coloured main wordmark includes ivory letters: choose the light-surface or black monochrome version on white.

## Regenerate exports

From the repository root:

```sh
python -m pip install -r scripts/brand-requirements.txt
python scripts/build_brand.py
```

The SVG masters are the source of truth. Exporting does not require an image-generation service.

## Website integration

Copy `favicon/` under your site's public asset directory, then adjust the paths below:

```html
<link rel="icon" href="/brand/favicon/favicon.ico" sizes="any">
<link rel="icon" href="/brand/favicon/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/brand/favicon/apple-touch-icon.png">
<link rel="manifest" href="/brand/favicon/site.webmanifest">
```
