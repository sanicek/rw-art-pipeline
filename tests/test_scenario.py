import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from rw_art_pipeline import __version__
from rw_art_pipeline.scenario import (
    ScenarioClient,
    ScenarioCredentials,
    ScenarioError,
    USER_AGENT,
    job_id,
    job_status,
    load_credentials,
    result_assets,
    store_credentials_interactive,
    _SafeAssetRedirect,
)


class ScenarioTests(unittest.TestCase):
    def test_user_agent_matches_package_version(self):
        self.assertEqual(f"rw-art-pipeline/{__version__}", USER_AGENT)
        client = ScenarioClient(ScenarioCredentials("key", "secret"))
        self.assertEqual(USER_AGENT, client.headers["User-Agent"])

    def test_environment_credentials_take_precedence(self):
        with patch.dict(os.environ, {"SCENARIO_API_KEY": "key", "SCENARIO_API_SECRET": "secret"}, clear=False):
            credentials = load_credentials()
        self.assertEqual("key", credentials.key)
        self.assertEqual("secret", credentials.secret)

    def test_interactive_credentials_are_stored_user_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}, clear=False):
                with patch("getpass.getpass", side_effect=("key", "secret")):
                    path = store_credentials_interactive()
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual({"api_key": "key", "api_secret": "secret"}, json.loads(path.read_text()))

    def test_permissive_credential_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rw-art-pipeline/scenario.json"
            path.parent.mkdir()
            path.write_text('{"api_key":"key","api_secret":"secret"}', encoding="utf-8")
            path.chmod(0o644)
            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": temporary, "SCENARIO_API_KEY": "", "SCENARIO_API_SECRET": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(ScenarioError, "mode 0600"):
                    load_credentials()

    def test_job_and_asset_shapes_are_normalized(self):
        response = {
            "job": {
                "jobId": "job_1",
                "status": "SUCCESS",
                "metadata": {"assetIds": ["asset_1"]},
                "result": {"images": [{"assetId": "asset_2", "url": "https://assets.example/2.png"}]},
            }
        }
        self.assertEqual("job_1", job_id(response))
        self.assertEqual("success", job_status(response))
        self.assertEqual(
            [{"id": "asset_1"}, {"id": "asset_2", "url": "https://assets.example/2.png"}],
            result_assets(response),
        )

    def test_paid_post_is_never_retried_without_idempotency(self):
        client = ScenarioClient(ScenarioCredentials("key", "secret"), retries=3)

        class FailingOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError("ambiguous timeout")

        opener = FailingOpener()
        client.opener = opener
        with self.assertRaisesRegex(ScenarioError, "ambiguous timeout"):
            client.submit_generation("model_test", {"prompt": "test"}, dry_run=False)
        self.assertEqual(1, opener.calls)

    def test_asset_redirects_remain_on_approved_https_cdns(self):
        handler = _SafeAssetRedirect()
        request = urllib.request.Request("https://cdn.cloud.scenario.com/assets/a")
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://cdn.cloud.scenario.com/assets-transform/a?signature=test",
        )
        self.assertEqual("cdn.cloud.scenario.com", urllib.parse.urlparse(redirected.full_url).hostname)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/private")
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
