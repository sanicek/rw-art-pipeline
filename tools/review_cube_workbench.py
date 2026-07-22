#!/usr/bin/env python3
"""Render the cube-workbench candidate sheet without promoting template bytes."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from generate_generic_workbench import cube_svg, draw_cube_mask, draw_cube_texture


STEEL = (160, 178, 181)
WOOD = (133, 97, 67)


def recolor(texture: Image.Image, mask: Image.Image, stuff: tuple[int, int, int]) -> Image.Image:
    """Approximate CutoutComplex's primary channel for an offline comparison."""

    output = texture.copy().convert("RGBA")
    source = output.load()
    channels = mask.convert("RGBA").load()
    for y in range(output.height):
        for x in range(output.width):
            red = channels[x, y][0] / 255
            if red <= 0:
                continue
            original = source[x, y]
            tinted = tuple(round(original[index] * stuff[index] / 255) for index in range(3))
            source[x, y] = tuple(round(original[index] * (1 - red) + tinted[index] * red) for index in range(3)) + (original[3],)
    return output


def checkerboard(size: tuple[int, int], square: int = 8) -> Image.Image:
    background = Image.new("RGB", size, "#25282b")
    draw = ImageDraw.Draw(background)
    for y in range(0, size[1], square):
        for x in range(0, size[0], square):
            if (x // square + y // square) % 2 == 0:
                draw.rectangle((x, y, x + square - 1, y + square - 1), fill="#303438")
    return background


def place_sprite(sheet: Image.Image, sprite: Image.Image, box: tuple[int, int, int, int], label: str) -> None:
    draw = ImageDraw.Draw(sheet)
    left, top, right, bottom = box
    panel = checkerboard((right - left, bottom - top))
    panel.thumbnail(panel.size)
    sheet.paste(panel, (left, top))
    preview = sprite.copy()
    preview.thumbnail((right - left - 24, bottom - top - 48), Image.Resampling.NEAREST)
    x = left + (right - left - preview.width) // 2
    y = top + 12 + (bottom - top - 48 - preview.height) // 2
    sheet.paste(preview, (x, y), preview)
    draw.text((left + 10, bottom - 28), label, fill="white", font=ImageFont.load_default())


def scaled_cell(sprite: Image.Image, draw_size: tuple[float, float]) -> Image.Image:
    panel = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    cell = (80, 80, 160, 160)
    draw.rectangle(cell, fill=(65, 69, 72, 255), outline=(145, 149, 152, 255), width=2)
    width = round(80 * draw_size[0])
    height = round(80 * draw_size[1])
    preview = sprite.resize((width, height), Image.Resampling.LANCZOS)
    panel.alpha_composite(preview, ((240 - width) // 2, (240 - height) // 2))
    return panel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="temporary mirrored vanilla guide")
    parser.add_argument("output", type=Path, help="external review directory")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    texture = draw_cube_texture(128)
    mask = draw_cube_mask(texture)
    texture.save(args.output / "cube-candidate.png")
    mask.save(args.output / "cube-candidate_m.png")
    (args.output / "cube-candidate.svg").write_bytes(cube_svg())

    reference = Image.open(args.reference).convert("RGBA")
    sheet = Image.new("RGB", (1200, 820), "#17191b")
    draw = ImageDraw.Draw(sheet)
    draw.text((30, 20), "Generic cube workbench: deterministic vector candidate", fill="white", font=ImageFont.load_default())
    draw.text((30, 42), "Vanilla pixels are shown only in the temporary guide panel and are not candidate content.", fill="#b8bdc2", font=ImageFont.load_default())

    place_sprite(sheet, reference.resize((128, 146), Image.Resampling.NEAREST), (30, 80, 300, 350), "local mirrored proportion guide")
    place_sprite(sheet, texture, (330, 80, 600, 350), "neutral diffuse, 128x128")
    place_sprite(sheet, mask, (630, 80, 900, 350), "CutoutComplex mask")
    place_sprite(sheet, recolor(texture, mask, STEEL), (930, 80, 1170, 350), "steel approximation")

    place_sprite(sheet, recolor(texture, mask, WOOD), (30, 390, 300, 780), "wood approximation")
    for index, draw_size in enumerate(((1.10, 1.45), (1.17, 1.50), (1.25, 1.55))):
        panel = scaled_cell(recolor(texture, mask, STEEL), draw_size)
        left = 330 + index * 280
        place_sprite(sheet, panel, (left, 390, left + 250, 780), f"simulated drawSize {draw_size}")
    sheet.save(args.output / "cube-review-sheet.png")
    print(f"Review sheet: {args.output / 'cube-review-sheet.png'}")


if __name__ == "__main__":
    main()
