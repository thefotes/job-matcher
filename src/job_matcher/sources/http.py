"""Minimal HTTP client for public ATS endpoints.

Standard library only: mcp brings httpx2 transitively, not httpx, and discovery should not
depend on another package's dependency choices. Tests replace ``transport``.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from copy import copy

USER_AGENT = "job-matcher/0.1 (open-source job discovery; public job-board APIs only)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Large unpaginated boards can exceed 10 MiB with descriptions included.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class SourceError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def urllib_transport(method, url, body, headers, timeout):
    """Return (status, text). HTTP errors are statuses; network failures raise OSError."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    max_bytes = MAX_RESPONSE_BYTES
    deadline = time.monotonic() + timeout

    def check():
        if time.monotonic() >= deadline:
            raise SourceError("Discovery timed out")

    check()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            try:
                advertised = int(length) if length is not None else 0
            except ValueError:
                advertised = 0
            if advertised > max_bytes:
                raise SourceError("ATS response exceeds the byte limit")
            chunks, size = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                check()
                chunk = read(min(64 * 1024, max_bytes + 1 - size))
                check()
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise SourceError("ATS response exceeds the byte limit")
                chunks.append(chunk)
            return response.status, b"".join(chunks).decode("utf-8", "replace")
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
        self.deadline = None
        self.cancelled = threading.Event()

    def bounded(self, deadline, cancelled):
        """A run-scoped client sharing the global request semaphore."""
        client = copy(self)
        client.deadline, client.cancelled = deadline, cancelled
        return client

    def check(self):
        if self.cancelled.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        ):
            raise SourceError("Discovery timed out")

    def _timeout(self):
        self.check()
        return (
            min(self.timeout, self.deadline - time.monotonic()) if self.deadline else self.timeout
        )

    def _pause(self, seconds):
        self.check()
        if self.deadline is not None:
            seconds = min(seconds, max(0, self.deadline - time.monotonic()))
        self.cancelled.wait(seconds)
        self.check()

    def request(self, method, url, body=None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.retries + 1):
            self.check()
            try:
                if not self.slots.acquire(timeout=self._timeout()):
                    self.check()
                    raise SourceError("Timed out waiting for an ATS request slot")
                try:
                    timeout = self._timeout()
                    status, text = self.transport(method, url, body, headers, timeout)
                finally:
                    self.slots.release()
            except (OSError, ValueError) as exc:
                if attempt < self.retries and isinstance(exc, TimeoutError):
                    self._pause(self.backoff)
                    continue
                raise SourceError(f"Request failed ({type(exc).__name__})") from None
            self.check()
            if status in RETRY_STATUSES and attempt < self.retries:
                self._pause(self.backoff * (attempt + 1))
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
        self.check()
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise SourceError("Response was not JSON") from None
        self.check()
        return value
