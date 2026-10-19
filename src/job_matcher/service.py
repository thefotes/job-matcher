import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import TypeAdapter

from job_matcher import consent, views
from job_matcher.errors import error_message
from job_matcher.models import (
    ExperienceAnswer,
    HistoryEntry,
    Job,
    Policy,
    Profile,
    StatusInput,
)
from job_matcher.onboarding import interview, strictness_options
from job_matcher.provider import JevProvider
from job_matcher.questions import VERSION
from job_matcher.scoring import summarize
from job_matcher.sources.resolve import same_company
from job_matcher.storage import Store, digest, now
from job_matcher.views import job_summary

SOURCE_ROOT = Path(__file__).resolve().parents[2]
ROOT = SOURCE_ROOT if (SOURCE_ROOT / "pyproject.toml").exists() else Path.cwd()


def data_directory():
    load_dotenv(os.environ.get("JOB_MATCHER_ENV_FILE", ROOT / ".env"))
    return os.environ.get("JOB_MATCHER_DATA_DIR", str(ROOT / ".job-matcher"))


def job_hash(data):
    return digest({key: data[key] for key in ("title", "company", "description")})


def rescore(profile, evaluation):
    """Re-derive the recommendation from stored Jev answers under current scoring rules.

    Scoring changes therefore apply to past evaluations without another paid call.
    """
    if not evaluation or profile is None:
        return evaluation
    return {**evaluation, **summarize(profile, evaluation)}


class Service:
    def __init__(self, directory=None, provider=None):
        default_directory = data_directory()
        self.store = Store(directory or default_directory)
        self.provider = provider or JevProvider()

    def onboarding(self, include_resume=True, include_schema=True):
        saved = self.store.profile()
        profile = Profile.model_validate(saved) if saved else Profile()
        agreement = self.consent()
        missing = profile.missing_required()
        resume_text = profile.resume_text
        result = {
            "profile": profile.model_dump(),
            "ready": bool(resume_text),
            "missing": missing + ([] if agreement["granted"] else ["consent"]),
            "steps": [
                "Review target roles, salary, location, and authorization",
                "Supply resume_text as plain text (agent can extract PDF/DOCX)",
                "Add preferences and dealbreakers; save the complete profile",
                "Ask how selective to be and record it with set_strictness",
                "Show consent.disclosure; if the user agrees, call record_consent",
            ],
            "consent": agreement,
            "strictness_options": strictness_options(),
            "interview": interview(profile),
        }
        if include_schema:
            result["schema"] = Profile.model_json_schema()
        if not include_resume:
            result["profile"]["resume_text"] = ""
            result["resume_chars"] = len(resume_text or "")
        return result

    def save_experience_answers(self, data):
        saved = self.store.profile()
        if not saved:
            raise ValueError("Save the resume/profile before recording interview answers")
        answers = TypeAdapter(list[ExperienceAnswer]).validate_python(data)
        if not 1 <= len(answers) <= 9:
            raise ValueError("Submit 1–9 confirmed experience answers")
        if len({a.topic for a in answers}) != len(answers):
            raise ValueError("Experience topics must be unique")
        profile = Profile.model_validate(saved)
        merged = {a.topic: a for a in profile.experience_answers}
        merged.update({a.topic: a for a in answers})
        profile.experience_answers = list(merged.values())
        result = self.save_profile(profile.model_dump())
        return {**result, "interview": interview(profile)}

    def save_profile(self, data):
        profile = Profile.model_validate(data)
        self.store.save_profile(profile)
        return {
            "saved": True,
            "ready": bool(profile.resume_text),
            "profile_hash": digest(profile.evidence()),
        }

    def consent(self):
        return consent.status(self.store.consent(consent.PROVIDER))

    def record_consent(self, user_confirmed):
        """Record that the user read the disclosure and agreed. user_confirmed is the
        caller's attestation, like interview answers; the server cannot verify it."""
        if user_confirmed is not True:
            raise ValueError("Record consent only after the user explicitly agrees")
        self.store.save_consent(consent.PROVIDER, consent.VERSION, consent.DISCLOSURE)
        return self.consent()

    def revoke_consent(self):
        revoked = self.store.revoke_consent(consent.PROVIDER)
        return {"revoked": revoked, **self.consent()}

    def set_strictness(self, strictness, overrides=None):
        """Change the benefit-of-the-doubt dial. Stored answers are re-scored on read,
        so this takes effect immediately with no new evaluations."""
        saved = self.store.profile()
        if not saved:
            raise ValueError("Save the resume/profile before setting strictness")
        profile = Profile.model_validate(saved)
        profile.policy = Policy.model_validate({"strictness": strictness, **(overrides or {})})
        self.save_profile(profile.model_dump())
        return {"policy": profile.policy.model_dump(), "effective": profile.policy.resolved()}

    def evaluate(self, data, force=False):
        job = Job.model_validate(data)
        granted = consent.require(self.store.consent(consent.PROVIDER))
        saved = self.store.profile()
        if not saved:
            raise ValueError("Complete onboarding and save a profile before evaluating")
        profile = Profile.model_validate(saved)
        missing = profile.missing_required()
        if missing:
            raise ValueError(
                "Profile is not ready to evaluate; missing: "
                + ", ".join(missing)
                + ". Finish onboarding first (get_onboarding shows what is missing)."
            )
        job_id, status, duplicate = self.store.ingest(job)
        profile_hash = digest(profile.evidence())
        cache_key = digest(
            {
                "profile": profile_hash,
                "job": {"title": job.title, "company": job.company, "description": job.description},
                "questions_version": VERSION,
                "model": self.provider.model,
            }
        )
        cached = self.store.cached(job_id, cache_key)
        if cached and not force:
            result = rescore(profile, cached)
            cache_hit = True
        else:
            response = self.provider.evaluate(profile, job)
            result = {
                **summarize(profile, response),
                "model": response.get("model"),
                "usage": response.get("usage"),
                "answers": response["answers"],
                "profile_hash": profile_hash,
                "job_hash": job_hash(job.model_dump()),
                "job_snapshot": job.model_dump(),
                "evaluated_at": now(),
                "questions_version": VERSION,
            }
            self.store.save_evaluation(job_id, cache_key, result)
            cache_hit = False
        return {
            "job_id": job_id,
            "job": job.model_dump(),
            "status": status,
            "duplicate": duplicate,
            "historical": self.store.is_historical(job_id),
            "cache_hit": cache_hit,
            "consent": granted,
            "evaluation": result,
        }

    def import_history(self, entries):
        """Import 1–100 observed entries independently, without profile or provider calls.

        Every tracker stage records the decision applied (origin import) unless the job
        already has a decision; a user's decision is never overwritten. tracker_stage
        retains the latest observation verbatim. Historical records remain suppressed
        from shortlists, including after status=new.
        """
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("Submit a list of 1–100 history entries")
        results, errors = [], []
        for index, data in enumerate(entries):
            try:
                entry = HistoryEntry.model_validate(data)
                job_id, status, duplicate = self.store.ingest(entry, historical=True)
                results.append(
                    {
                        "index": index,
                        "job_id": job_id,
                        "status": status,
                        "tracker_stage": entry.tracker_stage,
                        "duplicate": duplicate,
                    }
                )
            except ValueError as exc:
                errors.append({"index": index, "error": error_message(exc)})
        return {"results": results, "errors": errors}

    def batch(self, jobs, force=False):
        if not 1 <= len(jobs) <= 100:
            raise ValueError("Submit 1–100 jobs per batch")
        # Refuse the whole batch once rather than repeating the same error per job.
        granted = consent.require(self.store.consent(consent.PROVIDER))
        results, errors = [], []
        for index, data in enumerate(jobs):
            try:
                results.append(self.evaluate(data, force=force))
            except (ValueError, RuntimeError) as exc:
                errors.append({"index": index, "error": error_message(exc)})
        unique = {r["job_id"]: r for r in results}
        shortlist = [
            r
            for r in unique.values()
            if r["status"] == "new"
            and not r["historical"]
            and r["evaluation"]["recommendation"] != "skip"
        ]
        shortlist.sort(key=lambda r: r["evaluation"]["fit_percent"], reverse=True)
        return {
            "shortlist": shortlist,
            "results": results,
            "errors": errors,
            "consent": granted,
            "note": "After presenting jobs, mark their status shown to suppress repeats.",
        }

    @staticmethod
    def _status_write(status, reason):
        """Validate a status write: legacy aliases map to their state; reason goes only
        with dismissed."""
        validated = TypeAdapter(StatusInput).validate_python(status)
        reason = reason.strip() if reason else None
        if reason and validated != "dismissed":
            raise ValueError("reason is only stored with status dismissed")
        if reason and len(reason) > 2000:
            raise ValueError("reason must be at most 2000 characters")
        return validated, reason or None

    def status(self, job_id, status, reason=None):
        validated, reason = self._status_write(status, reason)
        return self.store.set_status(job_id, validated, reason)

    def statuses(self, job_ids, status, reason=None):
        """Set one status on several jobs; unknown IDs are reported, not fatal."""
        validated, reason = self._status_write(status, reason)
        updated, unknown = [], []
        for job_id in dict.fromkeys(job_ids):
            try:
                self.store.set_status(job_id, validated, reason)
                updated.append(job_id)
            except ValueError:
                unknown.append(job_id)
        return {"status": validated, "updated": updated, "unknown": unknown}

    def history(self, limit=100, detail=True, status=None, query=None):
        """Read stored history. Filters apply before the limit: status keeps rows whose
        status equals it; query matches company or title case-insensitively. Scans up to
        1000 stored rows and stops once limit matches are collected."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1–1000")
        if status is not None:
            status = TypeAdapter(StatusInput).validate_python(status)
        saved = self.store.profile()
        profile = Profile.model_validate(saved) if saved else None
        query_lower = query.lower() if query else None
        results = []
        for row in self.store.history(1000):
            if status is not None and row["status"] != status:
                continue
            if query_lower is not None:
                job = row["job"] or {}
                haystack = f"{job.get('company', '')} {job.get('title', '')}".lower()
                if query_lower not in haystack:
                    continue
            entry = {**row, "evaluation": rescore(profile, row["evaluation"])}
            results.append(entry if detail else job_summary(entry))
            if len(results) >= limit:
                break
        return results

    def matches(self, view="all", limit=20, include_stale=False, detail=False):
        """The New and Still open views (docs/design-matching-states.md) with excluded
        counts. view: new, open or all."""
        if view not in {"new", "open", "all"}:
            raise ValueError("view must be new, open or all")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1–1000")
        saved = self.store.profile()
        profile = Profile.model_validate(saved) if saved else Profile()
        found = views.matches(
            self.history(1000, detail=True),
            digest(profile.evidence()) if saved else None,
            profile.policy.open_window_days,
            include_stale=include_stale,
        )
        shape = (lambda r: r) if detail else job_summary
        result = {"view": view, "open_window_days": profile.policy.open_window_days}
        for name in ("new", "open"):
            if view in {name, "all"}:
                result[name] = [shape(r) for r in found[name][:limit]]
        result["excluded"] = found["excluded"]
        if found["excluded"]["stale"] and not include_stale:
            result["note"] = (
                f"{found['excluded']['stale']} shown jobs are older than "
                f"{profile.policy.open_window_days} days; pass include_stale=true to see them."
            )
        return result

    def shortlist(self, limit=50, detail=True, view="new"):
        """The matches view as one ranked list (new first when view is all)."""
        found = self.matches(view, limit, detail=detail)
        return (found.get("new", []) + found.get("open", []))[:limit]

    # Job-board discovery (public ATS APIs). Never calls the evaluation provider.

    @property
    def discovery(self):
        if getattr(self, "_discovery", None) is None:
            from job_matcher.discovery import Discovery

            self._discovery = Discovery(self.store)
        return self._discovery

    def add_job_board(self, company=None, careers_url=None):
        return self.discovery.add_board(company, careers_url)

    def list_job_boards(self, include_inactive=True):
        return self.discovery.list_boards(include_inactive)

    def remove_job_board(self, board_id=None, company=None, deactivate=False):
        return self.discovery.remove_board(board_id, company, deactivate)

    def add_boards_from_history(self, limit=20):
        """Probe public boards for companies in application history, most recent first.

        Companies that already have a saved board (same_company) are skipped. Calls the
        public job-board lookups only; nothing personal is sent. One failure does not
        stop the rest."""
        existing = self.store.boards()
        unique = []
        for row in self.store.history(1000):
            company = (row["job"].get("company") or "").strip()
            if not company:
                continue
            if any(same_company(company, other) for other in unique):
                continue
            if any(same_company(company, board["company"]) for board in existing):
                continue
            unique.append(company)
        result = {"added": [], "unverified": [], "not_found": [], "errors": [], "remaining": []}
        for index, company in enumerate(unique):
            if index >= limit:
                result["remaining"] = unique[index:]
                break
            try:
                outcome = self.add_job_board(company)
            except (ValueError, RuntimeError) as exc:
                result["errors"].append({"company": company, "error": error_message(exc)})
                continue
            if outcome.get("added"):
                result["added"].append(
                    {
                        "company": company,
                        "provider": outcome["board"]["provider"],
                        "board_id": outcome["board"]["id"],
                    }
                )
            elif outcome.get("candidates"):
                result["unverified"].append(
                    {"company": company, "candidates": len(outcome["candidates"])}
                )
            else:
                result["not_found"].append(company)
        return result

    def import_job_boards(self, path):
        return self.discovery.import_boards(path)

    def find_jobs(self, **filters):
        return self.discovery.find_jobs(self.store.profile(), **filters)

    # The daily routine as one call (see daily.py).

    def daily_run(self, since_days=7, max_new_evaluations=15, top_n=10, refresh=False, open_n=5):
        from job_matcher import daily

        return daily.run(self, since_days, max_new_evaluations, top_n, refresh, open_n)
