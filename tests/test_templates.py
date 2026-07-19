import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rw_art_pipeline.cli import main
from rw_art_pipeline.template_catalog import (
    TemplateError,
    export_template,
    get_template,
    get_variant,
    template_bytes,
    templates,
)


class TemplateTests(unittest.TestCase):
    """The bundled catalog is a stable byte-level distribution contract."""

    def test_catalog_exposes_both_sanicek_badge_variants(self):
        catalog = templates()
        self.assertEqual(["sanicek-badge"], [template.id for template in catalog])
        badge = get_template("sanicek-badge")
        self.assertEqual("MIT", badge.license)
        self.assertEqual({"source", "rimworld-mod-icon"}, {variant.id for variant in badge.variants})

    def test_variants_match_their_exact_png_contracts(self):
        expected = {
            "source": ((1024, 1024), "100e0d38ce7e546ca45bfe5facc3ac6fa8267e214d7f9404121b8a7f81efef75"),
            "rimworld-mod-icon": ((256, 256), "87fb2537410403305cf8970a97046a48e24ca1f7fb645ff32e212206a8eb2d53"),
        }
        for variant_id, (size, digest) in expected.items():
            payload = template_bytes("sanicek-badge", variant_id)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "template.png"
                path.write_bytes(payload)
                with Image.open(path) as image:
                    image.load()
                    self.assertEqual(("PNG", "RGBA", size), (image.format, image.mode, image.size))

    def test_export_preserves_bytes_and_requires_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "badge.png"
            exported = export_template("sanicek-badge", "rimworld-mod-icon", destination)
            self.assertEqual(destination, exported)
            self.assertEqual(template_bytes("sanicek-badge", "rimworld-mod-icon"), destination.read_bytes())
            with self.assertRaisesRegex(TemplateError, "already exists"):
                export_template("sanicek-badge", "rimworld-mod-icon", destination)
            destination.write_bytes(b"old")
            export_template("sanicek-badge", "source", destination, replace=True)
            self.assertEqual(template_bytes("sanicek-badge", "source"), destination.read_bytes())

    def test_export_rejects_symlink_destinations_and_parents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.png"
            destination = root / "badge.png"
            destination.symlink_to(target)
            with self.assertRaisesRegex(TemplateError, "may not be a symlink"):
                export_template("sanicek-badge", "source", destination)

            real_parent = root / "real"
            real_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(TemplateError, "existing real directory"):
                export_template("sanicek-badge", "source", linked_parent / "badge.png")

            nested = real_parent / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(TemplateError, "existing real directory"):
                export_template("sanicek-badge", "source", linked_parent / "nested" / "badge.png")

    def test_unknown_template_and_variant_report_available_ids(self):
        with self.assertRaisesRegex(TemplateError, "sanicek-badge"):
            get_template("missing")
        with self.assertRaisesRegex(TemplateError, "rimworld-mod-icon"):
            get_variant("sanicek-badge", "missing")

    def test_template_commands_do_not_require_scenario_or_a_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "badge.png"
            output = io.StringIO()
            with (
                patch("rw_art_pipeline.cli.load_credentials", side_effect=AssertionError("network path used")),
                patch.object(sys, "argv", ["rw-art", "templates", "list"]),
                redirect_stdout(output),
            ):
                main()
            self.assertIn("sanicek-badge", output.getvalue())
            with (
                patch("rw_art_pipeline.cli.load_credentials", side_effect=AssertionError("network path used")),
                patch.object(
                    sys,
                    "argv",
                    ["rw-art", "templates", "export", "sanicek-badge", "source", str(destination)],
                ),
                redirect_stdout(io.StringIO()),
            ):
                main()
            self.assertEqual(template_bytes("sanicek-badge", "source"), destination.read_bytes())

    def test_filesystem_errors_are_reported_as_template_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / ("x" * 300)
            with self.assertRaisesRegex(TemplateError, "cannot export template"):
                export_template("sanicek-badge", "source", destination)

    def test_catalog_contains_no_provider_specific_asset_ids(self):
        catalog = resources.files("rw_art_pipeline").joinpath("template_assets", "catalog.json")
        raw = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertNotIn("asset_", json.dumps(raw))


if __name__ == "__main__":
    unittest.main()
