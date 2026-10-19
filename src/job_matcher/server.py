import argparse
import functools
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from job_matcher.errors import error_message
from job_matcher.models import ExperienceAnswer, Job, Profile, StatusInput, Strictness
from job_matcher.service import Service


def readable_errors(fn):
    """Report a tool's ValueError as a ToolError. The SDK treats any other exception as a
    crash and shows the model only `Error executing tool <name>`, so the message that says
    what to fix would never reach the agent."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(error_message(exc)) from exc

    return wrapper


def create_server(directory=None):
    service = Service(directory)
    server = MCPServer(
        "job-matcher",
        version="0.1.0",
        # Some clients repeat these in every tool description: keep them short.
        instructions="Ranks jobs against the user's saved profile. Scores are fit, not hiring "
        "odds. Evaluation needs the user's recorded consent. For the daily search, call "
        "daily_run. Mark jobs shown only after showing them. Never apply to jobs.",
    )

    def tool(annotations):
        register = server.tool(annotations=annotations)
        return lambda fn: register(readable_errors(fn))

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    external = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    @tool(read)
    def get_onboarding(include_resume: bool = False, include_schema: bool = False) -> dict:
        """Get profile, resumability and a resumable interview plan for experience beyond
        the resume. Compact by default: the resume text and JSON schema are omitted
        (resume_chars reports the resume length). Pass include_resume=true and
        include_schema=true before a full save_profile so the existing resume and schema
        are preserved verbatim."""
        return service.onboarding(include_resume, include_schema)

    @tool(write)
    def save_profile(profile: Profile) -> dict:
        """Save the complete candidate profile and resume, replacing the prior profile."""
        return service.save_profile(profile.model_dump())

    @tool(write)
    def save_experience_answers(answers: list[ExperienceAnswer]) -> dict:
        """Merge 1–9 user-confirmed interview answers by topic, preserving resume/preferences.
        Confirmation is a caller attestation, not independent verification. Read onboarding
        first, ask the user, and save only explicit statements or confirmed summaries.
        This is local storage; subsequent evaluations send this evidence to TypeSafe.
        """
        return service.save_experience_answers([a.model_dump() for a in answers])

    @tool(write)
    def record_consent(user_confirmed: Literal[True]) -> dict:
        """Record the user's agreement to send their profile to TypeSafe for evaluation.
        First show the user get_onboarding's consent.disclosure verbatim and ask; call this
        only if they explicitly agree. user_confirmed is your attestation of that."""
        return service.record_consent(user_confirmed)

    @tool(write)
    def revoke_consent() -> dict:
        """Withdraw consent to send the profile to TypeSafe. Evaluation stops; stored
        history, evaluations and job boards remain available locally."""
        return service.revoke_consent()

    @tool(write)
    def set_strictness(strictness: Strictness, overrides: dict | None = None) -> dict:
        """Set how much benefit of the doubt recommendations give: wide_net, generous,
        balanced (default), selective or strict. Confirmed dealbreakers are skipped at
        every setting. Optional overrides adjust single knobs (e.g. apply_threshold).
        Takes effect immediately on stored evaluations; no new TypeSafe calls.
        """
        return service.set_strictness(strictness, overrides)

    @tool(external)
    def evaluate_job(job: Job, force: bool = False) -> dict:
        """Evaluate and remember a job. Sends the user's profile and the job text to TypeSafe
        (unless cached), as the user authorized via record_consent; refuses without that
        recorded consent. The result includes the consent record relied on."""
        return service.evaluate(job.model_dump(), force)

    @tool(external)
    def evaluate_jobs(jobs: list[Job], force: bool = False) -> dict:
        """Evaluate up to 100 jobs; return ranked unseen matches and per-item errors. Sends
        the user's profile and job text to TypeSafe as the user authorized via
        record_consent; refuses without that recorded consent."""
        return service.batch([job.model_dump() for job in jobs], force)

    @tool(write)
    def import_application_history(entries: list[dict]) -> dict:
        """Import 1–100 metadata entries with title, company, tracker_stage and URL or
        source/source_id. Stages: applied, interview, in_progress. No resume/API needed.
        Per-item validation errors allow partial success. Every stage records status
        applied unless the job already has a decision (user decisions always win). Latest
        tracker_stage is retained; historical entries are always excluded from shortlists.
        No descriptions or judgments.
        """
        return service.import_history(entries)

    @tool(read)
    def get_shortlist(
        view: Literal["new", "open", "all"] = "all",
        limit: int = 20,
        include_stale: bool = False,
        detail: bool = False,
    ) -> dict:
        """Ranked matches under the current profile and strictness. new: never shown.
        open: shown before with no decision, first shown within the profile's
        open_window_days (include_stale=true drops that window). Applied, dismissed and
        closed jobs never appear. `excluded` counts what was left out and why. Compact by
        default; pass detail=true only when a compact row is not enough to act."""
        return service.matches(view, limit, include_stale, detail)

    @tool(read)
    def get_history(
        limit: int = 100,
        detail: bool = False,
        status: StatusInput | None = None,
        query: str | None = None,
    ) -> list[dict]:
        """Read job history and latest evaluation, including application status. Compact
        by default (no full descriptions or raw answers). Filters apply before the
        limit: status keeps one state; query matches company or title case-insensitively
        (answers "have I applied to X?"). Pass detail=true only when a compact row is not
        enough to act."""
        return service.history(limit, detail, status, query)

    @tool(write)
    def set_job_status(
        status: StatusInput,
        job_id: str | None = None,
        job_ids: list[str] | None = None,
        reason: str | None = None,
    ) -> dict:
        """Mark shown (after showing; never changes a decision), applied, dismissed
        (optional reason), closed, or new (reset). Legacy: surfaced/saved mean shown,
        interview/in_progress mean applied. Pass job_id for one job or job_ids for several."""
        if job_ids:
            return service.statuses(job_ids + ([job_id] if job_id else []), status, reason)
        if not job_id:
            raise ValueError("Provide job_id or job_ids")
        return service.status(job_id, status, reason)

    # Job-board discovery: public ATS APIs only; nothing personal is sent.
    board_lookup = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    discover = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    remove = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    @tool(board_lookup)
    def add_job_board(company: str | None = None, careers_url: str | None = None) -> dict:
        """Add a company's public job board (Greenhouse, Ashby, Lever, SmartRecruiters,
        Recruitee or Workday). Prefer careers_url (any careers, job or API URL on those
        hosts). With only a company name, likely boards are probed and stored only if
        verified; otherwise candidates come back for the user to confirm by careers_url.
        """
        return service.add_job_board(company, careers_url)

    @tool(board_lookup)
    def add_boards_from_history(limit: int = 20) -> dict:
        """Find public job boards for companies in the user's application history, most
        recent first. Calls only public job-board APIs and sends nothing personal. Stores
        only verified boards; unverified candidates and unknown companies are reported
        for the user to confirm. Companies that already have a saved board are skipped."""
        return service.add_boards_from_history(limit)

    @tool(read)
    def list_job_boards(include_inactive: bool = True) -> dict:
        """List saved job boards with verification, last fetch time, job count and errors."""
        return service.list_job_boards(include_inactive)

    @tool(remove)
    def remove_job_board(
        board_id: int | None = None, company: str | None = None, deactivate: bool = False
    ) -> dict:
        """Delete a job board (by id, or every board for a company) and its cached postings.
        With deactivate=true the board is kept but skipped by find_jobs."""
        return service.remove_job_board(board_id, company, deactivate)

    @tool(write)
    def import_job_boards(path: str) -> dict:
        """Import boards from a local JSONL file (absolute path), one board per line with
        company, provider/ats, token/board_token and, for Workday, shard and site.
        Rows keep their prior verification; no network requests are made."""
        return service.import_job_boards(path)

    @tool(discover)
    def find_jobs(
        since_days: int = 7,
        title_keywords: list[str] | None = None,
        remote_only: bool | None = None,
        location_keywords: list[str] | None = None,
        limit: int = 25,
        exclude_known: bool = False,
        refresh: bool = False,
    ) -> dict:
        """Find fresh postings on saved job boards, newest first, with full descriptions.
        Does not evaluate: pass each result's `job` to evaluate_jobs. Title keywords match
        when all their words appear in the title; they default to the profile's
        target_roles. remote_only defaults to the profile's setting. location_keywords keep
        postings whose location names any keyword. `known` marks postings already in
        history. Listings are cached for 6 hours unless refresh=true.
        """
        return service.find_jobs(
            since_days=since_days,
            title_keywords=title_keywords,
            remote_only=remote_only,
            location_keywords=location_keywords,
            limit=limit,
            exclude_known=exclude_known,
            refresh=refresh,
        )

    @tool(external)
    def daily_run(
        since_days: int = 7,
        max_new_evaluations: int = 15,
        top_n: int = 10,
        open_n: int = 5,
        refresh: bool = False,
    ) -> dict:
        """The daily routine in one call. Finds fresh postings on saved job boards, skips
        known jobs and likely duplicates, evaluates up to max_new_evaluations new ones
        (newest first; sends the profile to TypeSafe only with recorded consent), records
        postings that left their boards as closed, and returns two views: up to top_n New
        matches (never shown) and up to open_n Still open ones (shown before, no decision).
        report_markdown is the user's deliverable. Marks nothing shown: follow next_step.
        """
        return service.daily_run(since_days, max_new_evaluations, top_n, refresh, open_n)

    return server


def main():
    parser = argparse.ArgumentParser(description="Personal job evaluator MCP server")
    parser.add_argument("--data-dir")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.data_dir)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        # Loopback only: connect using a private tunnel; no unauthenticated public listener.
        server.run(transport="streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
