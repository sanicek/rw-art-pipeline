"""Scenario credentials and asynchronous REST access.

The provider adapter owns authentication, HTTP retries, and response-shape
normalization. It intentionally knows nothing about RimWorld paths or image
processing, allowing the shared pipeline to retain a provider-neutral release
boundary while Scenario evolves its model catalog independently.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import random
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__


class ScenarioError(Exception):
    """A safe-to-display Scenario configuration or API failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authenticated API requests must never forward Basic credentials."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


SCENARIO_ASSET_HOSTS = {"cdn.cloud.scenario.com", "cdn.scenario.com", "media.scenario.com"}
USER_AGENT = f"rw-art-pipeline/{__version__}"


class _SafeAssetRedirect(urllib.request.HTTPRedirectHandler):
    """Follow CDN redirects only when they remain on an approved HTTPS host."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        parsed = urllib.parse.urlparse(new_url)
        if parsed.scheme != "https" or parsed.hostname not in SCENARIO_ASSET_HOSTS:
            raise urllib.error.HTTPError(new_url, code, "unsafe Scenario asset redirect", headers, file_pointer)
        return urllib.request.Request(new_url, headers={"User-Agent": USER_AGENT}, method=request.get_method())


@dataclass(frozen=True)
class ScenarioCredentials:
    key: str
    secret: str


def credentials_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "rw-art-pipeline" / "scenario.json"


def load_credentials() -> ScenarioCredentials:
    """Prefer ephemeral environment credentials, then a private local file."""
    key = os.environ.get("SCENARIO_API_KEY", "").strip()
    secret = os.environ.get("SCENARIO_API_SECRET", "").strip()
    if key or secret:
        if not key or not secret:
            raise ScenarioError("SCENARIO_API_KEY and SCENARIO_API_SECRET must both be set")
        return ScenarioCredentials(key, secret)

    path = credentials_path()
    if path.is_symlink():
        raise ScenarioError(f"Scenario credential file may not be a symlink: {path}")
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ScenarioError(f"Scenario credential file must use mode 0600: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        key, secret = str(data["api_key"]).strip(), str(data["api_secret"]).strip()
    except FileNotFoundError as error:
        raise ScenarioError(
            "Scenario credentials are not configured; export SCENARIO_API_KEY and "
            "SCENARIO_API_SECRET or run `rw-art auth scenario`"
        ) from error
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ScenarioError(f"cannot read Scenario credentials from {path}: {error}") from error
    if not key or not secret:
        raise ScenarioError(f"Scenario credential file is incomplete: {path}")
    return ScenarioCredentials(key, secret)


def store_credentials_interactive() -> Path:
    """Prompt without echo and atomically install a user-only credential file."""
    key = getpass.getpass("Scenario API key: ").strip()
    secret = getpass.getpass("Scenario API secret: ").strip()
    if not key or not secret:
        raise ScenarioError("both Scenario API key and secret are required")
    path = credentials_path()
    parent = path.parent
    if parent.is_symlink():
        raise ScenarioError(f"credential directory may not be a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    if path.is_symlink():
        raise ScenarioError(f"credential file may not be a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".scenario.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"api_key": key, "api_secret": secret}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class ScenarioClient:
    """Small standard-library client for Scenario's trigger/job/asset lifecycle."""

    def __init__(
        self,
        credentials: ScenarioCredentials,
        base_url: str = "https://api.cloud.scenario.com/v1",
        timeout: float = 60,
        retries: int = 3,
    ) -> None:
        token = base64.b64encode(f"{credentials.key}:{credentials.secret}".encode()).decode()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        attempts = self.retries + 1 if retry else 1
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, headers=self.headers, method=method)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(10 * 1024 * 1024 + 1)
                    if len(raw) > 10 * 1024 * 1024:
                        raise ScenarioError("Scenario JSON response exceeded 10 MiB")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ScenarioError("Scenario returned a non-object JSON response")
                    return parsed
            except urllib.error.HTTPError as error:
                detail = error.read(4096).decode("utf-8", "replace")
                if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                    raise ScenarioError(f"Scenario API {method} {path} failed ({error.code}): {detail}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == attempts - 1:
                    raise ScenarioError(f"Scenario API {method} {path} failed: {error}") from error
            time.sleep(min(8.0, (2**attempt) + random.random()))
        raise AssertionError("retry loop did not return or raise")

    def list_models(self, pagination_token: str | None = None) -> dict[str, Any]:
        query = {"privacy": "public"}
        if pagination_token:
            query["paginationToken"] = pagination_token
        return self._request("GET", "models", query=query)

    def get_model(self, model_id: str) -> dict[str, Any]:
        return self._request("GET", f"models/{urllib.parse.quote(model_id, safe='')}")

    def submit_generation(self, model_id: str, payload: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        model = urllib.parse.quote(model_id, safe="")
        query = {"dryRun": "true"} if dry_run else None
        return self._request("POST", f"generate/custom/{model}", payload, query, retry=dry_run)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs/{urllib.parse.quote(job_id, safe='')}")

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._request("GET", f"assets/{urllib.parse.quote(asset_id, safe='')}")

    def download(self, url: str, destination: Path) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in SCENARIO_ASSET_HOSTS:
            raise ScenarioError(f"Scenario asset URL is not on an approved HTTPS CDN: {url!r}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        descriptor, temporary_name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with urllib.request.build_opener(_SafeAssetRedirect()).open(request, timeout=self.timeout) as response, os.fdopen(descriptor, "wb") as stream:
                final_url = urllib.parse.urlparse(response.geturl())
                if final_url.scheme != "https" or final_url.hostname not in SCENARIO_ASSET_HOSTS:
                    raise ScenarioError("Scenario asset download left the approved HTTPS CDN")
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > 100 * 1024 * 1024:
                        raise ScenarioError("Scenario asset download exceeded 100 MiB")
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if total == 0:
                raise ScenarioError("Scenario returned an empty asset")
            os.replace(temporary, destination)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ScenarioError(f"cannot download Scenario asset: {error}") from error
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)


def job_id(response: dict[str, Any]) -> str:
    job = response.get("job", response)
    if not isinstance(job, dict):
        raise ScenarioError("Scenario generation response has no job object")
    value = job.get("jobId") or job.get("id")
    if not isinstance(value, str) or not value:
        raise ScenarioError("Scenario generation response has no job ID")
    return value


def job_status(response: dict[str, Any]) -> str:
    job = response.get("job", response)
    if not isinstance(job, dict) or not isinstance(job.get("status"), str):
        raise ScenarioError("Scenario job response has no status")
    return str(job["status"]).lower()


def result_assets(response: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize asset IDs and URLs from documented and model-specific results."""
    job = response.get("job", response)
    if not isinstance(job, dict):
        return []
    assets: list[dict[str, str]] = []

    metadata = job.get("metadata", {})
    if isinstance(metadata, dict):
        for value in metadata.get("assetIds", []) or []:
            if isinstance(value, str):
                assets.append({"id": value})

    result = job.get("result", {})
    values: list[Any] = []
    direct_assets = job.get("assets", [])
    values.extend(direct_assets if isinstance(direct_assets, list) else [direct_assets])
    if isinstance(result, dict):
        for key in ("images", "assets", "assetIds"):
            candidate = result.get(key, [])
            values.extend(candidate if isinstance(candidate, list) else [candidate])
    elif isinstance(result, list):
        values.extend(result)
    for value in values:
        if isinstance(value, str):
            if value.startswith("https://"):
                assets.append({"url": value})
            else:
                assets.append({"id": value})
        elif isinstance(value, dict):
            normalized = {}
            identifier = value.get("assetId") or value.get("id")
            url = value.get("url")
            if isinstance(identifier, str):
                normalized["id"] = identifier
            if isinstance(url, str):
                normalized["url"] = url
            if normalized:
                assets.append(normalized)

    unique: list[dict[str, str]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for asset in assets:
        identity = (asset.get("id"), asset.get("url"))
        if identity not in seen:
            seen.add(identity)
            unique.append(asset)
    return unique


def asset_url(response: dict[str, Any]) -> str:
    asset = response.get("asset", response)
    if not isinstance(asset, dict) or not isinstance(asset.get("url"), str):
        raise ScenarioError("Scenario asset response has no download URL")
    return str(asset["url"])
