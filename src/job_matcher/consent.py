"""The user's recorded agreement to send their profile to TypeSafe for evaluation.

Evaluation refuses until consent for the current disclosure version is recorded. Bump
VERSION whenever what is sent, or to whom, changes materially; users are then asked again.
"""

PROVIDER = "typesafe"
VERSION = "1"
DISCLOSURE = (
    "To score a job, job-matcher sends your saved profile to TypeSafe (typesafe.ai), using "
    "your own TypeSafe API key: your name, resume text, confirmed interview answers, target "
    "roles, salary floor and target, location, remote and work-authorization requirements, "
    "preferences and dealbreakers, together with the job posting. Your strictness setting, "
    "job history and application statuses stay on this computer. Each evaluation is billed "
    "to your key (roughly 5,000 input tokens per job in testing); repeat evaluations of an "
    "unchanged job and profile are served from the local cache. TypeSafe's own terms and "
    "privacy policy govern what it does with requests. You can revoke consent at any time; "
    "evaluation then stops, and everything already stored locally remains available."
)


class ConsentRequired(ValueError):
    """Raised when an evaluation would send data to TypeSafe without recorded consent."""

    def __init__(self, reason):
        super().__init__(
            f"{reason} Evaluation sends the user's profile to TypeSafe. Show the user "
            "get_onboarding's consent.disclosure; only if they explicitly agree, call "
            "record_consent with user_confirmed=true (CLI: job-matcher consent grant)."
        )


def status(record):
    """Describe the stored consent record against the current disclosure."""
    current = bool(record) and record["version"] == VERSION
    return {
        "granted": current,
        "record": record,
        "needs_renewal": bool(record) and not current,
        "provider": PROVIDER,
        "version": VERSION,
        "disclosure": DISCLOSURE,
        "record_with": "record_consent",
    }


def require(record):
    if not record:
        raise ConsentRequired("The user has not consented to sending their profile to TypeSafe.")
    if record["version"] != VERSION:
        raise ConsentRequired("What is sent to TypeSafe has changed since the user consented.")
    return {k: record[k] for k in ("provider", "version", "granted_at")}
