# LiveMotion icon

The master is an original **built-in `image_gen__imagegen`** output, generated
with a transparent background on 2026-10-28. The exact final prompt is in
`prompt.txt`. The visual is a blue photographic frame with a right-facing play
triangle, without text, a stopwatch, or a left arrow.

## Source and exports

- `livemotion-icon-master.png`: untouched 1254 × 1254 RGBA generation, including
  the generator's original metadata and alpha.
- `exports/`: 16, 32, 48, 64, 128, 256 and 512px deterministic PNG exports.
- `preview-light-dark.png`: actual-size 32/48/64/128/256px exports on the UI's
  light (`#f3f6fb`) and dark (`#111c2e`) surfaces.
- `checks.json`: dimensions, alpha bounds, visible bounds and SHA-256 hashes.

The blue tile's visible alpha-128 bounds on the master are
`(132, 151, 1122, 1106)`, leaving a transparent safety margin. Low-alpha
antialiased pixels are preserved rather than thresholded. The master has no
opaque rectangular background. Desktop pixels can be inspected in the preview;
actual fnOS desktop cropping, caching and appearance require a device check.

## Reproduce

Use Python 3.10+ with Pillow 10.2+ installed (Pillow 12.3.0 was used here):

```sh
python3 assets/branding/export_icons.py
python3 assets/branding/export_icons.py --check
```

The export uses premultiplied-alpha Lanczos resampling to avoid transparent-edge
halos; it does not redraw the generated artwork. The four packaging PNGs are
replaced by two identical alias pairs:

| Size | Packaging paths |
| --- | --- |
| 64 | `fpk/icon.png`, `fpk/ui/images/64.png` |
| 256 | `fpk/ICON_256.PNG`, `fpk/ui/images/256.png` |

The web templates use `static/livemotion-icon-64.png` and
`static/livemotion-icon-256.png`. `static/livemotion-icon.svg` is solely a legacy
favicon wrapper embedding the **same generated 64px PNG pixels**, not a separate
vector design. The existing `/favicon.svg` route can retain its SVG MIME type.
No application version was changed for the icon.

## Rollback

Restore the four tracked packaging PNGs and the two brand template references
from the desired Git revision; restore the favicon route along with its chosen
assets. Keep or remove this branding source directory and the new static icon
files as desired. Regenerating is not necessary for rollback.
