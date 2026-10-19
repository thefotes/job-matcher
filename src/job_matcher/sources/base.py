import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator, model_validator

from job_matcher.sources.http import SourceError

TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class Board(BaseModel):
    """A public ATS board. Workday boards also need a shard (wdN) and a career site."""

    provider: str
    token: str
    shard: str = ""
    site: str = ""
    company: str = ""

    @field_validator("token")
    @classmethod
    def safe_token(cls, value):
        # Tokens become URL path segments and, for Recruitee/Workday, hostnames.
        if not TOKEN.fullmatch(value) or ".." in value:
            raise ValueError("Board token may contain only letters, digits, '.', '_' and '-'")
        return value

    @model_validator(mode="after")
    def workday_parts(self):
        if self.provider == "workday":
            if not re.fullmatch(r"wd\d{1,3}", self.shard):
                raise ValueError("Workday boards require a shard such as wd5")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.site):
                raise ValueError("Workday boards require a career site name")
        elif self.shard or self.site:
            raise ValueError("Only Workday boards have a shard and site")
        return self


class Posting(BaseModel):
    """A normalized posting. ``description`` is None until a detail fetch fills it."""

    title: str
    company: str
    description: str | None = None
    url: str | None = None
    apply_url: str | None = None
    location: str | None = None
    remote: bool | None = None
    compensation: str | None = None
    posted_at: str | None = None
    updated_at: str | None = None
    source: str
    source_id: str
    detail_ref: str | None = Field(default=None, exclude=True)

    def job(self):
        """Input for the Job model / evaluate_jobs (empty optional values omitted)."""
        data = self.model_dump(exclude={"detail_ref"})
        data["title"] = data["title"][:500]
        data["company"] = data["company"][:500]
        data["description"] = (data["description"] or "")[:150000]
        if data["apply_url"] == data["url"]:
            data["apply_url"] = None
        return {k: v for k, v in data.items() if v not in (None, "")}


@dataclass
class Listing:
    postings: list = field(default_factory=list)
    board_name: str | None = None
    total: int | None = None
    complete: bool = True
    capped: bool = False  # stopped at a page cap before covering the horizon


def require(payload, kind, message="Board not found"):
    """Validate a decoded payload's top-level type; a missing board raises SourceError."""
    if payload is None:
        raise SourceError(message, 404)
    if not isinstance(payload, kind):
        raise SourceError("Unexpected response shape")
    return payload


def text(value):
    return str(value).strip() if value not in (None, "") else ""
