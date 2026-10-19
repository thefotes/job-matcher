"""Browser-free job discovery from public ATS job boards.

Boards are per-user rows in SQLite. find_jobs reads boards concurrently, caches each
listing for CACHE_HOURS, filters, and marks postings already in history. It never calls
TypeSafe; evaluation stays a separate, explicit step.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from job_matcher.errors import error_message
from job_matcher.models import Job
from job_matcher.sources import PROVIDERS, adapter
from job_matcher.sources.base import Board, Posting
from job_matcher.sources.http import Http, SourceError
from job_matcher.sources.resolve import infer_board, same_company, search, summarize
from job_matcher.sources.text import parse_date

CACHE_HOURS = 6
DETAIL_CACHE_HOURS = 72
MAX_DETAILS_PER_BOARD = 40
BOARD_WORKERS = 6
DEADLINE_SECONDS = 150
MAX_IMPORT_BYTES = 5_000_000
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

_STOP = {"of", "the", "and", "for", "a", "an", "in", "to", "at", "on", "with"}
_ALIASES = {"sr": "senior", "mgr": "manager", "eng": "engineering", "engg": "engineering"}


def _words(text):
    words = []
    for word in re.findall(r"[a-z0-9+#]+", (text or "").lower()):
        word = _ALIASES.get(word, word)
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in _STOP:
            words.append(word)
    return words


def title_matches(title, keywords):
    """A keyword matches when all of its words appear in the title, in any order:
    "Engineering Manager" matches "Manager, Software Engineering" and "Sr. Eng Manager"."""
    if not keywords:
        return True
    title_words = set(_words(title))
    return any(words and set(words) <= title_words for words in map(_words, keywords))


def title_key(title):
    """Order-insensitive normalized title, for spotting one opening listed on two sources."""
    return " ".join(sorted(set(_words(title))))


def location_matches(location, keywords):
    if not keywords or not location:
        return True
    return any(re.search(rf"\b{re.escape(k)}\b", location, re.I) for k in keywords if k.strip())


def remote_matches(posting, remote_only):
    """Remote-only keeps postings the ATS or location marks remote, and postings with no
    location information at all; on-site, hybrid and plain city locations are dropped."""
    if not remote_only:
        return True
    return posting.remote is True or (posting.remote is None and not posting.location)


def _timestamp(posting):
    return parse_date(posting.posted_at) or parse_date(posting.updated_at)


def _age(iso_time):
    return datetime.now(timezone.utc) - datetime.fromisoformat(iso_time)


class Discovery:
    def __init__(self, store, http=None):
        self.store = store
        self.http = http or Http()

    # ---- boards -------------------------------------------------------------------

    def _store(self, board, *, board_name, verified, method):
        row, created = self.store.save_board(
            board.model_dump(),
            board_url=adapter(board.provider).careers_url(board),
            board_name=board_name,
            verified=verified,
            method=method,
        )
        return {"added": True, "created": created, "board": row}

    def add_board(self, company=None, careers_url=None):
        company = (company or "").strip()
        careers_url = (careers_url or "").strip()
        if not company and not careers_url:
            raise ValueError("Provide a company name, a careers URL, or both")
        if careers_url:
            return self._add_url(company, careers_url)
        return self._add_company(company)

    def _add_url(self, company, careers_url):
        board = infer_board(careers_url)
        if not board:
            raise ValueError(
                "Unrecognized careers URL. Supported: boards/job-boards.greenhouse.io/<token>, "
                "jobs.ashbyhq.com/<token>, jobs.lever.co/<token>, "
                "careers.smartrecruiters.com/<token>, <token>.recruitee.com, "
                "<tenant>.wdN.myworkdayjobs.com/<site>, or their API URLs"
            )
        try:
            listing = adapter(board.provider).probe(self.http, board)
        except SourceError as exc:
            if exc.status in {404, 410}:
                raise ValueError(f"No public {board.provider} board at that URL") from None
            raise RuntimeError(f"Could not reach the {board.provider} board: {exc}") from None
        # These providers answer 200 for any name, so an empty board proves nothing.
        if not listing.postings and board.provider in {"smartrecruiters", "recruitee"}:
            raise ValueError(f"No public {board.provider} postings at that URL; check it")
        name = company or listing.board_name
        if not name:
            raise ValueError(
                f"{board.provider} does not publish a company name; pass company as well"
            )
        board = board.model_copy(update={"company": name})
        result = self._store(
            board, board_name=listing.board_name, verified=True, method="careers-url"
        )
        result["probe"] = summarize(board, listing)
        warnings = []
        if company and listing.board_name and not same_company(company, listing.board_name):
            warnings.append(
                f"The board calls itself {listing.board_name!r}; confirm it is {company!r}"
            )
        if not listing.postings:
            warnings.append("The board currently lists no postings")
        result["warnings"] = warnings
        return result

    def _add_company(self, company):
        hits, errors = search(self.http, company)
        verified = [h for h in hits if h["verified"]]
        if verified:
            best = max(verified, key=lambda h: h["job_count"])
            board = Board(provider=best["provider"], token=best["token"], company=company)
            result = self._store(
                board, board_name=best["board_name"], verified=True, method=best["method"]
            )
            result["alternatives"] = [h for h in hits if h is not best]
            result["errors"] = errors
            return result
        return {
            "added": False,
            "candidates": hits,
            "errors": errors,
            "next_step": (
                "No board could be verified automatically. Show the candidates (board name "
                "and sample titles) to the user; if one is right, call add_job_board with "
                "company and that candidate's careers_url. Workday boards and custom career "
                "domains are not guessed: ask the user for the careers page URL."
                if hits
                else "No public board found. Ask the user for the company's careers page "
                "URL (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee or Workday)."
            ),
        }

    def list_boards(self, include_inactive=True):
        boards = self.store.boards(active_only=not include_inactive)
        return {"boards": boards, "count": len(boards), "providers": list(PROVIDERS)}

    def remove_board(self, board_id=None, company=None, deactivate=False):
        if board_id is None and not company:
            raise ValueError("Provide board_id or company")
        ids = [
            b["id"]
            for b in self.store.boards()
            if b["id"] == board_id or (company and same_company(b["company"], company))
        ]
        if not ids:
            raise ValueError("No matching job board")
        removed = self.store.remove_boards(ids, deactivate=deactivate)
        return {"deactivated" if deactivate else "removed": removed}

    def import_boards(self, path):
        """Import a JSONL file of boards (connections-finder 02-boards.jsonl format or
        list_job_boards rows). Trusts each row's prior verification; makes no requests."""
        file = Path(path).expanduser()
        if not file.is_absolute():
            raise ValueError("Use an absolute path for the import file")
        if not file.is_file():
            raise ValueError("Import file not found")
        if file.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError("Import file is larger than 5 MB")
        results, errors, skipped = [], [], []
        for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Line is not a JSON object")
                check = row.get("verification")
                if isinstance(check, dict) and check.get("match") is False:
                    skipped.append({"line": number, "reason": "verification.match is false"})
                    continue
                provider = str(row.get("provider") or row.get("ats") or "").strip().lower()
                adapter(provider)
                board = Board(
                    provider=provider,
                    token=str(row.get("token") or row.get("board_token") or "").strip(),
                    shard=str(row.get("shard") or row.get("workday_shard") or "").strip(),
                    site=str(row.get("site") or row.get("workday_site") or "").strip(),
                    company=str(row.get("company") or "").strip(),
                )
                if not board.company:
                    raise ValueError("company is required")
                method = (check.get("method") if isinstance(check, dict) else None) or row.get(
                    "verification_method"
                )
                stored = self._store(
                    board,
                    board_name=row.get("board_name") or None,
                    verified=True,
                    method=f"import:{method}" if method else "import",
                )
                results.append(
                    {
                        "line": number,
                        "board_id": stored["board"]["id"],
                        "created": stored["created"],
                    }
                )
            except (ValueError, ValidationError) as exc:
                errors.append({"line": number, "error": error_message(exc)})
        return {
            "imported": len(results),
            "created": sum(r["created"] for r in results),
            "updated": sum(not r["created"] for r in results),
            "skipped": skipped,
            "errors": errors,
        }

    # ---- discovery ----------------------------------------------------------------

    def _listing(self, row, board, since_days, refresh):
        cached = None if refresh else self.store.board_listing(row["id"])
        if (
            cached
            and _age(cached["fetched_at"]) < timedelta(hours=CACHE_HOURS)
            and (cached["complete"] or (cached["horizon_days"] or 0) >= since_days)
        ):
            return [Posting(**p) for p in cached["postings"]], "cached"
        listing = adapter(board.provider).fetch(self.http, board, since_days)
        postings = [p.model_dump() | {"detail_ref": p.detail_ref} for p in listing.postings]
        self.store.save_board_listing(
            row["id"],
            postings,
            horizon_days=since_days,
            complete=listing.complete,
            job_count=listing.total if listing.total is not None else len(postings),
        )
        return listing.postings, "fetched_capped" if listing.capped else "fetched"

    def _details(self, row, board, postings):
        """Fill descriptions for list-only providers, newest first, within a cap."""
        module = adapter(board.provider)
        pending = [p for p in postings if p.description is None]
        if not pending or not hasattr(module, "detail"):
            return postings, 0, []
        fresh_after = (datetime.now(timezone.utc) - timedelta(hours=DETAIL_CACHE_HOURS)).isoformat()
        cached = self.store.board_postings(row["id"], [p.source_id for p in pending], fresh_after)
        pending.sort(key=lambda p: _timestamp(p) or _EPOCH, reverse=True)
        skipped = max(
            0, len([p for p in pending if p.source_id not in cached]) - MAX_DETAILS_PER_BOARD
        )
        filled, errors, fetched = {}, [], 0
        for stub in pending:
            if stub.source_id in cached:
                filled[stub.source_id] = Posting(**cached[stub.source_id])
                continue
            if fetched >= MAX_DETAILS_PER_BOARD:
                continue
            fetched += 1
            try:
                full = module.detail(self.http, board, stub)
            except SourceError as exc:
                errors.append(f"{stub.title}: {exc}")
                continue
            self.store.save_board_posting(row["id"], stub.source_id, full.model_dump())
            filled[stub.source_id] = full
        complete = [filled.get(p.source_id, p) if p.description is None else p for p in postings]
        return [p for p in complete if p.description is not None], skipped, errors

    def _board(self, row, filters):
        board = Board(**{k: row[k] for k in ("provider", "token", "shard", "site", "company")})
        report = {"board_id": row["id"], "company": row["company"], "provider": row["provider"]}
        try:
            postings, origin = self._listing(row, board, filters["since_days"], filters["refresh"])
        except SourceError as exc:
            message = str(exc) if exc.status != 404 else "Board not found (HTTP 404)"
            if self.store.board_error(row["id"], message, missing=exc.status in {404, 410}):
                report["deactivated"] = True  # three consecutive misses; kept inactive
            return {**report, "status": "error", "error": message}, []
        cutoff = datetime.now(timezone.utc) - timedelta(days=filters["since_days"])
        candidates = [
            p
            for p in postings
            if title_matches(p.title, filters["title_keywords"])
            and (_timestamp(p) is None or _timestamp(p) >= cutoff)
        ]
        candidates, skipped, detail_errors = self._details(row, board, candidates)
        matched = [
            p
            for p in candidates
            if remote_matches(p, filters["remote_only"])
            and location_matches(p.location, filters["location_keywords"])
            and (_timestamp(p) is None or _timestamp(p) >= cutoff)
        ]
        report.update(status=origin.removesuffix("_capped"), postings=len(postings))
        report["matched"] = len(matched)
        if origin.endswith("_capped"):
            report["listing_capped"] = True  # older postings in the window were not read
        if skipped:
            report["details_deferred"] = skipped
        if detail_errors:
            report["detail_errors"] = detail_errors[:5]
        return report, matched

    def find_jobs(
        self,
        profile,
        since_days=7,
        title_keywords=None,
        remote_only=None,
        location_keywords=None,
        limit=25,
        exclude_known=False,
        refresh=False,
    ):
        if not 1 <= since_days <= 365:
            raise ValueError("since_days must be 1–365")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100 (evaluate_jobs accepts up to 100)")
        keywords = [k.strip() for k in title_keywords or [] if k.strip()]
        keyword_source = "argument" if keywords else "none"
        if not keywords and profile and profile.get("target_roles"):
            keywords, keyword_source = list(profile["target_roles"]), "profile.target_roles"
        if remote_only is None:
            remote_only = bool(profile and profile.get("remote_only"))
        filters = {
            "since_days": since_days,
            "title_keywords": keywords,
            "title_keywords_from": keyword_source,
            "remote_only": remote_only,
            "location_keywords": [k for k in location_keywords or [] if k.strip()],
            "exclude_known": exclude_known,
            "refresh": refresh,
        }
        rows = self.store.boards(active_only=True)
        if not rows:
            return {
                "jobs": [],
                "boards": [],
                "errors": [],
                "filters": filters,
                "note": "No active job boards. Add some with add_job_board or "
                "import_job_boards first.",
            }
        pool = ThreadPoolExecutor(BOARD_WORKERS)
        futures = {pool.submit(self._board, row, filters): row for row in rows}
        done, pending = wait(futures, timeout=DEADLINE_SECONDS)
        pool.shutdown(wait=False, cancel_futures=True)
        reports, postings = [], []
        for future, row in futures.items():
            if future in pending:
                reports.append(
                    {
                        "board_id": row["id"],
                        "company": row["company"],
                        "provider": row["provider"],
                        "status": "error",
                        "error": "Timed out; try again later",
                    }
                )
                continue
            try:
                report, matched = future.result()
            except Exception as exc:  # one malformed board must not sink the others
                report, matched = (
                    {
                        "board_id": row["id"],
                        "company": row["company"],
                        "provider": row["provider"],
                        "status": "error",
                        "error": f"Unexpected {type(exc).__name__}",
                    },
                    [],
                )
            reports.append(report)
            postings += [(row["id"], p) for p in matched]
        jobs, seen, invalid = [], set(), 0
        for board_id, posting in postings:
            key = (posting.source, posting.source_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                job = Job.model_validate(posting.job())
            except ValidationError:
                invalid += 1  # e.g. a posting with no real description
                continue
            jobs.append((board_id, posting, job))
        # Newest first; undated postings last.
        jobs.sort(key=lambda item: _timestamp(item[1]) or _EPOCH, reverse=True)
        marks = self.store.known([job for _, _, job in jobs])
        results = [
            {"job": job.model_dump(exclude_none=True), "board_id": board_id, "known": mark}
            for (board_id, _, job), mark in zip(jobs, marks)
            if not (exclude_known and mark)
        ]
        errors = [
            {k: r[k] for k in ("board_id", "company", "provider", "error")}
            for r in reports
            if r["status"] == "error"
        ]
        return {
            "jobs": results[:limit],
            "matched": len(results),
            "returned": min(limit, len(results)),
            "known": sum(1 for r in results if r["known"]),
            "invalid_postings": invalid,
            "boards": sorted(reports, key=lambda r: r["board_id"]),
            "errors": errors,
            "deactivated_boards": [r["company"] for r in reports if r.get("deactivated")],
            "filters": filters,
            "note": "Pass each item's `job` to evaluate_jobs (up to 100) to rank them. "
            "`known` marks postings already in history; known evaluated jobs are served "
            "from cache. Dates use the posting's publish date, else its last update.",
        }
