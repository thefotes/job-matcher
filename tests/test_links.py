import pytest

from job_matcher.daily import _link, report_markdown
from job_matcher.links import is_http_url
from job_matcher.models import Job
from job_matcher.storage import Store


@pytest.mark.parametrize(
    "url",
    [
        "javascript:window.audit=1",
        "data:text/html,test",
        "file:///etc/passwd",
        "https:///missing-host",
        "https://example.invalid:99999/",
        "https://[invalid/",
        "https://trusted.invalid@other.invalid/",
        "https://example.invalid/with space",
        "https://example.invalid/line\nbreak",
        "https://example.invalid/\\escape",
    ],
)
def test_job_and_cached_link_writes_share_url_validation(tmp_path, url):
    data = {"title": "Director", "company": "Acme", "description": "Lead a software team."}
    assert not is_http_url(url)
    assert _link("Employer", url) == ""
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        Job(**data, url=url)
    store = Store(tmp_path)
    job_id, _, _ = store.ingest(Job(**data, url="https://example.invalid/job"))
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        store.fill_job_fields(job_id, {"apply_url": url})
    assert store.history(1)[0]["job"]["apply_url"] is None


def test_report_escapes_content_and_link_destinations():
    job = {
        "company": '<img src="https://tracker.invalid/image">',
        "title": "Director ![image](https://tracker.invalid/image) **fake** | second\nrow",
        "url": "https://example.invalid/job/)[image](https://tracker.invalid/image)",
        "apply_url": "javascript:window.audit=1",
        "checks": ["<script>alert(1)</script> [fake](https://tracker.invalid/)"],
    }
    coverage = {"boards_read": 1, "discovered": 1, "evaluated": 0, "strictness": "balanced"}
    report = report_markdown([job], [], coverage)
    assert "<img" not in report and "<script" not in report
    assert "&lt;img" in report and "&lt;script&gt;" in report
    assert "![image](" not in report and "**fake**" not in report
    assert "\\!\\[image\\]\\(" in report
    assert "%29[image]%28" in report
    assert "javascript:" not in report
    assert "| second" not in report
    assert len([line for line in report.splitlines() if line.startswith("| ")]) == 2


@pytest.mark.parametrize("url", ["https://example.invalid/job(1)", "http://[::1]:8765/job"])
def test_safe_links_remain_clickable(url):
    assert is_http_url(url)
    link = _link("Employer", url)
    assert link.startswith("[Employer](http")
    if "(" in url:
        assert "job%281%29" in link
    else:
        assert "[::1]" in link
