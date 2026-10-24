import io
import json
import threading
import time
from pathlib import Path

import pytest

from job_matcher.discovery import Discovery
from job_matcher.sources import http as module
from job_matcher.sources.base import Board
from job_matcher.sources.http import Http, SourceError, urllib_transport
from job_matcher.storage import Store


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


@pytest.mark.parametrize("length", [None, 2, "invalid"])
def test_http_bounds_actual_bytes_even_without_reliable_length(monkeypatch, length):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 16)
    response = Response(b"x" * 40, length)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="byte limit"):
        Http().json("https://example.invalid/jobs")
    assert response.bytes_read == 17
    assert response.closed


def test_http_rejects_advertised_size_before_reading(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 16)
    response = Response(b"x", 100)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="byte limit"):
        Http().json("https://example.invalid/jobs")
    assert response.bytes_read == 0
    assert response.closed


def test_http_accepts_exact_limit(monkeypatch):
    body = b'["123456789012"]'
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(body))
    response = Response(body, len(body))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    assert Http().json("https://example.invalid/jobs") == ["123456789012"]
    assert response.closed


def test_response_limit_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 16)
    response = Response(('"' + "é" * 8 + '"').encode())
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="byte limit"):
        Http().json("https://example.invalid/jobs")
    assert response.bytes_read == 17


@pytest.mark.parametrize("cancel", [False, True])
def test_stopped_run_never_opens_url(monkeypatch, cancel):
    def forbidden(*args, **kwargs):
        pytest.fail("stopped run must not open a URL")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    stop = threading.Event()
    if cancel:
        stop.set()
    client = Http().bounded(time.monotonic() + (1 if cancel else -1), stop)
    with pytest.raises(SourceError, match="timed out"):
        client.json("https://example.invalid/jobs")


def test_native_transport_checks_timeout_before_opening_url(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("expired request must not open a URL")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(SourceError, match="timed out"):
        urllib_transport("GET", "https://example.invalid/jobs", None, {}, 0)


def test_native_transport_stops_reading_when_timeout_expires(monkeypatch):
    clock = iter([0, 0, 0, 0, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    response = Response(b"[]")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    with pytest.raises(SourceError, match="timed out"):
        urllib_transport("GET", "https://example.invalid/jobs", None, {}, 1)
    assert response.bytes_read == 2
    assert response.closed


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


@pytest.mark.parametrize("nested", [False, True], ids=["invalid", "nested"])
def test_invalid_or_excessively_nested_json_is_a_source_error(monkeypatch, nested):
    # Whether deep nesting overflows depends on the interpreter, so simulate it.
    if nested:

        def overflow(text):
            raise RecursionError

        monkeypatch.setattr(module.json, "loads", overflow)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(b"not JSON"))
    with pytest.raises(SourceError, match="not JSON"):
        Http().json("https://example.invalid/jobs")


@pytest.mark.parametrize("job_count", [896, 2500])
def test_large_greenhouse_board_fetches_and_reuses_cache(tmp_path, monkeypatch, job_count):
    # Synthetic public API shape, with full descriptions and nested metadata. The two
    # sizes model the reported 896-job regression and a larger board with headroom.
    job = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_job.json").read_text())
    paragraph = "Lead engineers building reliable platforms. Collaborate across teams. "
    job["content"] = "<p>" + paragraph * 160 + "</p>"
    payload = json.dumps(
        {
            "jobs": [
                job | {"id": i, "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{i}"}
                for i in range(1, job_count + 1)
            ],
            "meta": {"total": job_count},
        }
    ).encode()
    assert 8 * 1024 * 1024 < len(payload) < module.MAX_RESPONSE_BYTES
    if job_count == 2500:
        assert len(payload) > 16 * 1024 * 1024
    response, calls = Response(payload, len(payload)), []

    def open_response(request, timeout):
        calls.append(request.full_url)
        return response

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    store = Store(tmp_path)
    board, _ = store.save_board(
        Board(provider="greenhouse", token="acme", company="Acme").model_dump(),
        board_url="https://job-boards.greenhouse.io/acme",
        board_name="Acme",
        verified=True,
        method="careers-url",
    )
    discovery = Discovery(store, Http())
    result = discovery.find_jobs(profile=None, limit=100)
    assert result["errors"] == []
    assert len(result["jobs"]) == 100
    assert result["boards"][0]["postings"] == job_count
    assert result["boards"][0]["status"] == "fetched"
    assert response.bytes_read == len(payload)
    assert response.closed
    cached = store.board_listing(board["id"])
    assert len(cached["postings"]) == job_count
    assert cached["complete"] is True
    assert paragraph.strip() in cached["postings"][-1]["description"]
    repeat = discovery.find_jobs(profile=None, limit=100)
    assert repeat["errors"] == []
    assert repeat["boards"][0]["status"] == "cached"
    assert len(repeat["jobs"]) == 100
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]
