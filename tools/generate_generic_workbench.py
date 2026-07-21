#!/usr/bin/env python3
"""Generate the editable and game-ready generic 1x1 workbench assets."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "artwork_sources/generic-workbench-1x1/source.svg"
ASSET_DIR = ROOT / "rw_art_pipeline/template_assets/generic-workbench-1x1"
CATALOG_PATH = ROOT / "rw_art_pipeline/template_assets/catalog.json"

CANVAS = 128
TOP_OUTER = [(18, 18), (110, 18), (118, 26), (118, 78), (110, 86), (18, 86), (10, 78), (10, 26)]
TOP_FRAME = [(21, 22), (107, 22), (114, 29), (114, 75), (107, 82), (21, 82), (14, 75), (14, 29)]
WORK_SURFACE = [(29, 28), (99, 28), (108, 37), (108, 66), (99, 75), (29, 75), (20, 66), (20, 37)]
APRON = [(14, 72), (22, 80), (106, 80), (114, 72), (114, 96), (106, 104), (22, 104), (14, 96)]
APRON_FACE = [(19, 80), (24, 85), (104, 85), (109, 80), (109, 94), (103, 100), (25, 100), (19, 94)]

PALETTE = {
    "outline": "#343839",
    "primary": "#d9dddc",
    "primary_dark": "#aeb4b3",
    "primary_light": "#ffffff",
    "surface": "#e8e7e2",
    "surface_dark": "#bdbdb8",
    "surface_light": "#ffffff",
    "hardware": "#626868",
    "hardware_light": "#e8ece9",
}


def _points(points: list[tuple[int, int]], scale: float, offset_y: int = 0) -> list[tuple[int, int]]:
    return [(round(x * scale), round((y + offset_y) * scale)) for x, y in points]


def _box(box: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    return tuple(round(value * scale) for value in box)  # type: ignore[return-value]


def _draw_texture(size: int) -> Image.Image:
    supersampling = 4 if size <= CANVAS else 2
    scale = size * supersampling / CANVAS
    image = Image.new("RGBA", (size * supersampling, size * supersampling), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    width = max(1, round(2 * scale))
    draw.polygon(_points(APRON, scale), fill=PALETTE["outline"])
    draw.polygon(_points(APRON_FACE, scale), fill=PALETTE["primary_dark"])
    draw.line(_points([(24, 85), (104, 85), (109, 80)], scale), fill=PALETTE["primary"], width=width)
    draw.line(_points([(109, 94), (103, 100), (25, 100)], scale), fill=PALETTE["outline"], width=width)

    draw.polygon(_points(TOP_OUTER, scale), fill=PALETTE["outline"])
    draw.polygon(_points(TOP_FRAME, scale), fill=PALETTE["primary"])

    # Broad bevels establish the same upper-left lighting used by vanilla worktables.
    draw.line(_points([(21, 22), (107, 22), (114, 29)], scale), fill=PALETTE["primary_light"], width=width)
    draw.line(_points([(114, 75), (107, 82), (21, 82)], scale), fill=PALETTE["primary_dark"], width=width)

    draw.polygon(_points(WORK_SURFACE, scale), fill=PALETTE["surface_dark"])
    draw.polygon(
        _points([(31, 31), (97, 31), (104, 38), (104, 64), (97, 71), (31, 71), (24, 64), (24, 38)], scale),
        fill=PALETTE["surface"],
    )
    draw.line(_points([(31, 31), (97, 31), (104, 38)], scale), fill=PALETTE["surface_light"], width=width)
    draw.line(_points([(104, 64), (97, 71), (31, 71)], scale), fill=PALETTE["surface_dark"], width=width)

    for x, y in ((20, 28), (108, 28), (108, 74), (20, 74)):
        draw.ellipse(_box((x - 3, y - 3, x + 3, y + 3), scale), fill=PALETTE["hardware"], outline=PALETTE["outline"], width=width)
        draw.ellipse(_box((x - 1, y - 2, x + 1, y), scale), fill=PALETTE["hardware_light"])

    return image.resize((size, size), Image.Resampling.LANCZOS)


def _draw_mask(texture: Image.Image) -> Image.Image:
    scale = 4
    mask = Image.new("RGBA", (CANVAS * scale, CANVAS * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mask)

    draw.polygon(_points(APRON, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(APRON_FACE, scale), fill=(255, 0, 0, 255))
    draw.polygon(_points(TOP_OUTER, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(TOP_FRAME, scale), fill=(255, 0, 0, 255))
    draw.polygon(_points(WORK_SURFACE, scale), fill=(0, 255, 0, 255))
    for x, y in ((20, 28), (108, 28), (108, 74), (20, 74)):
        draw.ellipse(_box((x - 3, y - 3, x + 3, y + 3), scale), fill=(0, 0, 0, 255))

    mask = mask.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)
    mask.putalpha(texture.getchannel("A"))
    return mask


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=9)
    return output.getvalue()


def _svg() -> bytes:
    outer = " ".join(f"{x},{y}" for x, y in TOP_OUTER)
    frame = " ".join(f"{x},{y}" for x, y in TOP_FRAME)
    surface = " ".join(f"{x},{y}" for x, y in WORK_SURFACE)
    apron = " ".join(f"{x},{y}" for x, y in APRON)
    apron_face = " ".join(f"{x},{y}" for x, y in APRON_FACE)
    css = "\n".join(f"      --{name.replace('_', '-')}: {color};" for name, color in PALETTE.items())
    bolts = "\n".join(
        f'    <g transform="translate({x} {y})"><circle r="3"/><ellipse cx="0" cy="-1" rx="1" ry="1" class="bolt-highlight"/></g>'
        for x, y in ((20, 28), (108, 28), (108, 74), (20, 74))
    )
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 128 128">
  <title>Generic 1x1 RimWorld workbench base</title>
  <desc>South-facing industrial workbench with a foreshortened top and a clear tool-overlay area.</desc>
  <style>
    :root {{
{css}
    }}
    .outline {{ fill: var(--outline); }}
    .bolt-highlight {{ fill: var(--hardware-light); stroke: none; }}
  </style>
  <g id="front-apron" inkscape:groupmode="layer" inkscape:label="Front apron">
    <polygon points="{apron}" fill="var(--outline)"/>
    <polygon points="{apron_face}" fill="var(--primary-dark)"/>
    <path d="M24 85 H104 L109 80" fill="none" stroke="var(--primary)" stroke-width="2"/>
  </g>
  <g id="primary-tint" inkscape:groupmode="layer" inkscape:label="Primary tint">
    <polygon points="{outer}" fill="var(--outline)"/>
    <polygon points="{frame}" fill="var(--primary)"/>
    <path d="M21 22 H107 L114 29" fill="none" stroke="var(--primary-light)" stroke-width="2"/>
    <path d="M114 75 L107 82 H21" fill="none" stroke="var(--primary-dark)" stroke-width="2"/>
  </g>
  <g id="secondary-tint" inkscape:groupmode="layer" inkscape:label="Secondary tint and tool-overlay area">
    <polygon points="{surface}" fill="var(--surface-dark)"/>
    <polygon points="31,31 97,31 104,38 104,64 97,71 31,71 24,64 24,38" fill="var(--surface)"/>
    <path d="M31 31 H97 L104 38" fill="none" stroke="var(--surface-light)" stroke-width="2"/>
    <path d="M104 64 L97 71 H31" fill="none" stroke="var(--surface-dark)" stroke-width="2"/>
  </g>
  <g id="hardware" inkscape:groupmode="layer" inkscape:label="Neutral hardware" fill="var(--hardware)" stroke="var(--outline)" stroke-width="2">
{bolts}
  </g>
  <g id="overlay-guide" inkscape:groupmode="layer" inkscape:label="Tool overlay guide (hidden)" display="none">
    <path d="M31 31 H97 L104 38 V64 L97 71 H31 L24 64 V38 Z" fill="none" stroke="#ff00ff" stroke-dasharray="2 2"/>
  </g>
</svg>
'''
    return content.encode("utf-8")


def generated_files() -> dict[Path, bytes]:
    texture = _draw_texture(CANVAS)
    return {
        SOURCE_PATH: _svg(),
        ASSET_DIR / "source.png": _png_bytes(_draw_texture(1024)),
        ASSET_DIR / "rimworld-texture.png": _png_bytes(texture),
        ASSET_DIR / "rimworld-texturem.png": _png_bytes(_draw_mask(texture)),
    }


def _catalog_problems(files: dict[Path, bytes]) -> list[str]:
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        template = next(item for item in catalog["templates"] if item["id"] == "generic-workbench-1x1")
    except (OSError, KeyError, StopIteration, TypeError, ValueError) as error:
        return [f"cannot validate catalog metadata: {error}"]

    paths = {
        "source": ASSET_DIR / "source.png",
        "rimworld-texture": ASSET_DIR / "rimworld-texture.png",
        "rimworld-color-mask": ASSET_DIR / "rimworld-texturem.png",
    }
    variants = {variant["id"]: variant for variant in template.get("variants", [])}
    problems: list[str] = []
    for variant_id, path in paths.items():
        variant = variants.get(variant_id)
        if not isinstance(variant, dict):
            problems.append(f"catalog is missing variant {variant_id}")
            continue
        payload = files[path]
        digest = hashlib.sha256(payload).hexdigest()
        with Image.open(io.BytesIO(payload)) as image:
            actual = (image.width, image.height, image.mode)
        expected = (variant.get("width"), variant.get("height"), variant.get("mode"))
        if expected != actual:
            problems.append(f"catalog metadata for {variant_id} is {expected}, generated asset is {actual}")
        if variant.get("sha256") != digest:
            problems.append(f"catalog SHA-256 for {variant_id} must be {digest}")
        expected_path = path.relative_to(ASSET_DIR.parent).as_posix()
        if variant.get("path") != expected_path:
            problems.append(f"catalog path for {variant_id} must be {expected_path}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed assets differ from generated output")
    parser.add_argument(
        "--replace-source",
        action="store_true",
        help="replace a differing generated SVG after intentionally changing this generator",
    )
    args = parser.parse_args()

    files = generated_files()
    generated_svg = files[SOURCE_PATH]
    if (
        not args.check
        and SOURCE_PATH.is_file()
        and SOURCE_PATH.read_bytes() != generated_svg
        and not args.replace_source
    ):
        print("generated SVG differs; rerun with --replace-source after confirming generator changes")
        return 1

    stale: list[Path] = []
    for path, payload in files.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != payload:
                stale.append(path.relative_to(ROOT))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != payload:
            path.write_bytes(payload)

    if stale:
        for path in stale:
            print(f"stale generated asset: {path}")
        return 1
    catalog_problems = _catalog_problems(files)
    if catalog_problems:
        for problem in catalog_problems:
            print(problem)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
