"""Minimal HTTP client for public ATS endpoints.

Standard library only: mcp brings httpx2 transitively, not httpx, and discovery should not
depend on another package's dependency choices. Tests replace ``transport``.
"""

import json
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "job-matcher/0.1 (open-source job discovery; public job-board APIs only)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def urllib_transport(method, url, body, headers, timeout):
    """Return (status, text). HTTP errors are statuses; network failures raise OSError."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


class Http:
    def __init__(self, transport=None, timeout=20, retries=1, concurrency=8, backoff=1.0):
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        # Bounds simultaneous requests across all boards and detail fetches.
        self.slots = threading.BoundedSemaphore(concurrency)

    def request(self, method, url, body=None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.retries + 1):
            try:
                with self.slots:
                    status, text = self.transport(method, url, body, headers, self.timeout)
            except (OSError, ValueError) as exc:
                if attempt < self.retries and isinstance(exc, TimeoutError):
                    time.sleep(self.backoff)
                    continue
                raise SourceError(f"Request failed ({type(exc).__name__})") from None
            if status in RETRY_STATUSES and attempt < self.retries:
                time.sleep(self.backoff * (attempt + 1))
                continue
            return status, text
        return status, text

    def json(self, url, body=None, missing_ok=False):
        """Parsed JSON for a 200, None for 404/410 when missing_ok, otherwise SourceError."""
        status, text = self.request("POST" if body is not None else "GET", url, body)
        if status in {404, 410} and missing_ok:
            return None
        if status != 200:
            raise SourceError(f"HTTP {status}", status)
        try:
            return json.loads(text)
        except ValueError:
            raise SourceError("Response was not JSON") from None
