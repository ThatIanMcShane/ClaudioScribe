"""Plaud API client — downloads recordings via the unofficial consumer API."""

import logging

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.plaud.ai"


class PlaudClient:
    """Wraps the Plaud consumer API.

    Handles the region-redirect pattern: regional endpoints (api-euc1, api-use1)
    may return status=-302 with a "domains" redirect to api.plaud.ai.
    """

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        # Token from localStorage already includes "bearer " prefix
        if not self.token.lower().startswith("bearer "):
            self.token = f"bearer {self.token}"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": self.token})

    def _request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        """Make an authenticated request, handling errors gracefully."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.error("Plaud API error: %s %s — %s", method, path, e)
            return None

    def _handle_region_redirect(self, data: dict) -> str | None:
        """If the API returns a region redirect, update base_url and return the new domain.

        Returns the new base URL if redirected, None otherwise.
        """
        if data.get("status") == -302:
            new_url = data.get("data", {}).get("domains", {}).get("api", "")
            if new_url:
                logger.info("Plaud region redirect: %s → %s", self.base_url, new_url)
                self.base_url = new_url.rstrip("/")
                return self.base_url
        return None

    def test_connection(self) -> dict:
        """Verify the token works. Returns {"ok": bool, "message": str, "recording_count": int}."""
        url = f"{self.base_url}/file/simple/web"
        try:
            resp = self.session.get(url, params={"skip": 0, "limit": 200, "is_trash": 0}, timeout=15)
        except requests.ConnectionError:
            return {"ok": False, "message": f"Cannot reach {self.base_url}", "recording_count": 0}
        except requests.Timeout:
            return {"ok": False, "message": "Connection timed out", "recording_count": 0}
        except requests.RequestException as e:
            return {"ok": False, "message": str(e), "recording_count": 0}

        if resp.status_code == 401:
            return {"ok": False, "message": "Token rejected (401) — expired or invalid", "recording_count": 0}
        if resp.status_code == 403:
            return {"ok": False, "message": "Access denied (403)", "recording_count": 0}
        if resp.status_code != 200:
            return {"ok": False, "message": f"Unexpected status {resp.status_code}", "recording_count": 0}

        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "message": "Invalid response from API", "recording_count": 0}

        # Handle region redirect — retry with the correct domain
        if self._handle_region_redirect(data):
            return self.test_connection()

        if data.get("status") != 0:
            return {"ok": False, "message": f"API error: {data.get('msg', 'unknown')}", "recording_count": 0}

        count = data.get("data_file_total", len(data.get("data_file_list", [])))
        return {"ok": True, "message": f"Connected to Plaud. {count} recordings available", "recording_count": count}

    def list_recordings(self, limit: int = 100) -> list[dict]:
        """Return list of recording metadata dicts.

        Each dict has at least: id, filename, duration, start_time.
        """
        resp = self._request(
            "GET",
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": limit,
                "is_trash": 0,
                "sort_by": "edit_time",
                "is_desc": "true",
            },
        )
        if resp is None:
            return []
        data = resp.json()

        # Handle region redirect — retry with the correct domain
        if self._handle_region_redirect(data):
            return self.list_recordings(limit=limit)

        return data.get("data_file_list", [])

    def download_recording(self, file_id: str, output_path: str) -> bool:
        """Download a recording MP3 to disk. Returns True on success."""
        resp = self._request("GET", f"/file/download/{file_id}", allow_redirects=True)
        if resp is None:
            return False
        try:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            size_mb = len(resp.content) / (1024 * 1024)
            logger.info("Downloaded %s (%.1f MB)", output_path, size_mb)
            return True
        except OSError as e:
            logger.error("Failed to write %s: %s", output_path, e)
            return False
