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
DESK_SOURCE_PATH = ROOT / "artwork_sources/generic-desk-workbench-1x1/source.svg"
DESK_ASSET_DIR = ROOT / "rw_art_pipeline/template_assets/generic-desk-workbench-1x1"
CUBE_SOURCE_PATH = ROOT / "artwork_sources/generic-cube-workbench-1x1/source.svg"
CUBE_ASSET_DIR = ROOT / "rw_art_pipeline/template_assets/generic-cube-workbench-1x1"
CATALOG_PATH = ROOT / "rw_art_pipeline/template_assets/catalog.json"

CANVAS = 128
TOP_OUTER = [(18, 18), (110, 18), (118, 26), (118, 78), (110, 86), (18, 86), (10, 78), (10, 26)]
TOP_FRAME = [(21, 22), (107, 22), (114, 29), (114, 75), (107, 82), (21, 82), (14, 75), (14, 29)]
WORK_SURFACE = [(29, 28), (99, 28), (108, 37), (108, 66), (99, 75), (29, 75), (20, 66), (20, 37)]
APRON = [(14, 72), (22, 80), (106, 80), (114, 72), (114, 96), (106, 104), (22, 104), (14, 96)]
APRON_FACE = [(19, 80), (24, 85), (104, 85), (109, 80), (109, 94), (103, 100), (25, 100), (19, 94)]

DESK_TOP_OUTER = [(10, 20), (118, 20), (122, 24), (122, 70), (116, 78), (12, 78), (6, 70), (6, 24)]
DESK_TOP_FRAME = [(13, 24), (115, 24), (118, 27), (118, 67), (112, 74), (16, 74), (10, 67), (10, 27)]
DESK_SURFACE = [(20, 29), (108, 29), (112, 33), (112, 62), (106, 68), (22, 68), (16, 62), (16, 33)]
DESK_CABINET = [(10, 67), (118, 67), (118, 101), (111, 108), (17, 108), (10, 101)]
DESK_CABINET_FACE = [(16, 75), (112, 75), (112, 98), (107, 103), (21, 103), (16, 98)]

# The cube template keeps the proportions of one mirrored end segment from a
# three-cell vanilla worktable, but uses original symmetric geometry. Its broad
# top is neutral; the perimeter and front apron receive the stuff color.
CUBE_TOP_OUTER = [(22, 8), (106, 8), (109, 12), (109, 98), (106, 102), (22, 102), (19, 98), (19, 12)]
CUBE_TOP_FRAME = [(23, 12), (105, 12), (106, 14), (106, 96), (105, 98), (23, 98), (22, 96), (22, 14)]
CUBE_SURFACE = [(27, 16), (101, 16), (103, 18), (103, 92), (101, 94), (27, 94), (25, 92), (25, 18)]
CUBE_APRON = [(19, 96), (109, 96), (109, 116), (106, 120), (22, 120), (19, 116)]
CUBE_APRON_FACE = [(22, 100), (106, 100), (106, 114), (103, 116), (25, 116), (22, 114)]

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


def _mirror_right_half(image: Image.Image) -> Image.Image:
    """Make even-width candidate output byte-symmetric after rasterization."""

    if image.width % 2:
        raise ValueError("symmetric workbench output requires an even width")
    half = image.width // 2
    right = image.crop((half, 0, image.width, image.height))
    image.paste(right.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, 0))
    return image


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


def _draw_desk_texture(size: int) -> Image.Image:
    supersampling = 4 if size <= CANVAS else 2
    scale = size * supersampling / CANVAS
    image = Image.new("RGBA", (size * supersampling, size * supersampling), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = max(1, round(2 * scale))

    draw.polygon(_points(DESK_CABINET, scale), fill=PALETTE["outline"])
    draw.polygon(_points(DESK_CABINET_FACE, scale), fill=PALETTE["primary_dark"])
    draw.line(_points([(20, 78), (108, 78)], scale), fill=PALETTE["primary"], width=width)
    draw.line(_points([(107, 103), (21, 103), (16, 98)], scale), fill=PALETTE["outline"], width=width)
    draw.line(_points([(64, 79), (64, 99)], scale), fill=PALETTE["outline"], width=max(1, round(scale)))

    draw.polygon(_points(DESK_TOP_OUTER, scale), fill=PALETTE["outline"])
    draw.polygon(_points(DESK_TOP_FRAME, scale), fill=PALETTE["primary"])
    draw.line(_points([(13, 24), (115, 24), (118, 27)], scale), fill=PALETTE["primary_light"], width=width)
    draw.line(_points([(118, 67), (112, 74), (16, 74)], scale), fill=PALETTE["primary_dark"], width=width)

    draw.polygon(_points(DESK_SURFACE, scale), fill=PALETTE["surface_dark"])
    draw.polygon(
        _points([(22, 32), (106, 32), (108, 34), (108, 60), (104, 64), (24, 64), (20, 60), (20, 34)], scale),
        fill=PALETTE["surface"],
    )
    draw.line(_points([(22, 32), (106, 32), (108, 34)], scale), fill=PALETTE["surface_light"], width=width)
    draw.line(_points([(108, 60), (104, 64), (24, 64)], scale), fill=PALETTE["surface_dark"], width=width)

    for x in (22, 106):
        draw.ellipse(_box((x - 2, 88, x + 2, 92), scale), fill=PALETTE["hardware"], outline=PALETTE["outline"], width=max(1, round(scale)))
        draw.ellipse(_box((x - 1, 88, x, 89), scale), fill=PALETTE["hardware_light"])

    return image.resize((size, size), Image.Resampling.LANCZOS)


def _draw_desk_mask(texture: Image.Image) -> Image.Image:
    scale = 4
    mask = Image.new("RGBA", (CANVAS * scale, CANVAS * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mask)

    draw.polygon(_points(DESK_CABINET, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(DESK_CABINET_FACE, scale), fill=(255, 0, 0, 255))
    draw.line(_points([(64, 79), (64, 99)], scale), fill=(0, 0, 0, 255), width=3 * scale)
    draw.polygon(_points(DESK_TOP_OUTER, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(DESK_TOP_FRAME, scale), fill=(255, 0, 0, 255))
    draw.polygon(_points(DESK_SURFACE, scale), fill=(0, 255, 0, 255))
    for x in (22, 106):
        draw.ellipse(_box((x - 2, 88, x + 2, 92), scale), fill=(0, 0, 0, 255))

    mask = mask.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)
    mask.putalpha(texture.getchannel("A"))
    return mask


def draw_cube_texture(size: int) -> Image.Image:
    """Render the symmetric blank-cube template at one requested resolution."""

    supersampling = 4 if size <= CANVAS else 2
    scale = size * supersampling / CANVAS
    image = Image.new("RGBA", (size * supersampling, size * supersampling), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = max(1, round(2 * scale))

    draw.polygon(_points(CUBE_APRON, scale), fill=PALETTE["outline"])
    draw.polygon(_points(CUBE_APRON_FACE, scale), fill=PALETTE["primary_dark"])
    draw.line(_points([(25, 102), (103, 102)], scale), fill=PALETTE["primary"], width=width)
    draw.line(_points([(103, 116), (25, 116), (22, 114)], scale), fill=PALETTE["outline"], width=width)

    draw.polygon(_points(CUBE_TOP_OUTER, scale), fill=PALETTE["outline"])
    draw.polygon(_points(CUBE_TOP_FRAME, scale), fill=PALETTE["primary"])

    surface_bounds = _box((25, 16, 104, 95), scale)
    surface_width = surface_bounds[2] - surface_bounds[0]
    surface_height = surface_bounds[3] - surface_bounds[1]
    gradient = Image.new("RGBA", (surface_width, surface_height), (0, 0, 0, 0))
    pixels = gradient.load()
    for y in range(surface_height):
        vertical = 1.0 - abs((y + 0.5) / surface_height - 0.48) * 0.30
        for x in range(surface_width):
            horizontal = 1.0 - abs((x + 0.5) / surface_width - 0.5) * 0.38
            value = round(188 + 48 * horizontal * vertical)
            pixels[x, y] = (value, value, value, 255)
    surface_mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(surface_mask).polygon(_points(CUBE_SURFACE, scale), fill=255)
    image.paste(gradient, surface_bounds[:2], surface_mask.crop(surface_bounds))

    draw = ImageDraw.Draw(image)
    draw.line(_points([(23, 12), (105, 12), (106, 14)], scale), fill=PALETTE["primary_light"], width=width)
    draw.line(_points([(106, 96), (105, 98), (23, 98)], scale), fill=PALETTE["primary_dark"], width=width)
    return _mirror_right_half(image.resize((size, size), Image.Resampling.LANCZOS))


def draw_cube_mask(texture: Image.Image) -> Image.Image:
    """Map stuff color to the cube shell while keeping its worktop neutral."""

    scale = 4
    mask = Image.new("RGBA", (CANVAS * scale, CANVAS * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mask)
    draw.polygon(_points(CUBE_APRON, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(CUBE_APRON_FACE, scale), fill=(255, 0, 0, 255))
    draw.polygon(_points(CUBE_TOP_OUTER, scale), fill=(0, 0, 0, 255))
    draw.polygon(_points(CUBE_TOP_FRAME, scale), fill=(255, 0, 0, 255))
    draw.polygon(_points(CUBE_SURFACE, scale), fill=(0, 255, 0, 255))
    mask = mask.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)
    mask.putalpha(texture.getchannel("A"))
    return _mirror_right_half(mask)


def cube_svg() -> bytes:
    """Emit an editable SVG carrying the same template geometry and channels."""

    points = {
        "top_outer": " ".join(f"{x},{y}" for x, y in CUBE_TOP_OUTER),
        "top_frame": " ".join(f"{x},{y}" for x, y in CUBE_TOP_FRAME),
        "surface": " ".join(f"{x},{y}" for x, y in CUBE_SURFACE),
        "apron": " ".join(f"{x},{y}" for x, y in CUBE_APRON),
        "apron_face": " ".join(f"{x},{y}" for x, y in CUBE_APRON_FACE),
    }
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 128 128">
  <title>Generic symmetric 1x1 RimWorld cube workbench</title>
  <desc>Fixed-perspective blank workbench with a stuff-colored shell and neutral worktop.</desc>
  <defs>
    <radialGradient id="surface-light" cx="50%" cy="48%" r="65%">
      <stop offset="0" stop-color="#ececec"/>
      <stop offset="1" stop-color="#bcbcbc"/>
    </radialGradient>
  </defs>
  <g id="front-apron" inkscape:groupmode="layer" inkscape:label="Stuff-colored front apron">
    <polygon points="{points['apron']}" fill="{PALETTE['outline']}"/>
    <polygon points="{points['apron_face']}" fill="{PALETTE['primary_dark']}"/>
    <path d="M25 102 H103" fill="none" stroke="{PALETTE['primary']}" stroke-width="2"/>
  </g>
  <g id="stuff-shell" inkscape:groupmode="layer" inkscape:label="Stuff-colored top shell">
    <polygon points="{points['top_outer']}" fill="{PALETTE['outline']}"/>
    <polygon points="{points['top_frame']}" fill="{PALETTE['primary']}"/>
    <path d="M23 12 H105 L106 14" fill="none" stroke="{PALETTE['primary_light']}" stroke-width="2"/>
  </g>
  <g id="neutral-worktop" inkscape:groupmode="layer" inkscape:label="Neutral worktop and overlay area">
    <polygon points="{points['surface']}" fill="url(#surface-light)"/>
  </g>
</svg>
'''
    return content.encode("utf-8")


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


def _desk_svg() -> bytes:
    top_outer = " ".join(f"{x},{y}" for x, y in DESK_TOP_OUTER)
    top_frame = " ".join(f"{x},{y}" for x, y in DESK_TOP_FRAME)
    surface = " ".join(f"{x},{y}" for x, y in DESK_SURFACE)
    cabinet = " ".join(f"{x},{y}" for x, y in DESK_CABINET)
    cabinet_face = " ".join(f"{x},{y}" for x, y in DESK_CABINET_FACE)
    css = "\n".join(f"      --{name.replace('_', '-')}: {color};" for name, color in PALETTE.items())
    hardware = "\n".join(
        f'    <g transform="translate({x} 90)"><circle r="2"/><circle cx="-0.5" cy="-0.5" r="0.6" class="bolt-highlight"/></g>'
        for x in (22, 106)
    )
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 128 128">
  <title>Generic boxy 1x1 RimWorld desk workbench base</title>
  <desc>South-facing enclosed desk workbench with a broad rectangular top and clear tool-overlay area.</desc>
  <style>
    :root {{
{css}
    }}
    .bolt-highlight {{ fill: var(--hardware-light); stroke: none; }}
  </style>
  <g id="enclosed-cabinet" inkscape:groupmode="layer" inkscape:label="Primary tint enclosed cabinet">
    <polygon points="{cabinet}" fill="var(--outline)"/>
    <polygon points="{cabinet_face}" fill="var(--primary-dark)"/>
    <path d="M20 78 H108" fill="none" stroke="var(--primary)" stroke-width="2"/>
    <path d="M64 79 V99" fill="none" stroke="var(--outline)" stroke-width="1"/>
  </g>
  <g id="primary-tint" inkscape:groupmode="layer" inkscape:label="Primary tint top frame">
    <polygon points="{top_outer}" fill="var(--outline)"/>
    <polygon points="{top_frame}" fill="var(--primary)"/>
    <path d="M13 24 H115 L118 27" fill="none" stroke="var(--primary-light)" stroke-width="2"/>
    <path d="M118 67 L112 74 H16" fill="none" stroke="var(--primary-dark)" stroke-width="2"/>
  </g>
  <g id="secondary-tint" inkscape:groupmode="layer" inkscape:label="Secondary tint and tool-overlay area">
    <polygon points="{surface}" fill="var(--surface-dark)"/>
    <polygon points="22,32 106,32 108,34 108,60 104,64 24,64 20,60 20,34" fill="var(--surface)"/>
    <path d="M22 32 H106 L108 34" fill="none" stroke="var(--surface-light)" stroke-width="2"/>
    <path d="M108 60 L104 64 H24" fill="none" stroke="var(--surface-dark)" stroke-width="2"/>
  </g>
  <g id="hardware" inkscape:groupmode="layer" inkscape:label="Neutral cabinet hardware" fill="var(--hardware)" stroke="var(--outline)" stroke-width="1">
{hardware}
  </g>
  <g id="overlay-guide" inkscape:groupmode="layer" inkscape:label="Tool overlay guide (hidden)" display="none">
    <path d="M22 32 H106 L108 34 V60 L104 64 H24 L20 60 V34 Z" fill="none" stroke="#ff00ff" stroke-dasharray="2 2"/>
  </g>
</svg>
'''
    return content.encode("utf-8")


def generated_files() -> dict[Path, bytes]:
    texture = _draw_texture(CANVAS)
    desk_texture = _draw_desk_texture(CANVAS)
    cube_texture = draw_cube_texture(CANVAS)
    return {
        SOURCE_PATH: _svg(),
        ASSET_DIR / "source.png": _png_bytes(_draw_texture(1024)),
        ASSET_DIR / "rimworld-texture.png": _png_bytes(texture),
        ASSET_DIR / "rimworld-texturem.png": _png_bytes(_draw_mask(texture)),
        DESK_SOURCE_PATH: _desk_svg(),
        DESK_ASSET_DIR / "source.png": _png_bytes(_draw_desk_texture(1024)),
        DESK_ASSET_DIR / "rimworld-texture.png": _png_bytes(desk_texture),
        DESK_ASSET_DIR / "rimworld-texturem.png": _png_bytes(_draw_desk_mask(desk_texture)),
        CUBE_SOURCE_PATH: cube_svg(),
        CUBE_ASSET_DIR / "source.png": _png_bytes(draw_cube_texture(1024)),
        CUBE_ASSET_DIR / "rimworld-texture.png": _png_bytes(cube_texture),
        CUBE_ASSET_DIR / "rimworld-texturem.png": _png_bytes(draw_cube_mask(cube_texture)),
    }


def _catalog_problems(files: dict[Path, bytes]) -> list[str]:
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        templates = {item["id"]: item for item in catalog["templates"]}
    except (OSError, KeyError, TypeError, ValueError) as error:
        return [f"cannot validate catalog metadata: {error}"]

    template_paths = {
        "generic-workbench-1x1": {
            "source": ASSET_DIR / "source.png",
            "rimworld-texture": ASSET_DIR / "rimworld-texture.png",
            "rimworld-color-mask": ASSET_DIR / "rimworld-texturem.png",
        },
        "generic-desk-workbench-1x1": {
            "source": DESK_ASSET_DIR / "source.png",
            "rimworld-texture": DESK_ASSET_DIR / "rimworld-texture.png",
            "rimworld-color-mask": DESK_ASSET_DIR / "rimworld-texturem.png",
        },
        "generic-cube-workbench-1x1": {
            "source": CUBE_ASSET_DIR / "source.png",
            "rimworld-texture": CUBE_ASSET_DIR / "rimworld-texture.png",
            "rimworld-color-mask": CUBE_ASSET_DIR / "rimworld-texturem.png",
        },
    }
    problems: list[str] = []
    for template_id, paths in template_paths.items():
        template = templates.get(template_id)
        if not isinstance(template, dict):
            problems.append(f"catalog is missing template {template_id}")
            continue
        variants = {variant["id"]: variant for variant in template.get("variants", [])}
        for variant_id, path in paths.items():
            variant = variants.get(variant_id)
            if not isinstance(variant, dict):
                problems.append(f"catalog is missing variant {template_id}/{variant_id}")
                continue
            payload = files[path]
            digest = hashlib.sha256(payload).hexdigest()
            with Image.open(io.BytesIO(payload)) as image:
                actual = (image.width, image.height, image.mode)
            expected = (variant.get("width"), variant.get("height"), variant.get("mode"))
            if expected != actual:
                problems.append(f"catalog metadata for {template_id}/{variant_id} is {expected}, generated asset is {actual}")
            if variant.get("sha256") != digest:
                problems.append(f"catalog SHA-256 for {template_id}/{variant_id} must be {digest}")
            expected_path = path.relative_to(ASSET_DIR.parent).as_posix()
            if variant.get("path") != expected_path:
                problems.append(f"catalog path for {template_id}/{variant_id} must be {expected_path}")
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
    if not args.check and not args.replace_source:
        for source_path in (SOURCE_PATH, DESK_SOURCE_PATH, CUBE_SOURCE_PATH):
            if source_path.is_file() and source_path.read_bytes() != files[source_path]:
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
