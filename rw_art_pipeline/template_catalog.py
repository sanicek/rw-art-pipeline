"""Discover and export immutable artwork templates bundled with rw-art.

Templates are provider-independent package resources. The catalog gives callers
stable semantic IDs while checksums preserve the exact approved PNG bytes across
editable installs, wheels, and exports into unrelated projects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image


class TemplateError(Exception):
    """A malformed bundled template or unsafe export request."""


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TemplateVariant:
    """One exact byte representation of a reusable artwork template."""

    id: str
    role: str
    resource_path: str
    width: int
    height: int
    mode: str
    sha256: str


@dataclass(frozen=True)
class ArtworkTemplate:
    """A named visual identity with one or more purpose-specific variants."""

    id: str
    name: str
    description: str
    license: str
    variants: tuple[TemplateVariant, ...]


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(f"template catalog field {field} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, field: str) -> str:
    identifier = _string(value, field)
    if not SAFE_ID.fullmatch(identifier):
        raise TemplateError(f"template catalog field {field} has an invalid identifier: {identifier!r}")
    return identifier


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TemplateError(f"template catalog field {field} must be a positive integer")
    return value


def _variant(raw: Any, template_id: str) -> TemplateVariant:
    if not isinstance(raw, dict):
        raise TemplateError(f"template {template_id} variants must be objects")
    variant_id = _identifier(raw.get("id"), f"{template_id}.variant.id")
    resource_path = _string(raw.get("path"), f"{template_id}.{variant_id}.path")
    path = PurePosixPath(resource_path)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != template_id
        or path.suffix != ".png"
        or ".." in path.parts
        or "\\" in resource_path
        or "\0" in resource_path
    ):
        raise TemplateError(f"template {template_id}/{variant_id} must use {template_id}/<file>.png")
    digest = _string(raw.get("sha256"), f"{template_id}.{variant_id}.sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise TemplateError(f"template {template_id}/{variant_id} has an invalid SHA-256")
    mode = _string(raw.get("mode"), f"{template_id}.{variant_id}.mode")
    if mode not in {"RGB", "RGBA"}:
        raise TemplateError(f"template {template_id}/{variant_id} uses unsupported mode {mode!r}")
    return TemplateVariant(
        id=variant_id,
        role=_identifier(raw.get("role"), f"{template_id}.{variant_id}.role"),
        resource_path=resource_path,
        width=_positive_integer(raw.get("width"), f"{template_id}.{variant_id}.width"),
        height=_positive_integer(raw.get("height"), f"{template_id}.{variant_id}.height"),
        mode=mode,
        sha256=digest,
    )


@lru_cache(maxsize=1)
def templates() -> tuple[ArtworkTemplate, ...]:
    """Load and validate the versioned bundled template catalog."""
    catalog = resources.files("rw_art_pipeline").joinpath("template_assets", "catalog.json")
    try:
        raw = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TemplateError(f"cannot read bundled template catalog: {error}") from error
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != CATALOG_SCHEMA_VERSION
    ):
        raise TemplateError(f"template catalog must use schema version {CATALOG_SCHEMA_VERSION}")
    entries = raw.get("templates")
    if not isinstance(entries, list):
        raise TemplateError("template catalog templates must be an array")

    parsed: list[ArtworkTemplate] = []
    seen_templates: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TemplateError("template catalog entries must be objects")
        template_id = _identifier(entry.get("id"), "template.id")
        if template_id in seen_templates:
            raise TemplateError(f"template catalog repeats template {template_id!r}")
        seen_templates.add(template_id)
        raw_variants = entry.get("variants")
        if not isinstance(raw_variants, list) or not raw_variants:
            raise TemplateError(f"template {template_id} must define at least one variant")
        variants = tuple(_variant(item, template_id) for item in raw_variants)
        variant_ids = {variant.id for variant in variants}
        if len(variant_ids) != len(variants):
            raise TemplateError(f"template {template_id} repeats a variant ID")
        parsed.append(
            ArtworkTemplate(
                id=template_id,
                name=_string(entry.get("name"), f"{template_id}.name"),
                description=_string(entry.get("description"), f"{template_id}.description"),
                license=_string(entry.get("license"), f"{template_id}.license"),
                variants=variants,
            )
        )
    return tuple(parsed)


def get_template(template_id: str) -> ArtworkTemplate:
    """Resolve one stable template ID or report the available choices."""
    for template in templates():
        if template.id == template_id:
            return template
    available = ", ".join(template.id for template in templates()) or "none"
    raise TemplateError(f"unknown template {template_id!r}; available templates: {available}")


def get_variant(template_id: str, variant_id: str) -> TemplateVariant:
    """Resolve one purpose-specific representation of a template."""
    template = get_template(template_id)
    for variant in template.variants:
        if variant.id == variant_id:
            return variant
    available = ", ".join(variant.id for variant in template.variants)
    raise TemplateError(f"unknown variant {variant_id!r} for {template_id}; available variants: {available}")


def template_bytes(template_id: str, variant_id: str) -> bytes:
    """Return exact validated resource bytes without decoding and re-encoding."""
    variant = get_variant(template_id, variant_id)
    resource = resources.files("rw_art_pipeline").joinpath("template_assets", *PurePosixPath(variant.resource_path).parts)
    try:
        payload = resource.read_bytes()
    except OSError as error:
        raise TemplateError(f"cannot read bundled template {template_id}/{variant_id}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != variant.sha256:
        raise TemplateError(f"bundled template checksum changed: {template_id}/{variant_id}")
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            actual = (image.format, image.mode, image.size)
    except OSError as error:
        raise TemplateError(f"bundled template is not a valid image: {template_id}/{variant_id}") from error
    expected = ("PNG", variant.mode, (variant.width, variant.height))
    if actual != expected:
        raise TemplateError(f"bundled template contract changed: {template_id}/{variant_id}; expected {expected}, got {actual}")
    return payload


def export_template(template_id: str, variant_id: str, destination: Path, replace: bool = False) -> Path:
    """Atomically export exact template bytes without silently replacing files."""
    payload = template_bytes(template_id, variant_id)
    destination = Path(os.path.abspath(destination.expanduser()))
    parent = destination.parent
    try:
        symlinked_ancestor = next((ancestor for ancestor in (parent, *parent.parents) if ancestor.is_symlink()), None)
        if not parent.is_dir() or symlinked_ancestor is not None:
            raise TemplateError(f"template export parent must be an existing real directory: {parent}")
        if destination.is_symlink():
            raise TemplateError(f"template export destination may not be a symlink: {destination}")
        if destination.exists() and (not destination.is_file() or not replace):
            reason = "is not a regular file" if not destination.is_file() else "already exists; pass --replace to overwrite"
            raise TemplateError(f"template export destination {reason}: {destination}")
    except TemplateError:
        raise
    except OSError as error:
        raise TemplateError(f"cannot inspect template export destination {destination}: {error}") from error

    staging_directory: Path | None = None
    temporary: Path | None = None
    try:
        staging_directory = Path(tempfile.mkdtemp(prefix=".rw-art-export-", dir=parent))
        temporary = staging_directory / "template.png"
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise TemplateError(f"template export destination appeared during export: {destination}") from error
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                pass
        except OSError:
            # Publication is already atomic; some filesystems do not support
            # opening or syncing directories, so durability is best-effort.
            pass
        return destination
    except TemplateError:
        raise
    except OSError as error:
        raise TemplateError(f"cannot export template to {destination}: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if staging_directory is not None:
            try:
                staging_directory.rmdir()
            except OSError:
                pass
