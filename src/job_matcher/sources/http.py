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
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BOARD_BYTES = 16 * 1024 * 1024
MAX_COLLECTION_ITEMS = 10_000
MAX_JSON_VALUES = 100_000
MAX_JSON_DEPTH = 64


class SourceError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def urllib_transport(
    method,
    url,
    body,
    headers,
    timeout,
    *,
    max_bytes=MAX_RESPONSE_BYTES,
    deadline=None,
    cancelled=None,
    consume=None,
):
    """Return (status, text). HTTP errors are statuses; network failures raise OSError."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + timeout)

    def check():
        if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
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
                if consume is not None:
                    consume(len(chunk))
                size += len(chunk)
                if size > max_bytes:
                    raise SourceError("ATS response exceeds the byte limit")
                chunks.append(chunk)
            return response.status, b"".join(chunks).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


_URLLIB_TRANSPORT = urllib_transport


def bounded_json(value, max_bytes=MAX_BOARD_BYTES, check=None):
    """Serialize a cache payload without building an unbounded output string."""
    chunks, size = [], 0
    for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value):
        if check is not None:
            check()
        size += len(chunk.encode("utf-8"))
        if size > max_bytes:
            raise SourceError("ATS cache payload exceeds the byte limit")
        chunks.append(chunk)
    return "".join(chunks)


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
        self.bytes_left = None

    def bounded(self, deadline, cancelled):
        """A board-local budget sharing this client's global request semaphore."""
        client = copy(self)
        client.deadline, client.cancelled = deadline, cancelled
        client.bytes_left = MAX_BOARD_BYTES
        return client

    def check_deadline(self):
        if self.cancelled.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        ):
            raise SourceError("Discovery timed out")

    def check(self):
        self.check_deadline()
        if self.bytes_left is not None and self.bytes_left < 0:
            raise SourceError("ATS board exceeds the download byte limit")

    def _consume(self, size):
        if self.bytes_left is not None:
            self.bytes_left -= size
            self.check()

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
            if self.bytes_left == 0:
                raise SourceError("ATS board exceeds the download byte limit")
            try:
                if not self.slots.acquire(timeout=self._timeout()):
                    self.check()
                    raise SourceError("Timed out waiting for an ATS request slot")
                try:
                    timeout = self._timeout()
                    if self.transport is _URLLIB_TRANSPORT:
                        cap = (
                            MAX_RESPONSE_BYTES
                            if self.bytes_left is None
                            else min(MAX_RESPONSE_BYTES, self.bytes_left)
                        )
                        status, text = self.transport(
                            method,
                            url,
                            body,
                            headers,
                            timeout,
                            max_bytes=cap,
                            deadline=self.deadline,
                            cancelled=self.cancelled,
                            consume=self._consume,
                        )
                    else:
                        # Preserve the five-argument seam used by offline transports.
                        status, text = self.transport(method, url, body, headers, timeout)
                finally:
                    self.slots.release()
            except (OSError, ValueError) as exc:
                if attempt < self.retries and isinstance(exc, TimeoutError):
                    self._pause(self.backoff)
                    continue
                raise SourceError(f"Request failed ({type(exc).__name__})") from None
            self.check()
            if len(text) > MAX_RESPONSE_BYTES:
                raise SourceError("ATS response exceeds the byte limit")
            size = len(text.encode("utf-8"))
            if size > MAX_RESPONSE_BYTES:
                raise SourceError("ATS response exceeds the byte limit")
            if self.transport is not _URLLIB_TRANSPORT:
                self._consume(size)
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
        # Bound decoded collection sizes/depth before adapters normalize or cache them.
        stack, count = [(iter((value,)), 0)], 0
        while stack:
            self.check()
            items, depth = stack[-1]
            try:
                item = next(items)
            except StopIteration:
                stack.pop()
                continue
            count += 1
            if count > MAX_JSON_VALUES:
                raise SourceError("ATS response contains too many JSON values")
            if isinstance(item, (dict, list)):
                if len(item) > MAX_COLLECTION_ITEMS or depth >= MAX_JSON_DEPTH:
                    raise SourceError("ATS response exceeds JSON collection limits")
                stack.append((iter(item.values() if isinstance(item, dict) else item), depth + 1))
        return value
