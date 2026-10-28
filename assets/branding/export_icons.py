#!/usr/bin/env python3
"""Export the generated master with premultiplied-alpha Lanczos resampling.

Requires Pillow. This script resizes/composites the generated artwork; it does
not redraw the icon or remove its generated transparency.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont


BRANDING = Path(__file__).resolve().parent
ROOT = BRANDING.parents[1]
MASTER = BRANDING / "livemotion-icon-master.png"
SIZES = (16, 32, 48, 64, 128, 256, 512)
FPK_TARGETS = {
    "fpk/icon.png": 64,
    "fpk/ICON_256.PNG": 256,
    "fpk/ui/images/64.png": 64,
    "fpk/ui/images/256.png": 256,
}
WEB_TARGETS = {
    "src/livephoto_worker/static/livemotion-icon-64.png": 64,
    "src/livephoto_worker/static/livemotion-icon-256.png": 256,
}
SVG_TARGET = ROOT / "src/livephoto_worker/static/livemotion-icon.svg"


def render(master: Image.Image, size: int) -> Image.Image:
    # Resampling premultiplied RGB avoids dark/color halos at transparent edges.
    return master.convert("RGBa").resize(
        (size, size), Image.Resampling.LANCZOS
    ).convert("RGBA")


def png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def svg_wrapper(png: bytes) -> bytes:
    data = base64.b64encode(png).decode("ascii")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
        'viewBox="0 0 64 64">\n'
        f'  <image width="64" height="64" href="data:image/png;base64,{data}"/>\n'
        '</svg>\n'
    ).encode("utf-8")


def inspect(path: Path, expected: Image.Image | None = None) -> dict:
    with Image.open(path) as source:
        source.load()
        assert source.format == "PNG", f"not PNG: {path}"
        assert source.mode == "RGBA", f"not RGBA: {path}"
        image = source.copy()
    width, height = image.size
    alpha = image.getchannel("A")
    assert width == height, f"not square: {path}"
    assert alpha.getextrema()[0] == 0, f"no transparent margin: {path}"
    assert alpha.getextrema()[1] >= 240, f"no near-opaque artwork: {path}"
    assert all(
        image.getpixel(point)[3] == 0
        for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    ), f"nontransparent corner: {path}"
    if expected is not None:
        assert image.size == expected.size, f"wrong dimensions: {path}"
        assert ImageChops.difference(image, expected).getbbox(alpha_only=False) is None, (
            f"pixels differ from master export: {path}"
        )
    visible = alpha.point(lambda value: 255 if value >= 128 else 0).getbbox()
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "size": list(image.size),
        "mode": image.mode,
        "alpha_range": list(alpha.getextrema()),
        "visible_bbox_alpha_128": list(visible) if visible else None,
        "transparent_pixels": alpha.histogram()[0],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def preview(rendered: dict[int, Image.Image]) -> None:
    """Show actual-size exports on both light and dark dashboard backgrounds."""
    width, height = 1200, 800
    image = Image.new("RGB", (width, height), "#f3f6fb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 400, width, height), fill="#111c2e")
    title_font = ImageFont.load_default(size=26)
    label_font = ImageFont.load_default(size=16)
    draw.text((36, 26), "LiveMotion | generated icon", fill="#172033", font=title_font)
    draw.text((36, 66), "Light surface - actual pixel sizes", fill="#64748b", font=label_font)
    draw.text((36, 426), "Dark surface - actual pixel sizes", fill="#cbd5e1", font=label_font)
    positions = ((100, 32), (270, 48), (440, 64), (650, 128), (980, 256))
    for row_y, color in ((244, "#64748b"), (644, "#cbd5e1")):
        for center_x, size in positions:
            icon = rendered[size]
            image.paste(icon, (center_x - size // 2, row_y - size // 2), icon)
            draw.text(
                (center_x, row_y + 145), f"{size} x {size}",
                anchor="mm", fill=color, font=label_font,
            )
    image.save(BRANDING / "preview-light-dark.png", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without changing files")
    args = parser.parse_args()
    with Image.open(MASTER) as source:
        assert source.mode == "RGBA", "Master must retain its generated RGBA alpha"
        master = source.copy()
    rendered = {size: render(master, size) for size in SIZES}
    pngs = {size: png_bytes(image) for size, image in rendered.items()}
    targets = {
        **{f"assets/branding/exports/livemotion-icon-{size}.png": size for size in SIZES},
        **FPK_TARGETS,
        **WEB_TARGETS,
    }
    if not args.check:
        for relative, size in targets.items():
            path = ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pngs[size])
        SVG_TARGET.write_bytes(svg_wrapper(pngs[64]))
        preview(rendered)
    report = [inspect(MASTER)]
    report.extend(inspect(ROOT / relative, rendered[size]) for relative, size in targets.items())
    # Packaging aliases must be byte-for-byte identical, not independently drawn.
    for first, second in (
        ("fpk/icon.png", "fpk/ui/images/64.png"),
        ("fpk/ICON_256.PNG", "fpk/ui/images/256.png"),
    ):
        assert (ROOT / first).read_bytes() == (ROOT / second).read_bytes(), (
            f"Packaging icon pair differs: {first}, {second}"
        )
    encoded_png = SVG_TARGET.read_text().split("data:image/png;base64,", 1)[1].split('"', 1)[0]
    assert base64.b64decode(encoded_png) == (ROOT / "fpk/icon.png").read_bytes(), (
        "Legacy SVG wrapper must embed the generated 64px PNG"
    )
    if not args.check:
        (BRANDING / "checks.json").write_text(
            json.dumps({"assets": report}, indent=2, ensure_ascii=False) + "\n"
        )
    print(f"Verified generated master and {len(targets)} PNG exports; RGBA/alpha/pixels/aliases/SVG passed.")


if __name__ == "__main__":
    main()
