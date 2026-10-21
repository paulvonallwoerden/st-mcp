"""HTTP access to st.com.

st.com sits behind Akamai bot protection, which fingerprints the TLS/HTTP2
handshake and drops connections from ordinary HTTP clients. curl_cffi lets us
present the handshake of a real browser, which is all it takes to get through.

Requests are serialized and lightly rate limited: the server is meant for one
person reading documentation, not for crawling.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

BASE_URL = "https://www.st.com"
ALLOWED_HOSTS = {"www.st.com", "st.com", "ksmk.st.com"}

# Browser fingerprints to cycle through when one gets blocked.
IMPERSONATE_TARGETS = ("chrome", "safari", "edge", "firefox")

DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}


class StError(RuntimeError):
    """A request to st.com failed in a way worth reporting to the caller."""


def default_cache_dir() -> Path:
    if env := os.environ.get("ST_MCP_CACHE_DIR"):
        return Path(env).expanduser()
    return Path.home() / ".cache" / "st-mcp"


def absolute_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = urljoin(BASE_URL + "/", url.lstrip("/"))
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise StError(f"Refusing to fetch non-st.com URL: {url}")
    return url


def _looks_blocked(resp: requests.Response) -> bool:
    if resp.status_code in (403, 429, 503):
        return True
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype and len(resp.content) < 4000:
        body = resp.text.lower()
        return "access denied" in body or "reference #" in body
    return False


class StClient:
    def __init__(self, cache_dir: Path | None = None, min_interval: float = 0.35):
        self.cache_dir = cache_dir or default_cache_dir()
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._target_idx = 0
        self._session = self._new_session()

    def _new_session(self) -> requests.Session:
        return requests.Session(
            impersonate=IMPERSONATE_TARGETS[self._target_idx],
            headers=DEFAULT_HEADERS,
            timeout=90,
        )

    def _rotate(self) -> None:
        self._target_idx = (self._target_idx + 1) % len(IMPERSONATE_TARGETS)
        self._session.close()
        self._session = self._new_session()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        allow_404: bool = False,
    ) -> requests.Response:
        url = absolute_url(url)
        last_error = ""
        with self._lock:
            for attempt in range(4):
                wait = self.min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                try:
                    resp = self._session.request(method, url, params=params, headers=headers)
                except RequestException as exc:
                    last_error = str(exc)
                    self._rotate()
                    time.sleep(1.5 * attempt)
                    continue
                finally:
                    self._last_request = time.monotonic()
                if _looks_blocked(resp):
                    last_error = f"HTTP {resp.status_code} (blocked by bot protection?)"
                    self._rotate()
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if resp.status_code == 404 and allow_404:
                    return resp
                if resp.status_code >= 400:
                    raise StError(f"HTTP {resp.status_code} for {url}")
                return resp
        raise StError(f"Could not fetch {url}: {last_error}")

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, params: dict | None = None):
        resp = self.get(url, params=params, headers={"Accept": "application/json"})
        try:
            return resp.json()
        except ValueError as exc:
            raise StError(f"Expected JSON from {url}, got {resp.headers.get('content-type')}") from exc

    def exists(self, url: str) -> str | None:
        """Return the content type if the URL exists, else None."""
        try:
            resp = self.request("HEAD", url, allow_404=True)
        except StError:
            return None
        if resp.status_code != 200:
            return None
        return resp.headers.get("content-type", "")

    def fetch_file(self, url: str, max_age_days: float = 14) -> Path:
        """Download a (binary) resource into the on-disk cache and return its path."""
        url = absolute_url(url)
        path_part = urlparse(url).path
        suffix = Path(path_part).suffix.lower() or ".bin"
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        dest = self.cache_dir / "files" / f"{Path(path_part).stem[:60]}-{key}{suffix}"
        if dest.exists() and time.time() - dest.stat().st_mtime < max_age_days * 86400:
            return dest
        resp = self.get(url)
        ctype = resp.headers.get("content-type", "")
        if suffix in (".pdf", ".zip") and "html" in ctype:
            raise StError(f"Expected a {suffix} file from {url} but got an HTML page")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return dest
