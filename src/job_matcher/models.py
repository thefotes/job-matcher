from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from job_matcher.links import is_http_url


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


Strictness = Literal["wide_net", "generous", "balanced", "selective", "strict"]
Doubt = Literal["apply", "investigate", "skip"]

# How much benefit of the doubt a candidate gets. Every knob moves the same way from
# wide_net to strict: a higher qualification bar, more certainty needed before a job is
# surfaced, less certainty needed before a likely conflict hides it, and doubtful jobs
# sent further from "apply". Confirmed conflicts are skipped at every stop.
STRICTNESS = {
    "wide_net": {
        "description": "Show nearly everything plausible; doubts never hide a job.",
        "apply_threshold": 60,
        "stretch_threshold": 40,
        "conflict_confidence": 0.85,
        "conflict_risk_threshold": 0.5,
        "settled_threshold": 0.45,
        "doubt": "apply",
        "unstated_is_doubt": False,
    },
    "generous": {
        "description": "Lean toward applying; doubts become notes, not blockers.",
        "apply_threshold": 65,
        "stretch_threshold": 45,
        "conflict_confidence": 0.75,
        "conflict_risk_threshold": 0.35,
        "settled_threshold": 0.55,
        "doubt": "apply",
        "unstated_is_doubt": False,
    },
    "balanced": {
        "description": "Surface good matches; real doubts are flagged to investigate.",
        "apply_threshold": 70,
        "stretch_threshold": 50,
        "conflict_confidence": 0.65,
        "conflict_risk_threshold": 0.25,
        "settled_threshold": 0.65,
        "doubt": "investigate",
        "unstated_is_doubt": False,
    },
    "selective": {
        "description": "Strong matches only; missing details also need investigating.",
        "apply_threshold": 72,
        "stretch_threshold": 60,
        "conflict_confidence": 0.55,
        "conflict_risk_threshold": 0.15,
        "settled_threshold": 0.75,
        "doubt": "investigate",
        "unstated_is_doubt": True,
    },
    "strict": {
        "description": "Only clear, confirmed, strong matches; any doubt hides the job.",
        "apply_threshold": 75,
        "stretch_threshold": 75,
        "conflict_confidence": 0.45,
        "conflict_risk_threshold": 0.1,
        "settled_threshold": 0.85,
        "doubt": "skip",
        "unstated_is_doubt": True,
    },
}
# Pre-dial profiles stored these values; they matched what is now "balanced".
LEGACY_POLICY = {"apply_threshold": 70, "stretch_threshold": 50, "conflict_risk_threshold": 0.25}


class Policy(StrictModel):
    """A strictness stop plus optional per-knob overrides (None follows the stop)."""

    strictness: Strictness = "balanced"
    apply_threshold: float | None = Field(default=None, ge=0, le=100)
    stretch_threshold: float | None = Field(default=None, ge=0, le=100)
    allow_stretch: bool | None = None
    conflict_confidence: float | None = Field(default=None, ge=0, le=1)
    conflict_risk_threshold: float | None = Field(default=None, ge=0, le=1)
    settled_threshold: float | None = Field(default=None, ge=0, le=1)
    doubt: Doubt | None = None
    unstated_is_doubt: bool | None = None
    # A local view setting: how long a shown job stays on the "still open" list.
    open_window_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="before")
    @classmethod
    def legacy(cls, data):
        if not isinstance(data, dict) or "strictness" in data:
            return data
        data = {k: v for k, v in data.items() if LEGACY_POLICY.get(k, object()) != v}
        # confidence_threshold used to gate both skipping and rating certainty.
        confidence = data.pop("confidence_threshold", 0.65)
        if confidence != 0.65:
            data.setdefault("conflict_confidence", confidence)
            data.setdefault("settled_threshold", confidence)
        if data.get("allow_stretch") is True:
            del data["allow_stretch"]
        return data

    @model_validator(mode="after")
    def thresholds(self):
        resolved = self.resolved()
        if resolved["stretch_threshold"] > resolved["apply_threshold"]:
            raise ValueError("stretch_threshold must not exceed apply_threshold")
        return self

    def resolved(self):
        """The effective knob values: the strictness stop with overrides applied."""
        values = {k: v for k, v in STRICTNESS[self.strictness].items() if k != "description"}
        overrides = self.model_dump(
            exclude={"strictness", "allow_stretch", "open_window_days"}, exclude_none=True
        )
        values.update(overrides)
        if self.allow_stretch is False:
            values["stretch_threshold"] = values["apply_threshold"]
        return values


ExperienceTopic = Literal[
    "engineering_tenure",
    "management_tenure",
    "leadership_scope",
    "hands_on",
    "technical_scope",
    "developer_tooling",
    "ai_development",
    "ai_products",
    "domains",
]


class ExperienceAnswer(StrictModel):
    topic: ExperienceTopic
    status: Literal["reported", "no_experience", "skipped"]
    statement: str = Field(default="", max_length=8000)
    user_confirmed: Literal[True]
    source: Literal["user_reported"] = "user_reported"

    @model_validator(mode="after")
    def evidence(self):
        if self.status == "reported" and not self.statement:
            raise ValueError("Reported experience requires the user's statement")
        if self.status == "skipped" and self.statement:
            raise ValueError("Skipped topics cannot contain experience assertions")
        return self


class Profile(StrictModel):
    """The candidate. Defaults are empty/neutral: nothing personal is assumed.

    Evaluation refuses (missing_required) until the user has set resume_text and
    target_roles, and either a location or remote_only. 0 means "not set" for the
    salary fields; an empty string means "not set" for the text fields.
    """

    name: str = Field(default="Candidate", min_length=1)
    resume_text: str = Field(default="", max_length=150000)
    target_roles: list[str] = Field(default_factory=list)
    salary_floor: int = Field(default=0, ge=0)
    salary_target: int = Field(default=0, ge=0)
    salary_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    # Open-to-remote is the neutral default; it is not a personal constraint until
    # the user says otherwise (and supplies a location for location checks).
    remote_only: bool = True
    location: str = ""
    work_authorization: str = ""
    preferences: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    experience_answers: list[ExperienceAnswer] = Field(default_factory=list, max_length=9)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def salary(self):
        if self.salary_target and self.salary_target < self.salary_floor:
            raise ValueError("salary_target must be at least salary_floor")
        return self

    def missing_required(self):
        """Fields the readiness gate (service.evaluate) requires before evaluation."""
        missing = []
        if not self.resume_text.strip():
            missing.append("resume_text")
        if not self.target_roles:
            missing.append("target_roles")
        if not self.location.strip() and not self.remote_only:
            missing.append("location (or set remote_only=true)")
        return missing

    @model_validator(mode="after")
    def unique_topics(self):
        topics = [a.topic for a in self.experience_answers]
        if len(topics) != len(set(topics)):
            raise ValueError("Experience topics must be unique")
        return self

    def evidence(self):
        """What Jev evaluates against. Policy is a local decision about Jev's answers, so
        changing strictness never requires (or pays for) another evaluation."""
        return self.model_dump(exclude={"policy"})


class Job(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    company: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=20, max_length=150000)
    url: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=200)
    source_id: str | None = Field(default=None, max_length=500)
    posted_at: str | None = None
    # Optional structured facts from job boards (find_jobs); evidence alongside the text.
    apply_url: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=1000)
    remote: bool | None = None
    compensation: str | None = Field(default=None, max_length=1000)
    updated_at: str | None = None

    @model_validator(mode="after")
    def apply_link(self):
        if self.apply_url and not is_http_url(self.apply_url):
            raise ValueError("apply_url must be an HTTP or HTTPS URL with a hostname")
        return self

    @model_validator(mode="after")
    def source_pair(self):
        if self.source_id and not self.source:
            raise ValueError("source is required with source_id")
        if self.url and not is_http_url(self.url):
            raise ValueError("url must be an HTTP or HTTPS URL with a hostname")
        return self


class HistoryEntry(Job):
    """Observed tracker metadata; never implies a qualification assessment."""

    description: None = None
    tracker_stage: Literal["applied", "interview", "in_progress"]

    @model_validator(mode="after")
    def stable_identity(self):
        if self.url and not urlsplit(self.url).hostname:
            raise ValueError("url must include a hostname")
        if not self.url and not self.source_id:
            raise ValueError("History entries require url or source and source_id")
        return self


# Matching states (docs/design-matching-states.md). new and shown are derived from the show
# log; applied and dismissed are decisions; closed means the posting is gone.
Status = Literal["new", "shown", "applied", "dismissed", "closed"]
LEGACY_STATUSES = {
    "surfaced": "shown",
    "saved": "shown",
    "interview": "applied",
    "in_progress": "applied",
}
# What callers may send: the states plus legacy aliases, which map to their state.
StatusInput = Annotated[
    Literal[
        "new",
        "shown",
        "applied",
        "dismissed",
        "closed",
        "surfaced",
        "saved",
        "interview",
        "in_progress",
    ],
    AfterValidator(lambda value: LEGACY_STATUSES.get(value, value)),
]
