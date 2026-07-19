import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rw_art_pipeline.cli import (
    PipelineError,
    approve,
    generate_scenario,
    intake,
    load_manifest,
    reject_batch,
    select_option,
    validate,
)
from rw_art_pipeline.scenario import ScenarioError


MANIFEST = """
[project]
package_id = "Tests.Art"
name = "Test Art"
root = "."

[[requests]]
id = "machine"
title = "Machine"
width = 64
height = 64
occupied_fraction = 0.5
alpha_required = true
accent_saturation_min = 0.2
prompt = "Generate a transparent test machine."
outputs = [
  { path = "Textures/MachineAmber.png", accent_color = "#c79e38" },
  { path = "Textures/MachineBlue.png", accent_color = "#468dd1" },
]
"""


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest_path = self.root / "manifest.toml"
        self.manifest_path.write_text(MANIFEST, encoding="utf-8")
        self.state = self.root / "state"
        self.manifest = load_manifest(self.manifest_path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_intake_recolors_and_approve_promotes_exact_canvases(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
        for y in range(20, 80):
            for x in range(50, 150):
                image.putpixel((x, y), (190, 120, 30, 255))
        image.save(source)

        intake(self.manifest, "machine", source, self.state)
        approve(self.manifest, "machine", self.state, replace=False)
        validate(self.manifest)

        amber = Image.open(self.root / "Textures/MachineAmber.png")
        blue = Image.open(self.root / "Textures/MachineBlue.png")
        self.assertEqual((64, 64), amber.size)
        self.assertEqual("RGBA", blue.mode)
        self.assertNotEqual(amber.getpixel((32, 32)), blue.getpixel((32, 32)))

    def test_opaque_source_is_rejected(self):
        source = self.root / "opaque.png"
        Image.new("RGB", (64, 64), "white").save(source)
        with self.assertRaisesRegex(PipelineError, "no transparent pixels"):
            intake(self.manifest, "machine", source, self.state)

    def test_baked_light_checkerboard_can_be_removed_explicitly(self):
        manifest_path = self.root / "checkerboard.toml"
        manifest_path.write_text(
            MANIFEST.replace("alpha_required = true", 'alpha_required = true\nbackground_removal = "light-checkerboard"'),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)
        source = self.root / "checkerboard.png"
        image = Image.new("RGB", (128, 128), "#ffffff")
        for y in range(0, 128, 16):
            for x in range(0, 128, 16):
                if (x // 16 + y // 16) % 2:
                    for py in range(y, y + 16):
                        for px in range(x, x + 16):
                            image.putpixel((px, py), (240, 240, 240))
        for y in range(32, 96):
            for x in range(32, 96):
                image.putpixel((x, y), (80, 80, 80))
        image.putpixel((64, 64), (245, 245, 245))
        image.save(source)

        intake(manifest, "machine", source, self.state)
        candidate_path = self.state / "candidates/machine/Textures/MachineAmber.png"
        with Image.open(candidate_path) as candidate:
            self.assertEqual(0, candidate.getpixel((0, 0))[3])
            self.assertEqual(255, candidate.getpixel((32, 32))[3])

    def test_approve_does_not_replace_existing_output_by_default(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        intake(self.manifest, "machine", source, self.state)
        destination = self.root / "Textures/MachineAmber.png"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")
        with self.assertRaisesRegex(PipelineError, "already exists"):
            approve(self.manifest, "machine", self.state, replace=False)

    def test_cover_request_fills_an_opaque_rgb_canvas(self):
        manifest_path = self.root / "preview.toml"
        manifest_path.write_text(
            MANIFEST.replace(
                'id = "machine"',
                'id = "preview"\nfit = "cover"\noutput_mode = "RGB"',
            ).replace("alpha_required = true", "alpha_required = false")
            .replace('path = "Textures/MachineAmber.png"', 'path = "About/PreviewA.png"')
            .replace('path = "Textures/MachineBlue.png"', 'path = "About/PreviewB.png"'),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)
        source = self.root / "wide.jpg"
        Image.new("RGB", (300, 100), "#776655").save(source)
        intake(manifest, "preview", source, self.state)
        with Image.open(self.state / "candidates/preview/About/PreviewA.png") as candidate:
            self.assertEqual((64, 64), candidate.size)
            self.assertEqual("RGB", candidate.mode)

    def test_candidate_symlink_cannot_redirect_cleanup(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        intake(self.manifest, "machine", source, self.state)
        candidate_root = self.state / "candidates/machine"
        shutil.rmtree(candidate_root)
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("keep", encoding="utf-8")
        candidate_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(PipelineError, "symlink"):
            intake(self.manifest, "machine", source, self.state)
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_nonempty_unmarked_state_directory_is_not_adopted(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        important = self.state / "candidates/machine/important"
        important.parent.mkdir(parents=True)
        important.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(PipelineError, "refusing to adopt"):
            intake(self.manifest, "machine", source, self.state)
        self.assertEqual("keep", important.read_text(encoding="utf-8"))

    def test_contract_change_invalidates_intake_receipt(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        intake(self.manifest, "machine", source, self.state)
        self.manifest_path.write_text(MANIFEST.replace("occupied_fraction = 0.5", "occupied_fraction = 0.4"), encoding="utf-8")

        with self.assertRaisesRegex(PipelineError, "contract changed"):
            approve(load_manifest(self.manifest_path), "machine", self.state, replace=False)

    def test_corrupt_raw_archive_object_is_rejected(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        intake(self.manifest, "machine", source, self.state)
        report = json.loads((self.state / "reports/machine.json").read_text(encoding="utf-8"))
        Path(report["archived_source"]).write_bytes(b"corrupt")

        with self.assertRaisesRegex(PipelineError, "raw archive object is corrupt"):
            intake(self.manifest, "machine", source, self.state)

    def test_validation_fully_decodes_png(self):
        source = self.root / "download.png"
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.putpixel((32, 32), (200, 100, 20, 255))
        image.save(source)
        intake(self.manifest, "machine", source, self.state)
        approve(self.manifest, "machine", self.state, replace=False)
        output = self.root / "Textures/MachineAmber.png"
        output.write_bytes(output.read_bytes()[:-12])

        with self.assertRaisesRegex(PipelineError, "validation failed"):
            validate(self.manifest)

    def test_scenario_batch_produces_four_selectable_finalized_options(self):
        manifest_path = self.root / "scenario.toml"
        manifest_path.write_text(
            MANIFEST.replace(
                'prompt = "Generate a transparent test machine."',
                'generation = { provider = "scenario", model_id = "model_test", aspect_ratio = "1:1", candidates = 4 }\n'
                'prompt = "Generate a transparent test machine."',
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)

        class FakeScenarioClient:
            def __init__(self):
                self.submitted = 0

            def submit_generation(self, model_id, payload, dry_run=False):
                if dry_run:
                    return {"credits": 4}
                self.submitted += 1
                return {"job": {"id": f"job_{self.submitted}", "status": "queued"}}

            def get_model(self, model_id):
                return {
                    "parameters": [
                        {"name": "prompt"},
                        {"name": "aspectRatio"},
                    ]
                }

            def get_job(self, job_id):
                return {
                    "job": {
                        "status": "success",
                        "result": {"images": [{"url": f"https://assets.example/{job_id}.png"}]},
                    }
                }

            def get_asset(self, asset_id):
                raise AssertionError("URL results should not fetch asset metadata")

            def download(self, url, destination):
                option = int(url.split("job_")[1].split(".")[0])
                image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
                for y in range(24, 104):
                    for x in range(24, 104):
                        image.putpixel((x, y), (160 + option, 100, 20, 255))
                image.save(destination, format="PNG")

        client = FakeScenarioClient()
        generate_scenario(manifest, "machine", self.state, False, False, True, client=client)
        self.assertEqual(4, client.submitted)
        self.assertTrue((self.state / "reviews/machine-options.png").is_file())
        select_option(manifest, "machine", 2, self.state)
        approve(manifest, "machine", self.state, replace=False)
        validate(manifest)
        receipt = json.loads((self.state / "reports/machine.json").read_text(encoding="utf-8"))
        self.assertEqual(2, receipt["provider"]["option"])
        self.assertEqual("job_2", receipt["provider"]["job_id"])

    def test_ambiguous_paid_submission_is_not_retried_or_resubmitted(self):
        manifest_path = self.root / "scenario.toml"
        manifest_path.write_text(
            MANIFEST.replace(
                'prompt = "Generate a transparent test machine."',
                'generation = { provider = "scenario", model_id = "model_test", aspect_ratio = "1:1", candidates = 4 }\n'
                'prompt = "Generate a transparent test machine."',
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)

        class AmbiguousClient:
            def __init__(self):
                self.paid_submissions = 0

            def get_model(self, model_id):
                return {"parameters": [{"name": "prompt"}, {"name": "aspectRatio"}]}

            def submit_generation(self, model_id, payload, dry_run=False):
                if dry_run:
                    return {"credits": 1}
                self.paid_submissions += 1
                raise ScenarioError("connection dropped after submission")

        client = AmbiguousClient()
        with self.assertRaisesRegex(PipelineError, "outcome is ambiguous"):
            generate_scenario(manifest, "machine", self.state, False, False, True, client=client)
        self.assertEqual(1, client.paid_submissions)
        with self.assertRaisesRegex(PipelineError, "pending or ambiguous"):
            reject_batch(manifest, "machine", "discard it", self.state)
        self.assertTrue((self.state / "generations/machine.json").is_file())
        with self.assertRaisesRegex(PipelineError, "unknown outcome"):
            generate_scenario(manifest, "machine", self.state, False, False, True, client=client)
        self.assertEqual(1, client.paid_submissions)

    def test_failed_option_requires_explicit_estimate_and_retry(self):
        manifest_path = self.root / "scenario.toml"
        manifest_path.write_text(
            MANIFEST.replace(
                'prompt = "Generate a transparent test machine."',
                'generation = { provider = "scenario", model_id = "model_test", candidates = 4 }\n'
                'prompt = "Generate a transparent test machine."',
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)

        class FailedOptionClient:
            def __init__(self):
                self.paid_submissions = 0
                self.estimates = 0

            def get_model(self, model_id):
                return {"parameters": [{"name": "prompt"}]}

            def submit_generation(self, model_id, payload, dry_run=False):
                if dry_run:
                    self.estimates += 1
                    return {"credits": 3}
                self.paid_submissions += 1
                return {"job": {"id": f"job_{self.paid_submissions}"}}

            def get_job(self, job_id):
                if job_id == "job_2":
                    return {"job": {"status": "failure"}}
                return {
                    "job": {
                        "status": "success",
                        "result": {"images": [{"url": f"https://assets.example/{job_id}.png"}]},
                    }
                }

            def get_asset(self, asset_id):
                raise AssertionError("URL results should not fetch asset metadata")

            def download(self, url, destination):
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                image.putpixel((32, 32), (200, 100, 20, 255))
                image.save(destination, format="PNG")

        client = FailedOptionClient()
        with self.assertRaisesRegex(PipelineError, "option 2 ended with status failure"):
            generate_scenario(manifest, "machine", self.state, False, False, True, client=client)
        self.assertEqual(4, client.paid_submissions)

        generate_scenario(manifest, "machine", self.state, False, False, False, client=client, retry_failed=True)
        self.assertEqual(4, client.paid_submissions)
        generate_scenario(manifest, "machine", self.state, False, False, True, client=client, retry_failed=True)

        self.assertEqual(5, client.paid_submissions)
        self.assertTrue((self.state / "reviews/machine-options.png").is_file())
        receipt = json.loads((self.state / "generations/machine.json").read_text(encoding="utf-8"))
        retried = receipt["jobs"][1]
        self.assertEqual("job_5", retried["job_id"])
        self.assertEqual([{"job_id": "job_2", "status": "failure"}], retried["failed_attempts"])

    def test_rejected_scenario_batch_preserves_history(self):
        manifest_path = self.root / "scenario.toml"
        manifest_path.write_text(
            MANIFEST.replace(
                'prompt = "Generate a transparent test machine."',
                'generation = { provider = "scenario", model_id = "model_test", aspect_ratio = "1:1", candidates = 1 }\n'
                'prompt = "Generate a transparent test machine."',
            ),
            encoding="utf-8",
        )
        manifest = load_manifest(manifest_path)

        class FakeClient:
            def get_model(self, model_id):
                return {"parameters": [{"name": "prompt"}, {"name": "aspectRatio"}]}

            def submit_generation(self, model_id, payload, dry_run=False):
                return {"credits": 1} if dry_run else {"job": {"id": "job_1"}}

            def get_job(self, job_id):
                return {"job": {"status": "success", "result": {"images": [{"url": "https://cdn.cloud.scenario.com/a"}]}}}

            def download(self, url, destination):
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                image.putpixel((32, 32), (200, 100, 20, 255))
                image.save(destination, format="PNG")

        generate_scenario(manifest, "machine", self.state, False, False, True, client=FakeClient())
        select_option(manifest, "machine", 1, self.state)
        reject_batch(manifest, "machine", "wrong silhouette", self.state)
        self.assertFalse((self.state / "generations/machine.json").exists())
        self.assertFalse((self.state / "reports/machine.json").exists())
        with self.assertRaisesRegex(PipelineError, "receipt is missing"):
            approve(manifest, "machine", self.state, replace=False)
        history = list((self.state / "history/machine").glob("*.json"))
        batch_history = [path for path in history if not path.name.endswith("-selection.json")]
        self.assertEqual(1, len(batch_history))
        self.assertEqual("wrong silhouette", json.loads(batch_history[0].read_text())["decision"]["reason"])


if __name__ == "__main__":
    unittest.main()
