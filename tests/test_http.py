import io
import threading
import time

import pytest

from job_matcher.sources import http as module
from job_matcher.sources.http import Http, SourceError, urllib_transport


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.bytes_read = 0

    def read1(self, size):
        assert size > 0  # Never issue an unbounded read, including at the byte limit.
        data = super().read1(size)
        self.bytes_read += len(data)
        return data


@pytest.mark.parametrize("length", [None, 2])
def test_transport_bounds_actual_bytes_even_without_reliable_length(monkeypatch, length):
    response = Response(b"x" * 40, length)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="byte limit"):
        urllib_transport("GET", "https://example.invalid/jobs", None, {}, 1, max_bytes=16)
    assert response.bytes_read == 17
    assert response.closed


def test_transport_rejects_advertised_size_before_reading(monkeypatch):
    response = Response(b"x", 100)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="byte limit"):
        urllib_transport("GET", "https://example.invalid/jobs", None, {}, 1, max_bytes=16)
    assert response.bytes_read == 0


def test_transport_accepts_exact_limit(monkeypatch):
    response = Response(b"x" * 16, 16)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    assert urllib_transport("GET", "https://example.invalid/jobs", None, {}, 1, max_bytes=16) == (
        200,
        "x" * 16,
    )


def test_custom_transport_is_bounded_before_json_parsing(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 16)
    client = Http(lambda *a: (200, '"' + "x" * 20 + '"'))
    with pytest.raises(SourceError, match="byte limit"):
        client.json("https://example.invalid/jobs")


def test_response_limit_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 16)
    client = Http(lambda *a: (200, '"' + "é" * 8 + '"'))
    with pytest.raises(SourceError, match="byte limit"):
        client.json("https://example.invalid/jobs")


def test_board_budget_counts_native_reads_even_on_rejected_responses(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 8)
    monkeypatch.setattr(module, "MAX_BOARD_BYTES", 12)
    responses = []

    def open_response(*args, **kwargs):
        response = Response(b"x" * 40)
        responses.append(response)
        return response

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    root = Http(retries=0)
    client = root.bounded(time.monotonic() + 1, threading.Event())
    with pytest.raises(SourceError, match="byte limit"):
        client.request("GET", "https://example.invalid/jobs")
    with pytest.raises(SourceError, match="byte limit"):
        client.request("GET", "https://example.invalid/jobs")
    with pytest.raises(SourceError, match="byte limit"):
        client.request("GET", "https://example.invalid/jobs")
    assert len(responses) == 2
    assert sum(r.bytes_read for r in responses) == 13
    assert all(r.closed for r in responses)
    assert root.bytes_left is None  # Budgets belong to each board/run.


def test_exact_board_budget_allows_parsing_but_stops_next_request(monkeypatch):
    monkeypatch.setattr(module, "MAX_BOARD_BYTES", 2)
    calls = []
    root = Http(lambda *a: (calls.append(a) or 200, "[]"))
    client = root.bounded(time.monotonic() + 1, threading.Event())
    assert client.json("https://example.invalid/jobs") == []
    with pytest.raises(SourceError, match="byte limit"):
        client.json("https://example.invalid/jobs")
    assert len(calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_stopped_run_never_starts_transport(cancel):
    calls, stop = [], threading.Event()
    if cancel:
        stop.set()
    client = Http(lambda *a: (calls.append(a) or 200, "[]")).bounded(
        time.monotonic() + (1 if cancel else -1), stop
    )
    with pytest.raises(SourceError, match="timed out"):
        client.json("https://example.invalid/jobs")
    assert not calls


def test_native_transport_checks_deadline_before_opening_url(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("expired request must not open a URL")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(SourceError, match="timed out"):
        urllib_transport("GET", "https://example.invalid/jobs", None, {}, 1, deadline=0)


def test_cancellation_after_response_prevents_retry():
    stop, calls = threading.Event(), []

    def limited(*args):
        calls.append(args)
        stop.set()
        return 429, ""

    client = Http(limited, retries=1, backoff=0).bounded(time.monotonic() + 1, stop)
    with pytest.raises(SourceError, match="timed out"):
        client.json("https://example.invalid/jobs")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("body", "constant", "limit"),
    [
        ("[1,2,3]", "MAX_COLLECTION_ITEMS", 2),
        ("[[[[[]]]]]", "MAX_JSON_DEPTH", 3),
        ("[1,2,3]", "MAX_JSON_VALUES", 3),
    ],
)
def test_json_budgets_are_checked_before_adapter_consumption(monkeypatch, body, constant, limit):
    monkeypatch.setattr(module, constant, limit)
    with pytest.raises(SourceError, match="JSON"):
        Http(lambda *a: (200, body)).json("https://example.invalid/jobs")
