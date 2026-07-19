"""Generate or intake artwork and promote only manifest-approved derivatives.

Provider generations and manual downloads converge on the same content-addressed
archive and repeatable transforms. Approval remains deliberately separate from
generation so visual judgment cannot be mistaken for structural validation.
"""

from __future__ import annotations

import argparse
import colorsys
import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import tomllib
import math
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .scenario import (
    ScenarioClient,
    ScenarioError,
    asset_url,
    job_id,
    job_status,
    load_credentials,
    result_assets,
    store_credentials_interactive,
)


class PipelineError(Exception):
    """A user-correctable manifest, source, or approval failure."""


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PROCESSING_VERSION = 2
MAX_CANVAS_PIXELS = 16_777_216
MAX_SOURCE_PIXELS = 50_000_000
MAX_OUTPUTS = 32


@dataclass(frozen=True)
class Output:
    path: Path
    accent_color: tuple[int, int, int] | None


@dataclass(frozen=True)
class ExternalReference:
    receipt: str
    asset_id: str
    source_sha256: str


@dataclass(frozen=True)
class Generation:
    provider: str
    model_id: str
    aspect_ratio: str | None
    candidates: int
    parameters: dict[str, Any]
    reference_parameter: str | None
    external_references: tuple[ExternalReference, ...]


@dataclass(frozen=True)
class Request:
    id: str
    title: str
    width: int
    height: int
    occupied_fraction: float
    alpha_required: bool
    background_removal: str
    fit: str
    output_mode: str
    accent_saturation_min: float
    references: tuple[str, ...]
    generation: Generation | None
    prompt: str
    outputs: tuple[Output, ...]


@dataclass(frozen=True)
class Manifest:
    path: Path
    package_id: str
    project_name: str
    project_root: Path
    scenario_poll_seconds: float
    scenario_max_wait_seconds: int
    requests: dict[str, Request]


def _hex_color(value: str) -> tuple[int, int, int]:
    if len(value) != 7 or not value.startswith("#"):
        raise PipelineError(f"accent_color must use #rrggbb: {value!r}")
    try:
        return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
    except ValueError as error:
        raise PipelineError(f"invalid accent_color: {value!r}") from error


def load_manifest(path: Path) -> Manifest:
    """Parse and constrain the project-owned contract before touching artwork."""
    manifest_path = path.expanduser().resolve()
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PipelineError(f"cannot read manifest {manifest_path}: {error}") from error

    project = data.get("project", {})
    package_id = str(project.get("package_id", "")).strip()
    project_name = str(project.get("name", "")).strip()
    if not SAFE_ID.fullmatch(package_id) or not project_name:
        raise PipelineError("project.package_id and project.name are required")
    project_root = (manifest_path.parent / str(project.get("root", "."))).resolve()
    if not project_root.is_dir():
        raise PipelineError(f"project root does not exist: {project_root}")
    try:
        manifest_parent = manifest_path.parent.relative_to(project_root)
    except ValueError as error:
        raise PipelineError("project.root must contain the artwork manifest") from error
    if len(manifest_parent.parts) > 1:
        raise PipelineError("project.root may be at most one directory above the manifest directory")

    providers = data.get("providers", {})
    scenario = providers.get("scenario", {}) if isinstance(providers, dict) else {}
    if not isinstance(scenario, dict):
        raise PipelineError("providers.scenario must be a table")
    scenario_poll_seconds = float(scenario.get("poll_seconds", 3))
    scenario_max_wait_seconds = int(scenario.get("max_wait_seconds", 900))
    default_candidates = int(scenario.get("candidates", 4))
    if not 0.5 <= scenario_poll_seconds <= 60 or not 30 <= scenario_max_wait_seconds <= 7200:
        raise PipelineError("Scenario polling settings are outside safe limits")
    if not 1 <= default_candidates <= 8:
        raise PipelineError("providers.scenario.candidates must be between 1 and 8")

    requests: dict[str, Request] = {}
    for raw in data.get("requests", []):
        request_id = str(raw.get("id", "")).strip()
        if not SAFE_ID.fullmatch(request_id) or request_id in requests:
            raise PipelineError(f"request id is empty or duplicated: {request_id!r}")
        width, height = int(raw.get("width", 0)), int(raw.get("height", 0))
        fraction = float(raw.get("occupied_fraction", 0.8))
        if width <= 0 or height <= 0 or width * height > MAX_CANVAS_PIXELS or not 0.1 <= fraction <= 1.0:
            raise PipelineError(f"invalid canvas contract for request {request_id}")
        fit = str(raw.get("fit", "contain"))
        output_mode = str(raw.get("output_mode", "RGBA"))
        background_removal = str(raw.get("background_removal", "none"))
        if (
            fit not in {"contain", "cover"}
            or output_mode not in {"RGB", "RGBA"}
            or background_removal not in {"none", "light-checkerboard"}
        ):
            raise PipelineError(f"request {request_id} has an unsupported fit, output_mode, or background_removal")
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            raise PipelineError(f"request {request_id} has no prompt")

        saturation_min = float(raw.get("accent_saturation_min", 0.2))
        if not math.isfinite(saturation_min) or not 0 <= saturation_min <= 1:
            raise PipelineError(f"request {request_id} has an invalid accent_saturation_min")
        references = tuple(str(value) for value in raw.get("references", []))
        if any(not SAFE_ID.fullmatch(value) or value == request_id for value in references):
            raise PipelineError(f"request {request_id} has an invalid reference")
        raw_generation = raw.get("generation")
        generation = None
        if raw_generation is not None:
            if not isinstance(raw_generation, dict):
                raise PipelineError(f"request {request_id} generation must be a table")
            provider = str(raw_generation.get("provider", "scenario"))
            model_id = str(raw_generation.get("model_id", "")).strip()
            aspect_ratio_value = raw_generation.get("aspect_ratio")
            aspect_ratio = str(aspect_ratio_value).strip() if aspect_ratio_value is not None else None
            candidates = int(raw_generation.get("candidates", default_candidates))
            parameters = raw_generation.get("parameters", {})
            reference_parameter_value = raw_generation.get("reference_parameter")
            reference_parameter = str(reference_parameter_value).strip() if reference_parameter_value else None
            raw_external_references = raw_generation.get("external_references", [])
            if provider != "scenario" or not model_id:
                raise PipelineError(f"request {request_id} has an incomplete Scenario generation contract")
            if not 1 <= candidates <= 8 or not isinstance(parameters, dict):
                raise PipelineError(f"request {request_id} has invalid generation candidates or parameters")
            if any(not isinstance(key, str) for key in parameters):
                raise PipelineError(f"request {request_id} generation parameter names must be strings")
            if any(key in parameters for key in ("referenceImages", "images", "image")):
                raise PipelineError(f"request {request_id} must declare image references outside opaque parameters")
            if not isinstance(raw_external_references, list):
                raise PipelineError(f"request {request_id} external_references must be an array")
            external_references: list[ExternalReference] = []
            for reference in raw_external_references:
                if not isinstance(reference, dict):
                    raise PipelineError(f"request {request_id} has a malformed external reference")
                receipt = str(reference.get("receipt", ""))
                asset_id = str(reference.get("asset_id", ""))
                source_sha256 = str(reference.get("source_sha256", ""))
                if not SAFE_ID.fullmatch(receipt) or not SAFE_ID.fullmatch(asset_id) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                    raise PipelineError(f"request {request_id} has an invalid external reference")
                external_references.append(ExternalReference(receipt, asset_id, source_sha256))
            if (references or external_references) and not reference_parameter:
                raise PipelineError(f"request {request_id} generation must name its reference_parameter")
            generation = Generation(
                provider,
                model_id,
                aspect_ratio,
                candidates,
                parameters,
                reference_parameter,
                tuple(external_references),
            )

        raw_outputs = raw.get("outputs", [])
        if not isinstance(raw_outputs, list) or len(raw_outputs) > MAX_OUTPUTS:
            raise PipelineError(f"request {request_id} has too many or malformed outputs")
        outputs: list[Output] = []
        for raw_output in raw_outputs:
            if not isinstance(raw_output, dict):
                raise PipelineError(f"request {request_id} has a malformed output")
            relative = Path(str(raw_output.get("path", "")))
            if not relative.as_posix() or relative.is_absolute() or ".." in relative.parts:
                raise PipelineError(f"request {request_id} has unsafe output path: {relative}")
            color_value = raw_output.get("accent_color")
            outputs.append(Output(relative, _hex_color(str(color_value)) if color_value else None))
        if not outputs or len({output.path for output in outputs}) != len(outputs):
            raise PipelineError(f"request {request_id} must have unique outputs")
        requests[request_id] = Request(
            request_id,
            str(raw.get("title", request_id)).strip(),
            width,
            height,
            fraction,
            bool(raw.get("alpha_required", True)),
            background_removal,
            fit,
            output_mode,
            saturation_min,
            references,
            generation,
            prompt,
            tuple(outputs),
        )
    if not requests:
        raise PipelineError("manifest must define at least one request")
    for request in requests.values():
        missing_references = set(request.references) - requests.keys()
        if missing_references:
            raise PipelineError(f"request {request.id} references an unknown request: {missing_references.pop()}")
    return Manifest(
        manifest_path,
        package_id,
        project_name,
        project_root,
        scenario_poll_seconds,
        scenario_max_wait_seconds,
        requests,
    )


def state_root(manifest: Manifest, override: Path | None) -> Path:
    if override:
        return override.expanduser().absolute()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "rw-art-pipeline" / manifest.package_id


def _prepare_state_root(manifest: Manifest, root: Path, create: bool) -> Path:
    """Require a marked, non-symlink archive before any managed deletion."""
    if root.is_symlink():
        raise PipelineError(f"state directory may not be a symlink: {root}")
    existed = root.exists()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise PipelineError(f"state directory does not exist: {root}")
    canonical = root.resolve()
    if create:
        os.chmod(canonical, 0o700)
    marker = canonical / ".rw-art-pipeline-state"
    expected = manifest.package_id + "\n"
    if marker.exists():
        if marker.is_symlink() or marker.read_text(encoding="utf-8") != expected:
            raise PipelineError(f"state marker does not match {manifest.package_id}: {marker}")
    elif create:
        if existed and any(canonical.iterdir()):
            raise PipelineError(f"refusing to adopt a non-empty unmarked state directory: {canonical}")
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(expected)
        except FileExistsError as error:
            raise PipelineError(f"state marker was replaced while creating it: {marker}") from error
    else:
        raise PipelineError(f"state archive has not been initialized by intake: {canonical}")
    return canonical


def _state_path(root: Path, *parts: str | Path) -> Path:
    path = root.joinpath(*parts)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise PipelineError(f"managed state path may not be a symlink: {current}")
    if not path.parent.resolve().is_relative_to(root):
        raise PipelineError(f"managed state path escapes the archive: {path}")
    return path


def request_for(manifest: Manifest, request_id: str) -> Request:
    try:
        return manifest.requests[request_id]
    except KeyError as error:
        raise PipelineError(f"unknown request {request_id!r}; choose: {', '.join(manifest.requests)}") from error


def _prompt_hash(request: Request) -> str:
    return hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()


def _contract_hash(request: Request) -> str:
    contract = {
        "processing_version": PROCESSING_VERSION,
        "id": request.id,
        "title": request.title,
        "width": request.width,
        "height": request.height,
        "occupied_fraction": request.occupied_fraction,
        "alpha_required": request.alpha_required,
        "background_removal": request.background_removal,
        "fit": request.fit,
        "output_mode": request.output_mode,
        "accent_saturation_min": request.accent_saturation_min,
        "references": request.references,
        "generation": (
            {
                "provider": request.generation.provider,
                "model_id": request.generation.model_id,
                "aspect_ratio": request.generation.aspect_ratio,
                "candidates": request.generation.candidates,
                "parameters": request.generation.parameters,
                "reference_parameter": request.generation.reference_parameter,
                "external_references": [
                    {
                        "receipt": reference.receipt,
                        "asset_id": reference.asset_id,
                        "source_sha256": reference.source_sha256,
                    }
                    for reference in request.generation.external_references
                ],
            }
            if request.generation
            else None
        ),
        "prompt": request.prompt,
        "outputs": [
            {"path": output.path.as_posix(), "accent_color": output.accent_color}
            for output in request.outputs
        ],
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_png(path: Path) -> None:
    """Require a complete PNG chunk stream with valid CRCs and terminal IEND."""
    try:
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise PipelineError(f"not a PNG file: {path}")
            chunk_count = 0
            saw_idat = False
            while True:
                header = stream.read(8)
                if len(header) != 8:
                    raise PipelineError(f"truncated PNG chunk header: {path}")
                length, chunk_type = struct.unpack(">I4s", header)
                data = stream.read(length)
                checksum = stream.read(4)
                if len(data) != length or len(checksum) != 4:
                    raise PipelineError(f"truncated PNG chunk: {path}")
                expected_crc = struct.unpack(">I", checksum)[0]
                actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    raise PipelineError(f"invalid PNG chunk checksum: {path}")
                chunk_count += 1
                if chunk_count == 1 and chunk_type != b"IHDR":
                    raise PipelineError(f"PNG does not begin with IHDR: {path}")
                saw_idat = saw_idat or chunk_type == b"IDAT"
                if chunk_type == b"IEND":
                    if length or not saw_idat or stream.read(1):
                        raise PipelineError(f"invalid PNG terminal chunk: {path}")
                    return
    except OSError as error:
        raise PipelineError(f"cannot verify PNG {path}: {error}") from error


def _project_path(manifest: Manifest, relative: Path) -> Path:
    """Reject existing symlink parents that would redirect approved artwork."""
    destination = manifest.project_root / relative
    if not destination.parent.resolve().is_relative_to(manifest.project_root):
        raise PipelineError(f"output escapes the project root through a symlink: {relative}")
    if destination.is_symlink():
        raise PipelineError(f"output may not replace a symlink: {destination}")
    return destination


def print_prompt(manifest: Manifest, request_id: str | None) -> None:
    if request_id is None:
        for request in manifest.requests.values():
            print(f"{request.id}: {request.title} ({request.width}x{request.height}, {len(request.outputs)} output(s))")
        return
    request = request_for(manifest, request_id)
    print(f"REQUEST: {request.id}\nTITLE: {request.title}\nPROMPT SHA-256: {_prompt_hash(request)}\n")
    print(request.prompt)
    if request.generation:
        print(
            f"\nAPI GENERATION: {request.generation.candidates} options via "
            f"{request.generation.model_id}; run `rw-art generate`."
        )
    else:
        background = "with transparency and clear transparent margins" if request.alpha_required else "with a fully opaque background"
        print(f"\nEXPECTED DOWNLOAD: one PNG {background}; pass its path explicitly to `rw-art intake`.")


def _recolor_accents(image: Image.Image, color: tuple[int, int, int], minimum: float) -> Image.Image:
    """Move saturated pixels to one target hue while preserving modeled shading."""
    target_hue, target_saturation, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in color))
    result = image.copy()
    pixels = result.load()
    assert pixels is not None
    for y in range(result.height):
        for x in range(result.width):
            red, green, blue, alpha = pixels[x, y]
            hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
            if alpha and saturation >= minimum:
                saturation = max(saturation, target_saturation * 0.75)
                recolored = colorsys.hsv_to_rgb(target_hue, min(saturation, 1.0), value)
                pixels[x, y] = tuple(round(channel * 255) for channel in recolored) + (alpha,)
    return result


def _remove_light_checkerboard(image: Image.Image) -> Image.Image:
    """Make a border-connected, near-neutral light checkerboard transparent.

    Generated sprites sometimes depict transparency instead of encoding alpha.
    Restricting removal to a bright low-chroma region connected to the canvas
    border protects similarly colored highlights enclosed by the subject.
    """
    rgb = image.convert("RGB")
    eligible = Image.new("L", rgb.size, 0)
    rgb_bytes = rgb.tobytes()
    mask = bytearray(len(rgb_bytes) // 3)
    for index in range(0, len(rgb_bytes), 3):
        red, green, blue = rgb_bytes[index : index + 3]
        mask[index // 3] = 255 if min(red, green, blue) >= 225 and max(red, green, blue) - min(red, green, blue) <= 16 else 0
    eligible.frombytes(bytes(mask))
    if eligible.getpixel((0, 0)) != 255:
        raise PipelineError("light-checkerboard removal requires a bright neutral canvas corner")
    ImageDraw.floodfill(eligible, (0, 0), 128, thresh=0)
    removed = eligible.tobytes().count(128)
    if removed < image.width * image.height * 0.05:
        raise PipelineError("light-checkerboard removal found too little border-connected background")
    alpha = eligible.point(lambda value: 0 if value == 128 else 255)
    result = image.convert("RGBA")
    result.putalpha(alpha)
    return result


def _normalize(source: Path, request: Request) -> tuple[Image.Image, dict[str, Any]]:
    """Trim transparent padding, then contain the sprite on its exact game canvas."""
    try:
        with Image.open(source) as opened:
            if getattr(opened, "is_animated", False):
                raise PipelineError("animated images are not supported")
            source_format = opened.format
            had_alpha = "A" in opened.getbands() or "transparency" in opened.info
            if opened.width * opened.height > MAX_SOURCE_PIXELS:
                raise PipelineError(f"source exceeds the {MAX_SOURCE_PIXELS}-pixel safety limit")
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.load()
    except (OSError, Image.DecompressionBombError) as error:
        raise PipelineError(f"cannot decode image {source}: {error}") from error
    removed_background = False
    if request.background_removal == "light-checkerboard" and not had_alpha:
        image = _remove_light_checkerboard(image)
        had_alpha = True
        removed_background = True
    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if request.alpha_required and (not had_alpha or alpha_min == 255):
        raise PipelineError("source has no transparent pixels; regenerate it with a transparent background")
    border = list(alpha.crop((0, 0, image.width, 1)).tobytes())
    border += list(alpha.crop((0, image.height - 1, image.width, image.height)).tobytes())
    border += list(alpha.crop((0, 1, 1, max(1, image.height - 1))).tobytes())
    border += list(alpha.crop((image.width - 1, 1, image.width, max(1, image.height - 1))).tobytes())
    transparent_border_fraction = sum(value < 32 for value in border) / len(border)
    if request.alpha_required and transparent_border_fraction < 0.9:
        raise PipelineError("source lacks clear transparent margins around the sprite")
    if request.output_mode == "RGB" and alpha_min != 255:
        raise PipelineError("RGB output requires a fully opaque source")
    if alpha_max == 0:
        raise PipelineError("source is fully transparent")
    bbox = alpha.point(lambda value: 255 if value > 8 else 0).getbbox()
    if bbox is None:
        raise PipelineError("source has no visible pixels above the alpha threshold")
    cropped = image.crop(bbox)
    if request.fit == "cover":
        scale = max(request.width / cropped.width, request.height / cropped.height)
        resized = cropped.resize(
            (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale))),
            Image.Resampling.LANCZOS,
        )
        left = (resized.width - request.width) // 2
        top = (resized.height - request.height) // 2
        canvas = resized.crop((left, top, left + request.width, top + request.height))
        normalized_size = resized.size
    else:
        maximum = (
            max(1, round(request.width * request.occupied_fraction)),
            max(1, round(request.height * request.occupied_fraction)),
        )
        cropped.thumbnail(maximum, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (request.width, request.height), (0, 0, 0, 0))
        position = ((request.width - cropped.width) // 2, (request.height - cropped.height) // 2)
        canvas.alpha_composite(cropped, position)
        normalized_size = cropped.size
    return canvas, {
        "source_format": source_format,
        "source_size": list(image.size),
        "source_alpha_extrema": [alpha_min, alpha_max],
        "removed_light_checkerboard": removed_background,
        "transparent_border_fraction": transparent_border_fraction,
        "visible_bounds": list(bbox),
        "normalized_visible_size": list(normalized_size),
        "canvas_size": [request.width, request.height],
    }


def _checkerboard(size: tuple[int, int], cell: int = 8) -> Image.Image:
    image = Image.new("RGBA", size, "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            fill = "#d8d8d8" if (x // cell + y // cell) % 2 else "#f4f4f4"
            draw.rectangle((x, y, min(x + cell, size[0]), min(y + cell, size[1])), fill=fill)
    return image


def _write_contact_sheet(request: Request, candidates: list[tuple[Output, Path]], destination: Path) -> None:
    label_height, gap = 28, 16
    tile_width = request.width
    sheet = Image.new(
        "RGB",
        (gap + len(candidates) * (tile_width + gap), request.height + label_height + gap * 2),
        "#252525",
    )
    draw = ImageDraw.Draw(sheet)
    for index, (output, path) in enumerate(candidates):
        x, y = gap + index * (tile_width + gap), gap + label_height
        background = _checkerboard((request.width, request.height))
        with Image.open(path) as candidate:
            background.alpha_composite(candidate.convert("RGBA"))
        sheet.paste(background.convert("RGB"), (x, y))
        draw.text((x, gap), output.path.name, fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True)


def _render_outputs(normalized: Image.Image, request: Request, candidate_root: Path) -> list[tuple[Output, Path]]:
    """Render one normalized source into every declared game-output variant."""
    candidates: list[tuple[Output, Path]] = []
    for output in request.outputs:
        candidate = candidate_root / output.path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        image = normalized if output.accent_color is None else _recolor_accents(
            normalized, output.accent_color, request.accent_saturation_min
        )
        image.convert(request.output_mode).save(candidate, format="PNG", optimize=True)
        candidates.append((output, candidate))
    return candidates


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_option_sheet(
    request: Request,
    options: list[list[tuple[Output, Path]]],
    destination: Path,
) -> None:
    """Show provider choices as columns and final output variants as rows."""
    label_width, label_height, gap = 150, 28, 12
    width = label_width + gap + len(options) * (request.width + gap)
    height = label_height + gap + len(request.outputs) * (request.height + gap)
    sheet = Image.new("RGB", (width, height), "#252525")
    draw = ImageDraw.Draw(sheet)
    for option_index in range(len(options)):
        x = label_width + gap + option_index * (request.width + gap)
        draw.text((x, 8), f"OPTION {option_index + 1}", fill="white")
    for output_index, output in enumerate(request.outputs):
        y = label_height + gap + output_index * (request.height + gap)
        draw.text((8, y + 8), output.path.name, fill="white")
        for option_index, candidates in enumerate(options):
            x = label_width + gap + option_index * (request.width + gap)
            background = _checkerboard((request.width, request.height))
            with Image.open(candidates[output_index][1]) as candidate:
                background.alpha_composite(candidate.convert("RGBA"))
            sheet.paste(background.convert("RGB"), (x, y))
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True)


def _reference_receipts(request: Request, root: Path) -> dict[str, str]:
    receipts: dict[str, str] = {}
    for reference in request.references:
        path = _state_path(root, "reports", f"{reference}.json")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            receipts[reference] = str(report["source_sha256"])
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as error:
            raise PipelineError(f"reference {reference} must complete intake first") from error
    return receipts


def _verify_archived_source(report: dict[str, Any], root: Path) -> None:
    try:
        path = Path(str(report["archived_source"]))
        expected = str(report["source_sha256"])
    except KeyError as error:
        raise PipelineError("receipt has no archived-source provenance") from error
    raw_root = _state_path(root, "raw").resolve()
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(raw_root):
        raise PipelineError(f"archived source is missing or outside managed state: {path}")
    if _file_hash(path) != expected or path.stem != expected:
        raise PipelineError(f"archived source no longer matches its receipt: {path}")


def _archive_source(source: Path, root: Path, request: Request) -> tuple[Path, str]:
    raw_dir = _state_path(root, "raw", request.id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=raw_dir)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_stream, os.fdopen(descriptor, "wb") as destination_stream:
            for block in iter(lambda: source_stream.read(1024 * 1024), b""):
                digest.update(block)
                destination_stream.write(block)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        checksum = digest.hexdigest()
        raw_path = _state_path(root, "raw", request.id, f"{checksum}{source.suffix.lower() or '.image'}")
        if raw_path.exists():
            if raw_path.is_symlink() or not raw_path.is_file() or _file_hash(raw_path) != checksum:
                raise PipelineError(f"raw archive object is corrupt: {raw_path}")
        else:
            try:
                os.link(temporary, raw_path)
            except FileExistsError:
                if raw_path.is_symlink() or _file_hash(raw_path) != checksum:
                    raise PipelineError(f"raw archive object changed during intake: {raw_path}")
        return raw_path, checksum
    finally:
        temporary.unlink(missing_ok=True)


def intake(manifest: Manifest, request_id: str, source: Path, root: Path) -> None:
    """Archive one explicit download and regenerate all declared candidates."""
    request = request_for(manifest, request_id)
    root = _prepare_state_root(manifest, root, create=True)
    source = source.expanduser().absolute()
    if not source.is_file() or source.is_symlink():
        raise PipelineError(f"source must be a regular file: {source}")
    references = _reference_receipts(request, root)
    raw_path, digest = _archive_source(source, root, request)

    normalized, report = _normalize(raw_path, request)
    candidate_root = _state_path(root, "candidates", request.id)
    if candidate_root.exists():
        if candidate_root.is_symlink() or not candidate_root.is_dir():
            raise PipelineError(f"candidate path is not a managed directory: {candidate_root}")
        shutil.rmtree(candidate_root)
    candidates = _render_outputs(normalized, request, candidate_root)

    output_receipts = [
        {"path": output.path.as_posix(), "sha256": _file_hash(candidate)}
        for output, candidate in candidates
    ]
    report.update(
        {
            "request": request.id,
            "prompt_sha256": _prompt_hash(request),
            "contract_sha256": _contract_hash(request),
            "source_sha256": digest,
            "references": references,
            "archived_source": str(raw_path),
            "outputs": output_receipts,
        }
    )
    report_path = _state_path(root, "reports", f"{request.id}.json")
    _atomic_json(report_path, report)
    sheet_path = _state_path(root, "reviews", f"{request.id}.png")
    _write_contact_sheet(request, candidates, sheet_path)
    print(f"Archived source: {raw_path}\nReport: {report_path}\nReview sheet: {sheet_path}")


def _generation_reference_assets(request: Request, root: Path) -> dict[str, str]:
    assets: dict[str, str] = {}
    for reference in request.references:
        report_path = _state_path(root, "reports", f"{reference}.json")
        report = _load_json(report_path, f"reference receipt for {reference}")
        provider = report.get("provider", {})
        asset_id_value = provider.get("asset_id") if isinstance(provider, dict) else None
        if not isinstance(asset_id_value, str) or not asset_id_value:
            raise PipelineError(f"reference {reference} has no Scenario asset ID; regenerate or upload it first")
        _verify_archived_source(report, root)
        assets[reference] = asset_id_value
    if request.generation:
        for reference in request.generation.external_references:
            report_path = _state_path(root, "reports", f"{reference.receipt}.json")
            report = _load_json(report_path, f"external reference receipt for {reference.receipt}")
            provider = report.get("provider", {})
            if (
                report.get("source_sha256") != reference.source_sha256
                or not isinstance(provider, dict)
                or provider.get("asset_id") != reference.asset_id
            ):
                raise PipelineError(f"external reference {reference.receipt} no longer matches its bound provenance")
            _verify_archived_source(report, root)
            assets[f"external:{reference.receipt}"] = reference.asset_id
    return assets


def _generation_contract(request: Request, root: Path) -> str:
    static = _contract_hash(request)
    if not request.references and not (request.generation and request.generation.external_references):
        return static
    bound = {
        "static": static,
        "source_hashes": _reference_receipts(request, root),
        "scenario_assets": _generation_reference_assets(request, root),
    }
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _generation_payload(request: Request, root: Path) -> dict[str, Any]:
    assert request.generation is not None
    payload = dict(request.generation.parameters)
    payload["prompt"] = request.prompt
    if request.generation.aspect_ratio:
        payload["aspectRatio"] = request.generation.aspect_ratio
    if request.references or request.generation.external_references:
        assert request.generation.reference_parameter is not None
        payload[request.generation.reference_parameter] = list(_generation_reference_assets(request, root).values())
    return payload


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(f"{description} is missing or invalid: {path}") from error
    if not isinstance(value, dict):
        raise PipelineError(f"{description} must be a JSON object: {path}")
    return value


def _scenario_client() -> ScenarioClient:
    try:
        return ScenarioClient(load_credentials())
    except ScenarioError as error:
        raise PipelineError(str(error)) from error


def scenario_auth(provider: str) -> None:
    if provider != "scenario":
        raise PipelineError("only the scenario credential provider is supported")
    try:
        path = store_credentials_interactive()
    except ScenarioError as error:
        raise PipelineError(str(error)) from error
    print(f"Stored Scenario credentials with mode 0600: {path}")


def scenario_models(filter_text: str | None) -> None:
    client = _scenario_client()
    needle = filter_text.lower() if filter_text else None
    found: list[tuple[str, str]] = []
    token = None
    for _page in range(20):
        try:
            response = client.list_models(token)
        except ScenarioError as error:
            raise PipelineError(str(error)) from error
        records = response.get("models", response.get("items", []))
        if not isinstance(records, list):
            raise PipelineError("Scenario public model response has no model list")
        for value in records:
            if not isinstance(value, dict):
                continue
            identifier = value.get("id")
            name = value.get("name") or value.get("title") or ""
            if isinstance(identifier, str) and isinstance(name, str):
                text = f"{identifier} {name}".lower()
                if needle is None or needle in text:
                    found.append((identifier, name))
        token_value = response.get("nextPaginationToken") or response.get("nextPageToken")
        token = token_value if isinstance(token_value, str) and token_value else None
        if not token:
            break
    for identifier, name in sorted(set(found), key=lambda item: (item[1].lower(), item[0])):
        print(f"{identifier}\t{name}")
    if not found:
        print("No matching public Scenario models.")


def _model_parameter_names(response: dict[str, Any]) -> set[str]:
    """Find the model's largest documented parameter list without logging metadata."""
    candidates: list[set[str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            names = {
                str(item["name"])
                for item in value
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if "prompt" in names:
                candidates.append(names)
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)

    visit(response)
    return max(candidates, key=len) if candidates else set()


def _validate_model_payload(client: ScenarioClient, request: Request, payload: dict[str, Any]) -> list[str]:
    assert request.generation is not None
    try:
        response = client.get_model(request.generation.model_id)
    except ScenarioError as error:
        raise PipelineError(str(error)) from error
    names = _model_parameter_names(response)
    if not names:
        raise PipelineError(
            f"Scenario returned no parameter schema for {request.generation.model_id}; inspect it through MCP before generation"
        )
    unknown = set(payload) - names
    if unknown:
        raise PipelineError(
            f"Scenario model {request.generation.model_id} does not declare parameters: {', '.join(sorted(unknown))}"
        )
    return sorted(names)


def _estimate_cost(response: dict[str, Any]) -> float | None:
    for key in ("creativeUnitsCost", "computeUnitsCost", "estimatedCost", "credits"):
        value = response.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    preferred = {"cost", "credits", "computeunits", "computecost", "estimatedcost", "creativeunitscost", "cucost"}
    found: list[float] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if normalized in preferred and isinstance(child, (int, float)) and not isinstance(child, bool):
                    found.append(float(child))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(response)
    return found[0] if found else None


@contextmanager
def _generation_lock(root: Path, request_id: str):
    lock_path = _state_path(root, "locks", f"{request_id}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError(f"another generation process already owns {request_id}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _resolve_asset(client: ScenarioClient, asset: dict[str, str]) -> tuple[str | None, str]:
    identifier = asset.get("id")
    url = asset.get("url")
    if not url and identifier:
        url = asset_url(client.get_asset(identifier))
    if not url:
        raise PipelineError("Scenario completed a job without an asset URL")
    return identifier, url


def generate_scenario(
    manifest: Manifest,
    request_id: str,
    root: Path,
    estimate_only: bool,
    restart: bool,
    confirm_cost: bool = False,
    client: ScenarioClient | None = None,
    retry_failed: bool = False,
) -> None:
    root = _prepare_state_root(manifest, root, create=True)
    with _generation_lock(root, request_id):
        _generate_scenario_locked(
            manifest,
            request_id,
            root,
            estimate_only,
            restart,
            confirm_cost,
            client,
            retry_failed,
        )


def _generate_scenario_locked(
    manifest: Manifest,
    request_id: str,
    root: Path,
    estimate_only: bool,
    restart: bool,
    confirm_cost: bool,
    client: ScenarioClient | None,
    retry_failed: bool,
) -> None:
    """Submit or resume one bounded Scenario batch and prepare final-size options."""
    request = request_for(manifest, request_id)
    if request.generation is None:
        raise PipelineError(f"request {request.id} has no API generation contract")
    if estimate_only and (restart or retry_failed):
        raise PipelineError("--estimate-only cannot be combined with --restart or --retry-failed")
    if restart and retry_failed:
        raise PipelineError("--restart and --retry-failed cannot be combined")
    generation_path = _state_path(root, "generations", f"{request.id}.json")
    generation_root = _state_path(root, "options", request.id)
    if restart:
        generation_path.unlink(missing_ok=True)
        if generation_root.exists():
            if generation_root.is_symlink() or not generation_root.is_dir():
                raise PipelineError(f"option path is not a managed directory: {generation_root}")
            shutil.rmtree(generation_root)
    client = client or _scenario_client()
    payload = _generation_payload(request, root)
    contract = _generation_contract(request, root)
    schema_parameters = _validate_model_payload(client, request, payload)

    if estimate_only:
        try:
            estimate = client.submit_generation(request.generation.model_id, payload, dry_run=True)
        except ScenarioError as error:
            raise PipelineError(str(error)) from error
        unit_cost = _estimate_cost(estimate)
        print(json.dumps(estimate, indent=2))
        if unit_cost is None:
            print(f"Batch size: {request.generation.candidates}; Scenario did not expose a recognized numeric cost field.")
        else:
            print(
                f"Estimated batch total: {unit_cost * request.generation.candidates:g} CU "
                f"({unit_cost:g} CU x {request.generation.candidates} options)"
            )
        return

    if generation_path.exists():
        report = _load_json(generation_path, "Scenario generation receipt")
        if report.get("contract_sha256") != contract:
            raise PipelineError("generation contract changed; pass --restart to start a new paid batch")
    else:
        if retry_failed:
            raise PipelineError("cannot retry failed options without an existing generation receipt")
        report = {
            "schema_version": 1,
            "request": request.id,
            "contract_sha256": contract,
            "provider": "scenario",
            "model_id": request.generation.model_id,
            "payload": payload,
            "candidate_count": request.generation.candidates,
            "schema_parameters": schema_parameters,
            "dry_run": None,
            "estimated_at": None,
            "estimated_unit_cost": None,
            "estimated_batch_cost": None,
            "jobs": [],
            "options": [],
        }
        _atomic_json(generation_path, report)

    jobs = report.get("jobs")
    if not isinstance(jobs, list):
        raise PipelineError(f"Scenario generation receipt has malformed jobs: {generation_path}")
    ambiguous = [job for job in jobs if job.get("status") in {"submitting", "ambiguous"} and not job.get("job_id")]
    if ambiguous:
        raise PipelineError(
            "a paid Scenario submission has an unknown outcome; reconcile recent jobs in Scenario, then use --restart explicitly"
        )

    if retry_failed:
        terminal_failure = {"failed", "failure", "cancelled", "canceled"}
        failed = [job for job in jobs if job.get("status") in terminal_failure]
        if not failed:
            raise PipelineError("generation receipt has no failed options to retry")
        try:
            estimate = client.submit_generation(request.generation.model_id, payload, dry_run=True)
        except ScenarioError as error:
            raise PipelineError(str(error)) from error
        unit_cost = _estimate_cost(estimate)
        retry_total = unit_cost * len(failed) if unit_cost is not None else None
        report["retry_estimate"] = {
            "response": estimate,
            "estimated_at": datetime.now(UTC).isoformat(),
            "failed_options": [job.get("index") for job in failed],
            "estimated_unit_cost": unit_cost,
            "estimated_total_cost": retry_total,
        }
        _atomic_json(generation_path, report)
        total_text = f"{retry_total:g} CU" if retry_total is not None else "unavailable from the response"
        print(f"Estimated failed-option retry total: {total_text} for {len(failed)} option(s).")
        if not confirm_cost:
            print("Re-run with --retry-failed --confirm-cost to submit only the replacement job(s).")
            return

        for slot in failed:
            attempts = slot.setdefault("failed_attempts", [])
            if not isinstance(attempts, list):
                raise PipelineError("Scenario generation receipt has malformed failed attempts")
            attempts.append({"job_id": slot.get("job_id"), "status": slot.get("status")})
            for key in ("job_id", "asset_id", "asset_url"):
                slot.pop(key, None)
            slot["status"] = "submitting"
            _atomic_json(generation_path, report)
            try:
                response = client.submit_generation(request.generation.model_id, payload)
                slot["job_id"] = job_id(response)
                slot["status"] = "submitted"
                _atomic_json(generation_path, report)
            except ScenarioError as error:
                slot["status"] = "ambiguous"
                _atomic_json(generation_path, report)
                raise PipelineError(
                    "Scenario replacement submission outcome is ambiguous; no retry was attempted. "
                    "Inspect recent Scenario jobs before taking further action."
                ) from error

    if not jobs:
        try:
            estimate = client.submit_generation(request.generation.model_id, payload, dry_run=True)
        except ScenarioError as error:
            raise PipelineError(str(error)) from error
        unit_cost = _estimate_cost(estimate)
        report["dry_run"] = estimate
        report["estimated_at"] = datetime.now(UTC).isoformat()
        report["estimated_unit_cost"] = unit_cost
        report["estimated_batch_cost"] = unit_cost * request.generation.candidates if unit_cost is not None else None
        _atomic_json(generation_path, report)
        total = report["estimated_batch_cost"]
        total_text = f"{total:g} CU" if isinstance(total, (int, float)) else "unavailable from the response"
        if not confirm_cost:
            print(
                f"Fresh Scenario estimate stored. Batch total: {total_text} for "
                f"{request.generation.candidates} options. Re-run with --confirm-cost to submit paid jobs."
            )
            return

    try:
        while len(jobs) < request.generation.candidates:
            slot = {"index": len(jobs) + 1, "status": "submitting"}
            jobs.append(slot)
            _atomic_json(generation_path, report)
            try:
                response = client.submit_generation(request.generation.model_id, payload)
                slot["job_id"] = job_id(response)
                slot["status"] = "submitted"
                _atomic_json(generation_path, report)
            except ScenarioError as error:
                slot["status"] = "ambiguous"
                _atomic_json(generation_path, report)
                raise PipelineError(
                    "Scenario submission outcome is ambiguous; no retry was attempted. "
                    "Inspect recent Scenario jobs before using --restart."
                ) from error
    except ScenarioError as error:
        raise PipelineError(str(error)) from error

    deadline = time.monotonic() + manifest.scenario_max_wait_seconds
    terminal_success = {"success", "succeeded", "completed"}
    terminal_failure = {"failed", "failure", "cancelled", "canceled"}
    while True:
        pending = False
        for job in jobs:
            if job.get("status") in terminal_success:
                continue
            try:
                response = client.get_job(str(job["job_id"]))
                status = job_status(response)
            except (KeyError, ScenarioError) as error:
                raise PipelineError(f"cannot poll Scenario job: {error}") from error
            job["status"] = status
            if status in terminal_failure:
                _atomic_json(generation_path, report)
                raise PipelineError(f"Scenario option {job.get('index')} ended with status {status}")
            if status in terminal_success:
                assets = result_assets(response)
                if not assets:
                    raise PipelineError(f"Scenario option {job.get('index')} completed without an asset")
                identifier, url = _resolve_asset(client, assets[0])
                job["asset_id"] = identifier
                job["asset_url"] = url
                job["status"] = status
            else:
                pending = True
            _atomic_json(generation_path, report)
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise PipelineError(f"Scenario jobs are still running; rerun generate to resume: {generation_path}")
        time.sleep(manifest.scenario_poll_seconds)

    if generation_root.exists():
        if generation_root.is_symlink() or not generation_root.is_dir():
            raise PipelineError(f"option path is not a managed directory: {generation_root}")
        shutil.rmtree(generation_root)
    option_sets: list[list[tuple[Output, Path]]] = []
    option_reports: list[dict[str, Any]] = []
    for job in jobs:
        option_index = int(job["index"])
        try:
            if job.get("asset_id"):
                job["asset_url"] = asset_url(client.get_asset(str(job["asset_id"])))
            else:
                refreshed = client.get_job(str(job["job_id"]))
                assets = result_assets(refreshed)
                if not assets:
                    raise PipelineError(f"Scenario option {option_index} no longer exposes its completed asset")
                identifier, refreshed_url = _resolve_asset(client, assets[0])
                job["asset_id"] = identifier
                job["asset_url"] = refreshed_url
            _atomic_json(generation_path, report)
        except ScenarioError as error:
            raise PipelineError(f"cannot refresh Scenario option {option_index}: {error}") from error
        incoming_dir = _state_path(root, "incoming", request.id)
        incoming_dir.mkdir(parents=True, exist_ok=True)
        incoming = incoming_dir / f"option-{option_index}.source"
        try:
            client.download(str(job["asset_url"]), incoming)
        except ScenarioError as error:
            raise PipelineError(str(error)) from error
        raw_path, digest = _archive_source(incoming, root, request)
        incoming.unlink(missing_ok=True)
        normalized, processing = _normalize(raw_path, request)
        option_root = generation_root / f"option-{option_index}"
        candidates = _render_outputs(normalized, request, option_root)
        outputs = [
            {"path": output.path.as_posix(), "sha256": _file_hash(path)}
            for output, path in candidates
        ]
        option_sets.append(candidates)
        option_reports.append(
            {
                "index": option_index,
                "job_id": job["job_id"],
                "asset_id": job.get("asset_id"),
                "source_sha256": digest,
                "archived_source": str(raw_path),
                "processing": processing,
                "outputs": outputs,
            }
        )
    report["options"] = option_reports
    _atomic_json(generation_path, report)
    sheet_path = _state_path(root, "reviews", f"{request.id}-options.png")
    _write_option_sheet(request, option_sets, sheet_path)
    print(f"Scenario options ready: {sheet_path}\nGeneration receipt: {generation_path}")


def select_option(manifest: Manifest, request_id: str, option_number: int, root: Path) -> None:
    """Make one provider option the ordinary approval candidate set."""
    request = request_for(manifest, request_id)
    root = _prepare_state_root(manifest, root, create=False)
    generation_path = _state_path(root, "generations", f"{request.id}.json")
    generation = _load_json(generation_path, "Scenario generation receipt")
    if generation.get("contract_sha256") != _generation_contract(request, root):
        raise PipelineError("generation contract changed; regenerate before selecting")
    options = generation.get("options", [])
    if not isinstance(options, list):
        raise PipelineError("Scenario generation receipt has malformed options")
    selected = next((option for option in options if option.get("index") == option_number), None)
    if not isinstance(selected, dict):
        raise PipelineError(f"option {option_number} is not available; choose 1-{len(options)}")
    _verify_archived_source(selected, root)
    try:
        receipts = {item["path"]: item["sha256"] for item in selected["outputs"]}
    except (KeyError, TypeError) as error:
        raise PipelineError("selected Scenario option has malformed output receipts") from error

    option_root = _state_path(root, "options", request.id, f"option-{option_number}")
    candidate_root = _state_path(root, "candidates", request.id)
    if candidate_root.exists():
        if candidate_root.is_symlink() or not candidate_root.is_dir():
            raise PipelineError(f"candidate path is not a managed directory: {candidate_root}")
        shutil.rmtree(candidate_root)
    candidates: list[tuple[Output, Path]] = []
    for output in request.outputs:
        source = option_root / output.path
        expected = receipts.get(output.path.as_posix())
        if not source.is_file() or source.is_symlink() or _file_hash(source) != expected:
            raise PipelineError(f"selected option output is missing or changed: {source}")
        destination = candidate_root / output.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        candidates.append((output, destination))

    references = _reference_receipts(request, root)
    report = dict(selected.get("processing", {}))
    report.update(
        {
            "request": request.id,
            "prompt_sha256": _prompt_hash(request),
            "contract_sha256": _contract_hash(request),
            "source_sha256": selected["source_sha256"],
            "references": references,
            "archived_source": selected["archived_source"],
            "outputs": selected["outputs"],
            "provider": {
                "name": "scenario",
                "model_id": generation.get("model_id"),
                "job_id": selected.get("job_id"),
                "asset_id": selected.get("asset_id"),
                "option": option_number,
            },
        }
    )
    report_path = _state_path(root, "reports", f"{request.id}.json")
    _atomic_json(report_path, report)
    sheet_path = _state_path(root, "reviews", f"{request.id}.png")
    _write_contact_sheet(request, candidates, sheet_path)
    print(f"Selected Scenario option {option_number}\nApproval sheet: {sheet_path}")


def reject_batch(manifest: Manifest, request_id: str, reason: str, root: Path) -> None:
    """Archive a rejected provider batch before allowing a replacement contract."""
    request = request_for(manifest, request_id)
    root = _prepare_state_root(manifest, root, create=False)
    with _generation_lock(root, request.id):
        generation_path = _state_path(root, "generations", f"{request.id}.json")
        report = _load_json(generation_path, "Scenario generation receipt")
        jobs = report.get("jobs", [])
        if not isinstance(jobs, list):
            raise PipelineError("Scenario generation receipt has malformed jobs")
        terminal = {"success", "succeeded", "completed", "failure", "failed", "cancelled", "canceled"}
        if any(not isinstance(job, dict) or job.get("status") not in terminal for job in jobs):
            raise PipelineError("cannot reject a batch with pending or ambiguous paid submissions")
        successful = all(job.get("status") in {"success", "succeeded", "completed"} for job in jobs)
        options = report.get("options", [])
        if successful and (not isinstance(options, list) or len(options) != report.get("candidate_count")):
            raise PipelineError("cannot reject a successful batch before all option provenance is complete")
        timestamp = datetime.now(UTC)
        history_name = f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{str(report.get('contract_sha256', 'unknown'))[:12]}"
        report["decision"] = {
            "status": "rejected",
            "reason": reason.strip() or "rejected without a recorded reason",
            "at": timestamp.isoformat(),
        }
        history_root = _state_path(root, "history", request.id)
        history_path = history_root / f"{history_name}.json"
        _atomic_json(history_path, report)
        review = _state_path(root, "reviews", f"{request.id}-options.png")
        if review.is_file() and not review.is_symlink():
            history_root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(review, history_root / f"{history_name}.png")
            review.unlink()
        job_ids = {str(job.get("job_id")) for job in jobs if job.get("job_id")}
        selected_report_path = _state_path(root, "reports", f"{request.id}.json")
        if selected_report_path.is_file() and not selected_report_path.is_symlink():
            selected_report = _load_json(selected_report_path, "selected candidate receipt")
            provider = selected_report.get("provider", {})
            selected_job = str(provider.get("job_id")) if isinstance(provider, dict) and provider.get("job_id") else None
            if selected_job in job_ids:
                _atomic_json(history_root / f"{history_name}-selection.json", selected_report)
                selected_report_path.unlink()
                selected_review = _state_path(root, "reviews", f"{request.id}.png")
                if selected_review.is_file() and not selected_review.is_symlink():
                    shutil.copyfile(selected_review, history_root / f"{history_name}-selection.png")
                    selected_review.unlink()
                candidates = _state_path(root, "candidates", request.id)
                if candidates.exists():
                    if candidates.is_symlink() or not candidates.is_dir():
                        raise PipelineError(f"candidate path is not a managed directory: {candidates}")
                    shutil.rmtree(candidates)
        options = _state_path(root, "options", request.id)
        if options.exists():
            if options.is_symlink() or not options.is_dir():
                raise PipelineError(f"option path is not a managed directory: {options}")
            shutil.rmtree(options)
        generation_path.unlink()
        print(f"Rejected batch archived: {history_path}")


def approve(manifest: Manifest, request_id: str, root: Path, replace: bool) -> None:
    """Copy a complete reviewed candidate set into the project atomically per file."""
    request = request_for(manifest, request_id)
    root = _prepare_state_root(manifest, root, create=False)
    candidate_root = _state_path(root, "candidates", request.id)
    report_path = _state_path(root, "reports", f"{request.id}.json")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(f"intake receipt is missing or invalid: {report_path}") from error
    if report.get("contract_sha256") != _contract_hash(request):
        raise PipelineError("the request contract changed after intake; run intake again before approval")
    if report.get("references") != _reference_receipts(request, root):
        raise PipelineError("a referenced source changed after intake; run intake again before approval")
    _verify_archived_source(report, root)
    try:
        receipts = {item["path"]: item["sha256"] for item in report["outputs"]}
    except (KeyError, TypeError) as error:
        raise PipelineError(f"intake receipt has malformed output hashes: {report_path}") from error
    pairs = [(candidate_root / output.path, _project_path(manifest, output.path)) for output in request.outputs]
    missing = [source for source, _ in pairs if not source.is_file()]
    existing = [destination for _, destination in pairs if destination.exists()]
    if missing:
        raise PipelineError(f"candidate set is incomplete; run intake first: {missing[0]}")
    if existing and not replace:
        raise PipelineError(f"approved output already exists; inspect it or pass --replace: {existing[0]}")
    for output, (source, _) in zip(request.outputs, pairs):
        if receipts.get(output.path.as_posix()) != _file_hash(source):
            raise PipelineError(f"candidate changed after intake; run intake again: {source}")
    for output, (source, destination) in zip(request.outputs, pairs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        try:
            with source.open("rb") as source_stream, os.fdopen(descriptor, "wb") as destination_stream:
                for block in iter(lambda: source_stream.read(1024 * 1024), b""):
                    digest.update(block)
                    destination_stream.write(block)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            if digest.hexdigest() != receipts[output.path.as_posix()]:
                raise PipelineError(f"candidate changed during approval; run intake again: {source}")
            if replace:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as error:
                    raise PipelineError(f"approved output appeared during approval: {destination}") from error
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Approved: {destination}")


def validate(manifest: Manifest) -> None:
    """Require every promoted output to match its declared PNG canvas contract."""
    failures: list[str] = []
    for request in manifest.requests.values():
        for output in request.outputs:
            path = _project_path(manifest, output.path)
            try:
                if path.is_symlink():
                    raise OSError("symlinks are not accepted")
                _verify_png(path)
                with Image.open(path) as image:
                    image.load()
                    if image.format != "PNG" or image.mode != request.output_mode or image.size != (request.width, request.height):
                        failures.append(
                            f"{path}: expected {request.output_mode} PNG {request.width}x{request.height}, got {image.format} {image.mode} {image.size}"
                        )
            except (OSError, PipelineError) as error:
                failures.append(f"{path}: {error}")
    if failures:
        raise PipelineError("approved artwork validation failed:\n" + "\n".join(failures))
    print(f"Artwork validation succeeded: {manifest.project_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="override the local raw/candidate/review archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prompt_parser = subparsers.add_parser("prompt", help="list requests or print one generation prompt")
    prompt_parser.add_argument("manifest", type=Path)
    prompt_parser.add_argument("request", nargs="?")
    intake_parser = subparsers.add_parser("intake", help="archive and process one explicit Web UI download")
    intake_parser.add_argument("manifest", type=Path)
    intake_parser.add_argument("request")
    intake_parser.add_argument("source", type=Path)
    auth_parser = subparsers.add_parser("auth", help="store provider credentials in a private local file")
    auth_parser.add_argument("manifest", type=Path)
    auth_parser.add_argument("provider", choices=("scenario",))
    models_parser = subparsers.add_parser("models", help="list Scenario model IDs available to the account")
    models_parser.add_argument("manifest", type=Path)
    models_parser.add_argument("filter", nargs="?")
    generate_parser = subparsers.add_parser("generate", help="submit or resume a provider candidate batch")
    generate_parser.add_argument("manifest", type=Path)
    generate_parser.add_argument("request")
    generate_parser.add_argument("--estimate-only", action="store_true")
    generate_parser.add_argument("--confirm-cost", action="store_true", help="submit paid jobs after refreshing the batch estimate")
    generate_parser.add_argument("--restart", action="store_true", help="discard the receipt and submit a new paid batch")
    generate_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="estimate or explicitly replace only terminal failed jobs in an existing receipt",
    )
    select_parser = subparsers.add_parser("select", help="select one provider option for ordinary approval")
    select_parser.add_argument("manifest", type=Path)
    select_parser.add_argument("request")
    select_parser.add_argument("option", type=int)
    reject_parser = subparsers.add_parser("reject", help="archive and reject a provider option batch")
    reject_parser.add_argument("manifest", type=Path)
    reject_parser.add_argument("request")
    reject_parser.add_argument("reason")
    approve_parser = subparsers.add_parser("approve", help="promote one reviewed candidate set into the mod")
    approve_parser.add_argument("manifest", type=Path)
    approve_parser.add_argument("request")
    approve_parser.add_argument("--replace", action="store_true")
    validate_parser = subparsers.add_parser("validate", help="validate all promoted outputs")
    validate_parser.add_argument("manifest", type=Path)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        root = state_root(manifest, args.state_dir)
        if args.command == "prompt":
            print_prompt(manifest, args.request)
        elif args.command == "intake":
            intake(manifest, args.request, args.source, root)
        elif args.command == "auth":
            scenario_auth(args.provider)
        elif args.command == "models":
            scenario_models(args.filter)
        elif args.command == "generate":
            generate_scenario(
                manifest,
                args.request,
                root,
                args.estimate_only,
                args.restart,
                args.confirm_cost,
                retry_failed=args.retry_failed,
            )
        elif args.command == "select":
            select_option(manifest, args.request, args.option, root)
        elif args.command == "reject":
            reject_batch(manifest, args.request, args.reason, root)
        elif args.command == "approve":
            approve(manifest, args.request, root, args.replace)
        elif args.command == "validate":
            validate(manifest)
    except PipelineError as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
