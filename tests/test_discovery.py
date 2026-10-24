import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_evaluator import FakeProvider

from job_matcher.discovery import Discovery, title_matches
from job_matcher.models import Job
from job_matcher.service import Service
from job_matcher.sources import adapter
from job_matcher.sources.base import Board
from job_matcher.sources.http import Http, SourceError
from job_matcher.sources.resolve import infer_board, search, token_candidates
from job_matcher.sources.text import html_to_text, infer_remote, salary_ranges

DESCRIPTION = "<p>Lead a team of engineers building our platform. Salary is posted.</p>"


def ago(days, fmt="iso"):
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    if fmt == "ms":
        return int(moment.timestamp() * 1000)
    if fmt == "recruitee":
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC")
    return moment.isoformat()


class FakeAts:
    """Transport stub: routes map URL -> payload, (status, payload) or callable(body)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body))
        assert "User-Agent" in headers
        route = self.routes.get(url)
        if route is None:
            return 404, ""
        if callable(route):
            route = route(body)
        status, payload = route if isinstance(route, tuple) else (200, route)
        return status, payload if isinstance(payload, str) else json.dumps(payload)


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true"
LEVER = "https://api.lever.co/v0/postings/{}?mode=json"
SR = "https://api.smartrecruiters.com/v1/companies/{}/postings"
WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers"


def greenhouse_payload(company="Acme"):
    return {
        "jobs": [
            {
                "id": 11,
                "title": "Senior Engineering Manager, Platform",
                "company_name": company,
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/11",
                "location": {"name": "Remote, USA"},
                "content": "&lt;p&gt;Lead a team of engineers building our platform.&lt;/p&gt;"
                "&lt;ul&gt;&lt;li&gt;Hire&lt;/li&gt;&lt;li&gt;Coach&lt;/li&gt;&lt;/ul&gt;",
                "metadata": [{"name": "Salary Range", "value": "$200k-$250k"}],
                "first_published": ago(2),
                "updated_at": ago(1),
            },
            {
                "id": 12,
                "title": "Engineering Manager",
                "company_name": company,
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/12",
                "location": {"name": "New York, NY"},
                "content": DESCRIPTION,
                "first_published": ago(1),
            },
            {
                "id": 13,
                "title": "Director of Engineering",
                "company_name": company,
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/13",
                "location": {"name": "Remote"},
                "content": DESCRIPTION,
                "first_published": ago(40),
            },
            {
                "id": 14,
                "title": "Account Executive",
                "company_name": company,
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/14",
                "location": {"name": "Remote"},
                "content": DESCRIPTION,
                "first_published": ago(1),
            },
        ]
    }


def ashby_payload():
    return {
        "jobs": [
            {
                "id": "a-1",
                "title": "Manager, Software Engineering",
                "location": "San Francisco",
                "secondaryLocations": [{"location": "New York"}],
                "isRemote": True,
                "workplaceType": "Remote",
                "isListed": True,
                "publishedAt": ago(3),
                "jobUrl": "https://jobs.ashbyhq.com/globex/a-1",
                "applyUrl": "https://jobs.ashbyhq.com/globex/a-1/application",
                "descriptionHtml": DESCRIPTION,
                "compensation": {"compensationTierSummary": "$210K – $260K • Offers Equity"},
            },
            {"id": "a-2", "title": "Hidden", "isListed": False, "descriptionHtml": DESCRIPTION},
        ]
    }


def lever_payload():
    return [
        {
            "id": "l-1",
            "text": "Engineering Manager - Payments",
            "categories": {"location": "Austin, TX", "allLocations": ["Austin, TX"]},
            "workplaceType": "hybrid",
            "createdAt": ago(1, "ms"),
            "hostedUrl": "https://jobs.lever.co/initech/l-1",
            "applyUrl": "https://jobs.lever.co/initech/l-1/apply",
            "description": "<div>Opening paragraph about the team.</div>",
            "lists": [{"text": "Requirements", "content": "<li>Seven years leading teams</li>"}],
            "additional": "<p>Benefits and equal opportunity statement.</p>",
            "salaryRange": {
                "min": 180000,
                "max": 220000,
                "currency": "USD",
                "interval": "per-year-salary",
            },
        }
    ]


def test_html_to_text_handles_escaped_markup_lists_and_scripts():
    text = html_to_text(
        "&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;We&amp;nbsp;build.&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;&lt;p&gt;One&lt;/p&gt;&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    assert text == "About\n\nWe build.\n\n- One\n- Two"
    assert html_to_text(None) == ""


def test_remote_inference_prefers_explicit_workplace():
    assert infer_remote("Remote") is True
    assert infer_remote("hybrid", True, "Remote") is False
    assert infer_remote(None, False, "Remote - US") is True
    assert infer_remote(None, False, "Berlin") is False
    assert infer_remote(location="New York, NY") is None


def test_title_keywords_match_words_in_any_order():
    keywords = ["Engineering Manager", "Director of Engineering"]
    assert title_matches("Manager, Software Engineering", keywords)
    assert title_matches("Sr. Eng Manager", keywords)
    assert title_matches("Engineering Director, Payments", keywords)
    assert title_matches("Engineering Managers", keywords)
    assert not title_matches("Software Engineer", keywords)
    assert title_matches("Anything", [])


def test_greenhouse_adapter_normalizes_postings():
    http = Http(FakeAts({GH.format("acme") + "?content=true": greenhouse_payload()}))
    listing = adapter("greenhouse").fetch(http, Board(provider="greenhouse", token="acme"))
    first = listing.postings[0]
    assert listing.board_name == "Acme"
    assert first.company == "Acme"
    assert first.description.startswith("Lead a team of engineers")
    assert "- Hire\n- Coach" in first.description
    assert first.remote is True and first.location == "Remote, USA"
    assert first.compensation == "$200k-$250k"
    assert first.source == "greenhouse" and first.source_id == "11"
    assert first.posted_at.endswith("+00:00")
    job = Job.model_validate(first.job())
    assert job.url == "https://job-boards.greenhouse.io/acme/jobs/11"


def test_ashby_adapter_reads_remote_and_compensation():
    http = Http(FakeAts({ASHBY.format("globex"): ashby_payload()}))
    board = Board(provider="ashby", token="globex", company="Globex")
    listing = adapter("ashby").fetch(http, board)
    assert len(listing.postings) == 1  # unlisted postings are skipped
    posting = listing.postings[0]
    assert posting.company == "Globex"
    assert posting.remote is True
    assert posting.location == "San Francisco, New York"
    assert posting.compensation.startswith("$210K")
    assert posting.apply_url.endswith("/application")
    Job.model_validate(posting.job())


def test_lever_adapter_keeps_full_description_sections():
    http = Http(FakeAts({LEVER.format("initech"): lever_payload()}))
    posting = adapter("lever").fetch(http, Board(provider="lever", token="initech")).postings[0]
    assert "Opening paragraph" in posting.description
    assert "Requirements\n\n- Seven years leading teams" in posting.description
    assert "Benefits" in posting.description
    assert posting.remote is False
    assert posting.compensation == "USD 180,000–220,000 per year salary"
    assert posting.posted_at
    Job.model_validate(posting.job())


def test_smartrecruiters_adapter_pages_and_fetches_details(monkeypatch):
    monkeypatch.setattr("job_matcher.sources.smartrecruiters.PAGE", 2)

    def page(offset):
        ids = [1, 2] if offset == 0 else [3]
        return {
            "totalFound": 3,
            "content": [
                {
                    "id": str(i),
                    "name": f"Engineering Manager {i}",
                    "company": {"identifier": "Hooli", "name": "Hooli"},
                    "releasedDate": ago(i),
                    "location": {"fullLocation": "Remote, US", "remote": True},
                }
                for i in ids
            ],
        }

    routes = {
        SR.format("hooli") + "?limit=2&offset=0": page(0),
        SR.format("hooli") + "?limit=2&offset=2": page(2),
        SR.format("hooli") + "/1": {
            "id": "1",
            "name": "Engineering Manager 1",
            "postingUrl": "https://jobs.smartrecruiters.com/Hooli/1-em",
            "applyUrl": "https://jobs.smartrecruiters.com/Hooli/1-em?oga=true",
            "location": {"fullLocation": "Remote, US", "remote": True},
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": DESCRIPTION},
                    "qualifications": {"text": "<p>Five years managing.</p>"},
                }
            },
        },
    }
    board = Board(provider="smartrecruiters", token="hooli")
    module = adapter("smartrecruiters")
    listing = module.fetch(Http(FakeAts(routes)), board)
    assert [p.source_id for p in listing.postings] == ["1", "2", "3"]
    assert listing.board_name == "Hooli" and listing.complete
    assert all(p.description is None for p in listing.postings)
    full = module.detail(Http(FakeAts(routes)), board, listing.postings[0])
    assert "Five years managing" in full.description
    assert full.url == "https://jobs.smartrecruiters.com/Hooli/1-em"
    assert full.remote is True
    Job.model_validate(full.job())


def test_recruitee_adapter_skips_sample_offers():
    offers = {
        "offers": [
            {
                "id": 7,
                "title": "Head of Engineering",
                "company_name": "Umbrella",
                "careers_url": "https://umbrella.recruitee.com/o/head",
                "careers_apply_url": "https://umbrella.recruitee.com/o/head/c/new",
                "location": "Amsterdam, Netherlands",
                "remote": False,
                "hybrid": True,
                "description": DESCRIPTION,
                "requirements": "<p>Experience required.</p>",
                "salary": {"min": 100000, "max": 120000, "currency": "EUR", "period": "year"},
                "published_at": ago(1, "recruitee"),
            },
            {"id": 8, "title": "Demo", "slug": "sample-job", "description": DESCRIPTION},
        ]
    }
    http = Http(FakeAts({"https://umbrella.recruitee.com/api/offers/": offers}))
    listing = adapter("recruitee").fetch(http, Board(provider="recruitee", token="umbrella"))
    assert [p.source_id for p in listing.postings] == ["7"]
    posting = listing.postings[0]
    assert posting.remote is False and posting.compensation == "EUR 100,000–120,000 year"
    assert "Experience required." in posting.description
    assert posting.posted_at
    Job.model_validate(posting.job())


def workday_routes(pages):
    def listing(body):
        offset = body["offset"]
        items = pages[offset // 20] if offset // 20 < len(pages) else []
        return {"total": sum(len(p) for p in pages), "jobPostings": items}

    def item(n, posted):
        return {
            "title": f"Engineering Manager {n}",
            "externalPath": f"/job/Remote/Engineering-Manager_R{n}",
            "locationsText": "2 Locations",
            "postedOn": posted,
        }

    pages = [[item(n, posted) for n, posted in page] for page in pages]
    routes = {WD + "/jobs": listing}
    for page in pages:
        for posting in page:
            routes[WD + posting["externalPath"]] = {
                "jobPostingInfo": {
                    "title": posting["title"],
                    "jobDescription": DESCRIPTION,
                    "location": "Remote, United States",
                    "additionalLocations": ["New York, NY"],
                    "startDate": ago(1)[:10],
                    "externalUrl": "https://acme.wd5.myworkdayjobs.com/Careers"
                    + posting["externalPath"],
                }
            }
    return routes


def test_workday_adapter_stops_paging_past_horizon(monkeypatch):
    pages = [
        [(n, "Posted Today") for n in range(20)],
        [(n, "Posted 30+ Days Ago") for n in range(20, 40)],
        [(n, "Posted 30+ Days Ago") for n in range(40, 45)],
    ]
    fake = FakeAts(workday_routes(pages))
    board = Board(provider="workday", token="acme", shard="wd5", site="Careers", company="Acme")
    module = adapter("workday")
    listing = module.fetch(Http(fake), board, horizon_days=7)
    assert len(fake.calls) == 2 and not listing.complete and listing.total == 45
    assert not listing.capped
    monkeypatch.setattr("job_matcher.sources.workday.MAX_PAGES", 1)
    assert module.fetch(Http(fake), board, horizon_days=7).capped
    stub = listing.postings[0]
    assert stub.source_id == "acme:Engineering-Manager_R0"
    assert stub.location is None and stub.description is None
    full = module.detail(Http(fake), board, stub)
    assert full.location == "Remote, United States; New York, NY"
    assert full.remote is True
    assert full.posted_at == ago(1)[:10] + "T00:00:00+00:00"
    Job.model_validate(full.job())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme", "", "")),
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme", "", "")),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", ("greenhouse", "acme", "", "")),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", ("greenhouse", "acme", "", "")),
        ("jobs.ashbyhq.com/globex/a-1", ("ashby", "globex", "", "")),
        ("https://api.ashbyhq.com/posting-api/job-board/globex", ("ashby", "globex", "", "")),
        ("https://jobs.lever.co/initech/l-1/apply", ("lever", "initech", "", "")),
        ("https://api.lever.co/v0/postings/initech?mode=json", ("lever", "initech", "", "")),
        ("https://careers.smartrecruiters.com/Hooli", ("smartrecruiters", "Hooli", "", "")),
        ("https://jobs.smartrecruiters.com/Hooli/1-em", ("smartrecruiters", "Hooli", "", "")),
        ("https://umbrella.recruitee.com/o/head", ("recruitee", "umbrella", "", "")),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Remote/x",
            ("workday", "acme", "wd5", "Careers"),
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs",
            ("workday", "acme", "wd5", "Careers"),
        ),
    ],
)
def test_infer_board_from_careers_urls(url, expected):
    board = infer_board(url)
    assert (board.provider, board.token, board.shard, board.site) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/careers",
        "https://jobs.lever.co/",
        "https://acme.wd5.myworkdayjobs.com/",
    ],
)
def test_infer_board_rejects_unknown_urls(url):
    assert infer_board(url) is None


def test_board_tokens_cannot_escape_the_url():
    with pytest.raises(ValueError):
        Board(provider="recruitee", token="evil.com/x")
    with pytest.raises(ValueError):
        Board(provider="workday", token="acme", shard="wd5")
    assert token_candidates("The Initech Group, Inc.") == ["initech"]
    assert token_candidates("Globex Corporation (US)") == ["globex"]


def test_resolver_verifies_exact_tokens_and_rejects_partial_matches():
    routes = {
        GH.format("acme"): greenhouse_payload("Acme Rockets"),
        LEVER.format("initech"): lever_payload(),
        SR.format("hooli") + "?limit=10&offset=0": {
            "totalFound": 1,
            "content": [{"id": "1", "name": "EM", "company": {"name": "Hooli XYZ"}}],
        },
    }
    http = Http(FakeAts(routes))
    hits, errors = search(http, "Acme")
    assert [(h["provider"], h["verified"], h["method"]) for h in hits] == [
        ("greenhouse", True, "exact-token")
    ]
    hits, _ = search(http, "Acme Rockets")
    assert hits[0]["verified"] and hits[0]["method"] == "board-name"
    hits, _ = search(http, "Initech Global")
    assert [(h["token"], h["verified"], h["method"]) for h in hits] == [
        ("initech", False, "first-word-only")
    ]
    hits, _ = search(http, "Hooli")
    assert [(h["provider"], h["verified"], h["method"]) for h in hits] == [
        ("smartrecruiters", False, "low-trust-provider")
    ]
    assert hits[0]["sample_titles"] == ["EM"]
    assert errors == []


def test_resolver_reports_transport_errors():
    def broken(method, url, body, headers, timeout):
        raise OSError("down")

    hits, errors = search(Http(broken, retries=0), "Acme")
    assert hits == [] and errors and errors[0]["error"].startswith("Request failed")


def test_http_retries_rate_limits_then_raises():
    calls = []

    def limited(method, url, body, headers, timeout):
        calls.append(url)
        return 429, ""

    with pytest.raises(SourceError) as raised:
        Http(limited, retries=1, backoff=0).json("https://api.lever.co/v0/postings/x")
    assert raised.value.status == 429 and len(calls) == 2


@pytest.mark.parametrize("provider", ["recruitee", "smartrecruiters"])
def test_matching_low_trust_board_name_requires_explicit_url(discovery, ats, provider):
    if provider == "recruitee":
        url = "https://hooli.recruitee.com/api/offers/"
        ats.routes[url] = {
            "offers": [
                {
                    "id": 1,
                    "title": "Engineering Manager",
                    "company_name": "Hooli",
                    "slug": "engineering-manager",
                    "description": DESCRIPTION,
                }
            ]
        }
        careers = "https://hooli.recruitee.com"
    else:
        ats.routes[SR.format("hooli") + "?limit=10&offset=0"] = {
            "totalFound": 1,
            "content": [{"id": "1", "name": "Engineering Manager", "company": {"name": "Hooli"}}],
        }
        careers = "https://careers.smartrecruiters.com/hooli"
    result = discovery.add_job_board(company="Hooli")
    assert result["added"] is False
    assert discovery.list_job_boards()["count"] == 0
    assert [(c["provider"], c["verified"], c["method"]) for c in result["candidates"]] == [
        (provider, False, "low-trust-provider")
    ]
    confirmed = discovery.add_job_board(company="Hooli", careers_url=careers)
    assert confirmed["board"]["verified"] is True
    assert confirmed["board"]["verification_method"] == "careers-url"


@pytest.mark.parametrize("provider", ["workday", "smartrecruiters"])
def test_provider_cannot_exceed_requested_page_size(monkeypatch, provider):
    module = adapter(provider)
    monkeypatch.setattr(module, "PAGE", 1)
    if provider == "workday":
        board = Board(provider=provider, token="acme", shard="wd5", site="Careers")
        payload = {
            "total": 2,
            "jobPostings": [
                {"externalPath": f"/job/role_{i}", "title": "Engineering Manager"} for i in range(2)
            ],
        }
        routes = {WD + "/jobs": payload}
    else:
        board = Board(provider=provider, token="hooli")
        routes = {
            SR.format("hooli") + "?limit=1&offset=0": {
                "totalFound": 2,
                "content": [{"id": str(i)} for i in range(2)],
            }
        }
    ats = FakeAts(routes)
    with pytest.raises(SourceError, match="page size"):
        module.fetch(Http(ats), board)
    assert len(ats.calls) == 1


@pytest.mark.parametrize("stage", ["listing", "detail"])
def test_deadline_stops_running_workers_before_more_requests_or_cache_writes(
    discovery, ats, monkeypatch, stage
):
    from job_matcher import discovery as module

    board = discovery.add_job_board("Acme", "https://acme.wd5.myworkdayjobs.com/Careers")["board"]
    started, release = threading.Event(), threading.Event()
    futures, calls = [], []
    original = discovery.discovery.http.transport

    def blocked(method, url, body, headers, timeout):
        calls.append(url)
        is_listing = url == WD + "/jobs"
        if (stage == "listing") == is_listing:
            started.set()
            assert release.wait(2), "test must release its dummy transport"
        return original(method, url, body, headers, timeout)

    def deadline_wait(pending, timeout):
        futures.extend(pending)
        assert started.wait(2), "dummy transport must start before simulating the deadline"
        return set(), set(pending)

    discovery.discovery.http.transport = blocked
    monkeypatch.setattr(module, "wait", deadline_wait)
    try:
        result = discovery.find_jobs(refresh=True, title_keywords=[])
        assert result["jobs"] == []
        assert "Timed out" in result["errors"][0]["error"]
    finally:
        release.set()
        for future in futures:
            future.result(timeout=2)
    assert len(calls) == (1 if stage == "listing" else 2)
    assert bool(discovery.store.board_listing(board["id"])) == (stage == "detail")
    assert discovery.store.board_postings(board["id"], ["acme:Engineering-Manager_R1"], "") == {}
    assert discovery.store.boards()[0]["not_found_count"] == 0


@pytest.fixture
def ats():
    return FakeAts(
        {
            GH.format("acme"): greenhouse_payload(),
            GH.format("acme") + "?content=true": greenhouse_payload(),
            ASHBY.format("globex"): ashby_payload(),
            LEVER.format("initech"): lever_payload(),
            **workday_routes([[(1, "Posted Today"), (2, "Posted 2 Days Ago")]]),
        }
    )


@pytest.fixture
def discovery(tmp_path, ats):
    service = Service(tmp_path, FakeProvider())
    service.record_consent(True)
    service._discovery = Discovery(service.store, Http(ats, backoff=0))
    return service


def test_add_board_by_url_and_company(discovery, ats):
    added = discovery.add_job_board(careers_url="https://job-boards.greenhouse.io/acme/jobs/11")
    assert added["created"] and added["board"]["company"] == "Acme"
    assert added["board"]["verification_method"] == "careers-url"
    assert added["board"]["board_url"] == "https://job-boards.greenhouse.io/acme"
    again = discovery.add_job_board(company="Acme")
    assert not again["created"] and again["board"]["id"] == added["board"]["id"]
    assert again["board"]["verification_method"] == "board-name"

    with pytest.raises(ValueError, match="company"):
        discovery.add_job_board(careers_url="https://jobs.lever.co/initech")
    lever = discovery.add_job_board("Initech", "https://jobs.lever.co/initech")
    assert lever["board"]["provider"] == "lever" and lever["warnings"] == []
    mismatch = discovery.add_job_board("Globex", "https://boards.greenhouse.io/acme")
    assert mismatch["warnings"]

    workday = discovery.add_job_board("Acme", "https://acme.wd5.myworkdayjobs.com/en-US/Careers")
    assert workday["board"]["shard"] == "wd5" and workday["board"]["site"] == "Careers"

    with pytest.raises(ValueError, match="No public"):
        discovery.add_job_board("Nope", "https://jobs.lever.co/nope")
    with pytest.raises(ValueError, match="Unrecognized"):
        discovery.add_job_board("Acme", "https://acme.example/careers")
    with pytest.raises(ValueError):
        discovery.add_job_board()

    candidates = discovery.add_job_board(company="Initech Global")
    assert not candidates["added"]
    assert candidates["candidates"][0]["careers_url"] == "https://jobs.lever.co/initech"
    assert "careers_url" in candidates["next_step"]
    # Re-adding the Acme board under another name relabels it rather than duplicating it.
    assert len(discovery.list_job_boards()["boards"]) == 3


def test_remove_and_deactivate_boards(discovery):
    acme = discovery.add_job_board(careers_url="https://boards.greenhouse.io/acme")["board"]
    discovery.add_job_board("Globex", "https://jobs.ashbyhq.com/globex")
    discovery.find_jobs(title_keywords=["Engineering Manager"])
    deactivated = discovery.remove_job_board(company="globex", deactivate=True)
    assert deactivated["deactivated"][0]["company"] == "Globex"
    assert len(discovery.list_job_boards(include_inactive=False)["boards"]) == 1
    removed = discovery.remove_job_board(board_id=acme["id"])
    assert removed["removed"][0]["id"] == acme["id"]
    assert discovery.store.board_listing(acme["id"]) is None  # cache removed with it
    with pytest.raises(ValueError):
        discovery.remove_job_board(board_id=999)


def test_dead_boards_are_retired_after_three_consecutive_misses(discovery, ats, monkeypatch):
    board = discovery.add_job_board("Initech", "https://jobs.lever.co/initech")["board"]

    def counts():
        row = next(b for b in discovery.store.boards() if b["id"] == board["id"])
        return row["not_found_count"], row["active"], row["last_error"]

    del ats.routes[LEVER.format("initech")]
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert counts() == (2, True, "Board not found (HTTP 404)")

    # A successful fetch resets the count...
    ats.routes[LEVER.format("initech")] = lever_payload()
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert counts() == (0, True, None)

    # ...and other errors never count toward retirement.
    ats.routes[LEVER.format("initech")] = (500, "")
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert counts() == (0, True, "HTTP 500")

    from job_matcher import discovery as discovery_module

    deadline = discovery_module.DEADLINE_SECONDS
    monkeypatch.setattr("job_matcher.discovery.DEADLINE_SECONDS", 0)
    result = discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert result["deactivated_boards"] == []  # timeouts do not count either
    monkeypatch.setattr("job_matcher.discovery.DEADLINE_SECONDS", deadline)
    ats.routes[LEVER.format("initech")] = lever_payload()
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)

    ats.routes[LEVER.format("initech")] = (404, "")
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    result = discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert result["deactivated_boards"] == ["Initech"]
    assert counts() == (3, False, "Board not found (HTTP 404)")


def test_add_boards_from_history(discovery, monkeypatch):
    # Distinct last_seen stamps make the recency order deterministic.
    stamps = iter(f"2026-01-01T00:00:{second:02d}+00:00" for second in range(1, 99))
    monkeypatch.setattr("job_matcher.storage.now", lambda: next(stamps))
    discovery.import_history(
        [
            {"title": "A", "company": "Initech Global", "source": "linkedin", "source_id": "2",
             "url": "https://www.linkedin.com/jobs/view/2/", "tracker_stage": "applied"},
            {"title": "B", "company": "Unknown Corp", "source": "linkedin", "source_id": "3",
             "url": "https://www.linkedin.com/jobs/view/3/", "tracker_stage": "applied"},
            {"title": "C", "company": "Umbrella Inc", "source": "linkedin", "source_id": "4",
             "url": "https://www.linkedin.com/jobs/view/4/", "tracker_stage": "applied"},
            {"title": "D", "company": "Globex", "source": "linkedin", "source_id": "1",
             "url": "https://www.linkedin.com/jobs/view/1/", "tracker_stage": "applied"},
        ]
    )  # fmt: skip
    discovery.add_job_board(careers_url="https://boards.greenhouse.io/acme")

    limited = discovery.add_boards_from_history(limit=2)
    assert [a["company"] for a in limited["added"]] == ["Globex"]
    assert limited["added"][0]["provider"] == "ashby"
    assert limited["not_found"] == ["Umbrella Inc"]
    assert limited["unverified"] == [] and limited["errors"] == []
    assert limited["remaining"] == ["Unknown Corp", "Initech Global"]

    def failing(company=None, careers_url=None):
        if company == "Unknown Corp":
            raise RuntimeError("Name service is unavailable")
        return real_add(company, careers_url)

    real_add = discovery.add_job_board
    monkeypatch.setattr(discovery, "add_job_board", failing)
    result = discovery.add_boards_from_history()
    # Globex now has a board, so it is skipped the same as Acme; one failure is contained.
    assert result["added"] == []
    assert result["unverified"] == [{"company": "Initech Global", "candidates": 1}]
    assert result["not_found"] == ["Umbrella Inc"]
    assert [e["company"] for e in result["errors"]] == ["Unknown Corp"]
    assert result["remaining"] == []


def test_import_connections_finder_jsonl(discovery, tmp_path):
    rows = [
        {"company": "Acme", "ats": "greenhouse", "board_token": "acme",
         "verification": {"method": "exact", "match": True}},
        {"company": "Acme Workday", "ats": "workday", "board_token": "acme",
         "workday_shard": "wd5", "workday_site": "Careers",
         "verification": {"method": "llm", "match": True}},
        {"company": "Rejected", "ats": "lever", "board_token": "x",
         "verification": {"match": False}},
        {"company": "Old", "ats": "workable", "board_token": "old"},
        {"company": "Broken", "ats": "workday", "board_token": "acme"},
    ]  # fmt: skip
    path = tmp_path / "boards.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n\n")
    result = discovery.import_job_boards(str(path))
    assert result["imported"] == 2 and result["created"] == 2
    assert [s["line"] for s in result["skipped"]] == [3]
    assert [e["line"] for e in result["errors"]] == [4, 5, 6]
    boards = discovery.list_job_boards()["boards"]
    assert {b["verification_method"] for b in boards} == {"import:exact", "import:llm"}
    assert discovery.import_job_boards(str(path))["updated"] == 2
    with pytest.raises(ValueError, match="absolute"):
        discovery.import_job_boards("boards.jsonl")
    with pytest.raises(ValueError, match="not found"):
        discovery.import_job_boards(str(tmp_path / "missing.jsonl"))


def test_find_jobs_filters_sorts_and_caches(discovery, ats):
    discovery.add_job_board(careers_url="https://boards.greenhouse.io/acme")
    discovery.add_job_board("Globex", "https://jobs.ashbyhq.com/globex")
    discovery.add_job_board("Initech", "https://jobs.lever.co/initech")
    discovery.add_job_board("Acme", "https://acme.wd5.myworkdayjobs.com/Careers")
    ats.calls.clear()

    found = discovery.find_jobs(
        since_days=7, title_keywords=["Engineering Manager"], remote_only=False
    )
    titles = [r["job"]["title"] for r in found["jobs"]]
    # The 40-day-old director role, the sales role and the unmatched title are excluded.
    assert set(titles) == {
        "Senior Engineering Manager, Platform",
        "Engineering Manager",
        "Manager, Software Engineering",
        "Engineering Manager - Payments",
        "Engineering Manager 1",
        "Engineering Manager 2",
    }
    dates = [r["job"]["posted_at"] for r in found["jobs"]]
    assert dates == sorted(dates, reverse=True)
    assert found["errors"] == [] and found["known"] == 0
    assert {b["status"] for b in found["boards"]} == {"fetched"}
    for result in found["jobs"]:
        Job.model_validate(result["job"])
        assert result["known"] is None
    network_calls = len(ats.calls)
    assert network_calls == 4 + 2  # four listings, two Workday descriptions

    repeat = discovery.find_jobs(since_days=7, title_keywords=["Engineering Manager"])
    assert len(ats.calls) == network_calls  # listing and detail caches
    assert {b["status"] for b in repeat["boards"]} == {"cached"}
    # remote_only falls back to the saved profile? No profile here, so default is False.
    assert repeat["filters"]["remote_only"] is False

    remote = discovery.find_jobs(title_keywords=["Engineering Manager"], remote_only=True)
    assert {r["job"]["title"] for r in remote["jobs"]} == {
        "Senior Engineering Manager, Platform",
        "Manager, Software Engineering",
        "Engineering Manager 1",
        "Engineering Manager 2",
    }
    located = discovery.find_jobs(
        title_keywords=["Engineering Manager"], location_keywords=["New York"]
    )
    assert {r["job"]["company"] for r in located["jobs"]} == {"Acme", "Globex"}
    limited = discovery.find_jobs(title_keywords=["Engineering Manager"], limit=2)
    assert limited["returned"] == 2 and limited["matched"] == 6

    discovery.find_jobs(title_keywords=["Engineering Manager"], refresh=True)
    assert len(ats.calls) == network_calls + 4  # listings refetched, details still cached
    with pytest.raises(ValueError):
        discovery.find_jobs(since_days=0)
    with pytest.raises(ValueError):
        discovery.find_jobs(limit=101)


def test_find_jobs_defaults_to_profile_and_marks_history(discovery):
    discovery.add_job_board(careers_url="https://boards.greenhouse.io/acme")
    discovery.save_profile(
        {
            "resume_text": "Managed engineering teams for ten years.",
            "target_roles": ["Director of Engineering", "Engineering Manager"],
            "remote_only": True,
        }
    )
    found = discovery.find_jobs(since_days=60)
    assert found["filters"]["title_keywords_from"] == "profile.target_roles"
    assert found["filters"]["remote_only"] is True
    assert [r["job"]["title"] for r in found["jobs"]] == [
        "Senior Engineering Manager, Platform",
        "Director of Engineering",
    ]
    evaluated = discovery.evaluate(found["jobs"][0]["job"])
    discovery.status(evaluated["job_id"], "dismissed")
    # A copy seen elsewhere with the same posting URL is the same job.
    imported = discovery.import_history(
        [
            {
                "title": "Director of Engineering",
                "company": "Acme",
                "url": "https://job-boards.greenhouse.io/acme/jobs/13?utm_source=x",
                "tracker_stage": "applied",
            }
        ]
    )
    marked = discovery.find_jobs(since_days=60)
    marks = {r["job"]["title"]: r["known"] for r in marked["jobs"]}
    assert marks["Senior Engineering Manager, Platform"] == {
        "job_id": evaluated["job_id"],
        "status": "dismissed",
        "historical": False,
        "evaluated": True,
    }
    assert marks["Director of Engineering"]["job_id"] == imported["results"][0]["job_id"]
    assert marks["Director of Engineering"]["historical"] is True
    assert marked["known"] == 2
    assert discovery.find_jobs(since_days=60, exclude_known=True)["jobs"] == []
    assert discovery.provider.calls == 1  # only the explicit evaluate call


def test_find_jobs_reports_board_errors_and_drops_empty_descriptions(discovery, ats):
    board = discovery.add_job_board(careers_url="https://boards.greenhouse.io/acme")["board"]
    discovery.add_job_board("Initech", "https://jobs.lever.co/initech")
    payload = greenhouse_payload()
    payload["jobs"][1]["content"] = ""
    ats.routes[GH.format("acme") + "?content=true"] = payload
    del ats.routes[LEVER.format("initech")]
    found = discovery.find_jobs(title_keywords=["Engineering Manager"])
    assert found["invalid_postings"] == 1
    assert [e["company"] for e in found["errors"]] == ["Initech"]
    assert "404" in found["errors"][0]["error"]
    assert [r["job"]["source_id"] for r in found["jobs"]] == ["11"]
    rows = {b["company"]: b for b in discovery.list_job_boards()["boards"]}
    assert rows["Initech"]["last_error"] and rows["Acme"]["last_job_count"] == 4
    assert rows["Acme"]["id"] == board["id"] and rows["Acme"]["last_fetched_at"]


def test_find_jobs_without_boards(tmp_path):
    result = Service(tmp_path, FakeProvider()).find_jobs()
    assert result["jobs"] == [] and "add_job_board" in result["note"]


def test_mcp_discovery_tools(tmp_path, monkeypatch, ats):
    from starlette.testclient import TestClient

    from job_matcher.server import create_server

    monkeypatch.setattr("job_matcher.sources.http.urllib_transport", ats)
    monkeypatch.setattr("job_matcher.service.JevProvider", FakeProvider)
    app = create_server(tmp_path).streamable_http_app(json_response=True, stateless_http=True)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:

        def rpc(method, params):
            response = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            assert response.status_code == 200
            return response.json()["result"]

        rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        tools = {t["name"]: t for t in rpc("tools/list", {})["tools"]}
        assert tools["find_jobs"]["annotations"]["readOnlyHint"] is True
        assert tools["find_jobs"]["annotations"]["openWorldHint"] is True
        assert tools["remove_job_board"]["annotations"]["destructiveHint"] is True

        def call(name, arguments):
            result = rpc("tools/call", {"name": name, "arguments": arguments})
            assert not result.get("isError", False), result
            return json.loads(result["content"][0]["text"])

        added = call("add_job_board", {"careers_url": "https://boards.greenhouse.io/acme"})
        assert added["board"]["company"] == "Acme"
        assert call("list_job_boards", {})["count"] == 1
        found = call("find_jobs", {"title_keywords": ["engineering manager"], "since_days": 7})
        assert found["matched"] == 2
        call(
            "save_profile",
            {
                "profile": {
                    "resume_text": "Ten years managing engineers",
                    "target_roles": ["Engineering Manager"],
                }
            },
        )
        call("record_consent", {"user_confirmed": True})
        evaluated = call("evaluate_jobs", {"jobs": [r["job"] for r in found["jobs"]]})
        assert evaluated["errors"] == [] and len(evaluated["results"]) == 2
        assert call("remove_job_board", {"board_id": added["board"]["id"]})["removed"]


def test_cli_board_import_list_and_find(tmp_path):
    path = tmp_path / "boards.jsonl"
    path.write_text(json.dumps({"company": "Acme", "provider": "greenhouse", "token": "acme"}))

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), *args],
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        return result.returncode, json.loads(result.stdout)

    assert cli("boards-import", "--input", str(path)) == (
        0,
        {"imported": 1, "created": 1, "updated": 0, "skipped": [], "errors": []},
    )
    code, listed = cli("boards")
    assert code == 0 and listed["boards"][0]["board_url"] == "https://job-boards.greenhouse.io/acme"
    code, removed = cli("board-remove", "--company", "Acme", "--deactivate")
    assert code == 0 and removed["deactivated"]
    code, found = cli("find", "--keyword", "Engineering Manager", "--no-remote-only")
    assert code == 0 and found["jobs"] == [] and found["boards"] == []
    code, error = cli("board-add")
    assert code == 1 and "company" in error["error"]


def test_salary_ranges_from_posting_text():
    assert salary_ranges("The base pay is $129,000.00 - $210,000.00 per year.") == [
        (129000, 210000)
    ]
    assert salary_ranges("Pay: $200K-$250K plus equity") == [(200000, 250000)]
    assert salary_ranges("USD 200,000–250,000 per year") == [(200000, 250000)]
    zones = "Zone A: $195,000 to $312,000. Zone B: $169,000 - 270,000. Zone A: $195,000 to $312,000"
    assert salary_ranges(zones) == [(195000, 312000), (169000, 270000)]
    assert salary_ranges("$45 - $60 per hour; 401(k) match up to $5,000") == []
    assert salary_ranges(None) == []
