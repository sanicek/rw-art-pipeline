import hashlib
import io
import json
import re
import subprocess
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from generate_generic_workbench import draw_cube_mask, draw_cube_texture  # noqa: E402


class TemplateTests(unittest.TestCase):
    """The bundled catalog is a stable byte-level distribution contract."""

    def test_catalog_exposes_templates_and_sanicek_badge_variants(self):
        catalog = templates()
        self.assertEqual(
            ["generic-cube-workbench-1x1", "generic-desk-workbench-1x1", "generic-workbench-1x1", "sanicek-badge"],
            [template.id for template in catalog],
        )
        badge = get_template("sanicek-badge")
        self.assertEqual("MIT", badge.license)
        self.assertEqual({"source", "rimworld-mod-icon"}, {variant.id for variant in badge.variants})

    def test_catalog_exposes_generic_workbench_variants(self):
        for template_id in ("generic-workbench-1x1", "generic-desk-workbench-1x1", "generic-cube-workbench-1x1"):
            workbench = get_template(template_id)
            self.assertEqual("MIT", workbench.license)
            self.assertEqual(
                {"source", "rimworld-texture", "rimworld-color-mask"},
                {variant.id for variant in workbench.variants},
            )

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

    def test_workbench_variants_match_their_exact_png_contracts(self):
        expected = {
            "source": ((1024, 1024), "e726175885f6e712d074175121f62499feb82eda5a67f972f9e50f4c12abfc85"),
            "rimworld-texture": ((128, 128), "c97a8c4684f2aa0107091167a3bb2e44955eba6cffffbd6f7723237449309846"),
            "rimworld-color-mask": ((128, 128), "697218e1916b90e71ecde69a269ca3e35d78307411897a16f30f3ca3943e605c"),
        }
        for variant_id, (size, digest) in expected.items():
            payload = template_bytes("generic-workbench-1x1", variant_id)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                self.assertEqual(("PNG", "RGBA", size), (image.format, image.mode, image.size))

    def test_workbench_mask_recolors_frame_and_surface_only(self):
        with Image.open(io.BytesIO(template_bytes("generic-workbench-1x1", "rimworld-texture"))) as texture:
            texture.load()
            texture_alpha = texture.getchannel("A").tobytes()
        with Image.open(io.BytesIO(template_bytes("generic-workbench-1x1", "rimworld-color-mask"))) as mask:
            mask.load()
            self.assertEqual(texture_alpha, mask.getchannel("A").tobytes())
            self.assertEqual((0, 0), mask.getchannel("B").getextrema())
            self.assertEqual((255, 0, 0), mask.getpixel((18, 50))[:3])
            self.assertEqual((255, 0, 0), mask.getpixel((64, 90))[:3])
            self.assertEqual((0, 255, 0, 255), mask.getpixel((64, 64)))
            for point in ((20, 28), (64, 110), (12, 50), (16, 96), (118, 50)):
                self.assertTrue(all(channel <= 1 for channel in mask.getpixel(point)[:3]))

    def test_desk_workbench_variants_and_mask_contract(self):
        expected = {
            "source": ((1024, 1024), "d74c83dcbd241efa1189704406fe1109ec4428dab9bebf33e17b2c4a5b1fffb1"),
            "rimworld-texture": ((128, 128), "1a2dbfaad2f507040967324c0da63e5b3ad1ebebefd22c81308e9970fdb9681d"),
            "rimworld-color-mask": ((128, 128), "76e4f0a5e3c0b6b83f0df3f9aa276962bb82cc2e39b832115fca6d303488e2e2"),
        }
        for variant_id, (size, digest) in expected.items():
            payload = template_bytes("generic-desk-workbench-1x1", variant_id)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                self.assertEqual(("PNG", "RGBA", size), (image.format, image.mode, image.size))

        with Image.open(io.BytesIO(template_bytes("generic-desk-workbench-1x1", "rimworld-texture"))) as texture:
            texture.load()
            texture_alpha = texture.getchannel("A").tobytes()
        with Image.open(io.BytesIO(template_bytes("generic-desk-workbench-1x1", "rimworld-color-mask"))) as mask:
            mask.load()
            self.assertEqual(texture_alpha, mask.getchannel("A").tobytes())
            self.assertEqual((0, 0), mask.getchannel("B").getextrema())
            self.assertEqual((255, 0, 0), mask.getpixel((14, 50))[:3])
            self.assertEqual((255, 0, 0), mask.getpixel((40, 90))[:3])
            self.assertEqual((0, 255, 0), mask.getpixel((64, 50))[:3])
            for point in ((8, 50), (22, 90), (64, 90), (64, 115)):
                self.assertTrue(all(channel <= 8 for channel in mask.getpixel(point)[:3]))

    def test_cube_candidate_is_symmetric_and_uses_expected_mask_channels(self):
        texture = draw_cube_texture(128)
        mask = draw_cube_mask(texture)
        self.assertEqual((128, 128), texture.size)
        self.assertEqual("RGBA", texture.mode)
        self.assertEqual(texture.getchannel("A").tobytes(), mask.getchannel("A").tobytes())
        self.assertEqual(texture.getchannel("A").tobytes(), texture.transpose(Image.Transpose.FLIP_LEFT_RIGHT).getchannel("A").tobytes())
        self.assertEqual((255, 0, 0), mask.getpixel((23, 50))[:3])
        self.assertEqual((0, 255, 0), mask.getpixel((64, 60))[:3])
        self.assertEqual((255, 0, 0), mask.getpixel((64, 108))[:3])
        self.assertTrue(all(channel <= 8 for channel in mask.getpixel((20, 50))[:3]))

    def test_cube_workbench_variants_match_their_exact_png_contracts(self):
        expected = {
            "source": ((1024, 1024), "81ce69079a90da3f77eeaf70f5355c4b88dc6a1b9a766966a17e606459eab3a9"),
            "rimworld-texture": ((128, 128), "fc3aff67a72a181a35b0dffcccce36a8d14632ddeb47a5cb594e5944421750a2"),
            "rimworld-color-mask": ((128, 128), "d04af9edf226198dbf6dca48571a1b0055f28766e97ee4ff943ff6021da68785"),
        }
        for variant_id, (size, digest) in expected.items():
            payload = template_bytes("generic-cube-workbench-1x1", variant_id)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                self.assertEqual(("PNG", "RGBA", size), (image.format, image.mode, image.size))

    def test_workbench_svg_defines_every_referenced_palette_variable(self):
        root = Path(__file__).resolve().parents[1]
        for source in (
            root / "artwork_sources/generic-workbench-1x1/source.svg",
            root / "artwork_sources/generic-desk-workbench-1x1/source.svg",
            root / "artwork_sources/generic-cube-workbench-1x1/source.svg",
        ):
            svg = source.read_text(encoding="utf-8")
            definitions = set(re.findall(r"(--[a-z-]+)\s*:", svg))
            references = set(re.findall(r"var\((--[a-z-]+)\)", svg))
            self.assertEqual(set(), references - definitions)

    def test_workbench_assets_are_reproducible(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "tools/generate_generic_workbench.py"), "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

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
