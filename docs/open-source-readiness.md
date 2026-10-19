# Open-source readiness

A triage of what job-matcher needs before strangers can install it, trust it and get value
from it. Written 2026-10-01, after the first week of real use.

**Verdict:** the core works. Jev's ranking is useful once the decision layer stops second-
guessing it, deduplication and history are solid, and per-user state already lives in
SQLite. What was missing is mostly *de-personalization*, *packaging* and *moving workflow
out of long agent prompts into tools*. Nothing here needs a rewrite.

Sizes: **S** = an hour or two, **M** = a focused session, **L** = several sessions.

## What is already in good shape

- **Per-user state in SQLite, nothing personal in code paths.** Profile, interview evidence,
  job history, statuses and Jev answers live in the user's own database, created empty.
- **Deduplication and history.** LinkedIn IDs, canonical URLs and exact content hashes;
  imported application history suppresses resurfacing; explicit user statuses win.
- **Cost control.** Evaluations are cached; Jev's raw answers are stored, so scoring rules and
  the strictness dial re-apply to old evaluations with no new API calls.
- **Strictness dial.** Five stops from `wide_net` to `strict`, asked in the interview,
  property-tested so a stricter stop never upgrades a recommendation. The stop values
  are initial heuristics; tune them against real outcomes (see #15).
- **Browser-free discovery** from six job-board systems (see Discovery).
- **Safety posture.** Job text treated as untrusted evidence; SDK errors never echo resume
  text; loopback-only HTTP; owner-only file permissions.
- **Tests.** Unit, CLI and both MCP transports, with no paid calls.

## Blockers for a public release

| # | Item | Why it blocks | Size |
|---|---|---|---|
| 1 | ~~**Empty defaults + readiness gate.**~~ **Done:** `Profile` defaults are empty (`models.py`), and `service.evaluate` / `daily_run` refuse with a listed error until `resume_text`, `target_roles`, and a location or `remote_only` are set. `examples/profile.json` uses placeholder values. | A new user silently inherits one person's job search. | S |
| 2 | **Role-agnostic questions and interview.** The role question derives its wording from `target_roles`, but qualification is still 40% people management for everyone; the nine interview topics assume engineering managers. Add a track (individual contributor vs. manager) chosen in the interview that sets the leadership/technical/seniority weights, and split interview topics into a universal core (tenure, scope, hands-on work, domains, outcomes) plus track packs. | A designer, PM or IC engineer gets graded on managing engineers. | M |
| 3 | ~~**De-personalize the docs.**~~ **Done:** the recurring prompt, README and `examples/profile.json` all read from the profile; defaults are empty. | Same as 1. | S |
| 4 | ~~**Explicit consent for sending data to TypeSafe.**~~ **Done 2026-10-01:** versioned, timestamped consent; evaluation refuses without it; `record_consent` / `revoke_consent` / `job-matcher consent`. Record consent (with timestamp) during onboarding and state it in the evaluate tools' descriptions. On 2026-09-30 Codex's auto-reviewer blocked `evaluate_jobs` as "exporting the resume to an untrusted service without specific authorization", and the agent silently fell back to its own judgment. Also tell users the rough cost (~4.7k input tokens per evaluation). | The core feature can be blocked by client safety layers, and users deserve to know what leaves their machine. | S |
| 5 | **Packaging.** Publish to PyPI so `uvx job-matcher-mcp` works; default the data directory to a per-user location (e.g. `~/.local/share/job-matcher` via platformdirs) instead of the checkout; a `job-matcher setup` command that stores the key and prints a ready-to-paste config block for Claude Code/Desktop, Codex and Cursor. | Today setup means cloning, `uv sync`, editing `.env` and hand-writing absolute paths. | M |
| 6 | **Repository hygiene.** `git init`, a license (MIT or Apache-2.0), CI running pytest + ruff, CONTRIBUTING, SECURITY (how to report issues; the data-handling summary), CHANGELOG and versioning. | Can't publish without it. | S |
| 7 | **Ship the prompts as MCP prompts.** Register onboarding and the daily run as MCP prompts so every client offers them natively, instead of users copying markdown. | Portability across agents is the whole pitch. | S |

## Product gaps that hurt real use

| # | Item | Evidence | Size |
|---|---|---|---|
| 8 | **Hard exclusions.** An `exclusions` table (company, reason, optional expiry) applied deterministically *before* any Jev call, with `exclude_company` / `list_exclusions` tools. Today a "don't recommend company X" request becomes free text sent to Jev on every call, and adding it invalidated every cached evaluation. | Free-text dealbreakers are the only exclusion mechanism today | S |
| 9 | **Company-level history warning.** When a new posting's company matches a recent application or rejection, attach "you applied here 3 weeks ago (not moving forward)" without merging the records. | Nothing links a new posting to company-level history | S |
| 10 | **Liveness check.** *Done 2026-10-01 for board jobs in `daily_run`: `live` flag and a `gone` list; statuses are not changed automatically.* Verify a posting still exists before surfacing it. Job-board sources make this nearly free: a posting missing from its board is closed. | Dead links waste a daily-run slot | S |
| 11 | **Evidence quotes.** Return the posting sentences behind the remote, location and salary judgments. Jev returns labels, not reasons; job-board structured fields (location, remote flag, compensation) now cover part of this. | A "remote-friendly" role can still prefer candidates near an office | M |
| 12 | **One-call daily run.** *Done 2026-10-01: `daily_run` / `job-matcher daily`; the recurring prompt shrank from ~1,600 to ~500 words.* A `daily_run` tool (fetch from boards → skip known/excluded → evaluate new → rank under the user's strictness → return top N with LinkedIn and employer links) replacing most of the ~2,000-word recurring prompt. Prompts are followed partially; code is followed every time. | Long prompts are followed partially by agents | M |
| 13 | **Partial profile updates.** `update_profile` (patch) alongside `save_profile` (replace); a full replace is risky for agents. | — | S |
| 14 | **Lookups and pagination.** `find_job(url or company)` and paginated history; `get_history` caps at 1,000 with no cursor. | "Have I applied to X?" | S |
| 15 | **Outcome statuses and calibration.** Add `rejected` (distinct from `closed`), record interview/offer outcomes, and periodically compare outcomes against fit and strictness to tune the stop values and weights, which are guesses today. | — | M |
| 16 | **Interview evidence review.** A tool that shows saved answers and flags hedged or verbatim conversational text. Hedged verbatim answers measurably lower qualification scores. The interview instructions now ask for confirmed factual summaries; existing users need a way to fix old answers. | Hedging reads as weaker evidence to Jev | S |

## Discovery

**Built 2026-10-01** (`sources/`, `discovery.py`): browser-free discovery from public job-board
APIs for Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee and Workday (Workday by careers
URL only). Boards are per-user rows in SQLite, added by careers URL, by company name (verified
by fixed rules, otherwise returned as candidates for the user to confirm), or imported from a
JSONL file. `find_jobs` reads all boards concurrently with a 6-hour cache, filters by date,
title keywords (default: the profile's target roles), remote and location, marks postings
already in history, and never calls Jev.

Boards are per-user rows in SQLite, added by careers URL, by company name (verified
by fixed rules, otherwise returned as candidates for the user to confirm), or imported
from a JSONL file. `find_jobs` reads all boards concurrently with a 6-hour cache, filters
by date, title keywords (default: the profile's target roles), remote and location, marks
postings already in history, and never calls Jev.

LinkedIn has no public job-search API and scraping it breaks its terms, so LinkedIn remains
something a user's own browser agent does; job boards are the default path.

| # | Item | Why | Size |
|---|---|---|---|
| 17 | **Cross-source duplicates.** *Done 2026-10-01 in `daily_run`: company + normalized title from another source within 45 days; reported under `duplicates` with the board link, not evaluated, not merged.* The same opening found on LinkedIn and on its job board has different IDs and slightly different text, so exact-match dedupe misses it. Add a "possible duplicate" match on company + normalized title within a few weeks, surfaced as a warning rather than a silent merge. | Duplicate evaluations cost money and resurface jobs. | S |
| 18 | **Board coverage.** *`add_boards_from_history` exists: it probes public boards for unique companies in application history, most recent first, and stores only verified ones.* A user's boards are only as good as the companies they list. Seed sources: companies in imported application history, user-pasted careers URLs, and possibly a community-maintained public registry of company → board mappings shipped as data (public information, open to pull requests). | Boards must come from the user's own targets; discovery alone is not coverage. | M |
| 19 | **Evaluate by reference.** *Largely covered by `daily_run`, which evaluates server-side; descriptions never pass through the agent.* `find_jobs` returns full descriptions (5–15 KB each), which the agent then sends back to `evaluate_jobs`. An `evaluate_found_jobs(ids)` tool that reads cached postings server-side would save context, and folds into the daily run (#12). | Context limits in smaller agents. | S |
| 20 | **Workday at scale.** Large employers hit the 500-posting listing cap within days; use Workday's server-side keyword search. Workday discovery by company name is not implemented. | Big companies are disproportionately on Workday. | M |
| 21 | **Not covered:** Workable (rate-limits), EU-hosted Lever/Greenhouse, custom career domains. | — | M |
| 22 | **Tighter title matching.** Keywords match when all their words appear in any order, so "Regional Manager, Sales Engineering" matches "Engineering Manager". With consent recorded, each false match costs a `daily_run` evaluation. Weigh word order or adjacency, or add a cheap function-word exclusion list, before evaluating. | Substring title matching is deliberately loose | S |

## Later

- Results page or digest (HTML or artifact) instead of chat-only output.
- Notifications for scheduled runs.
- Encryption at rest for the SQLite file.
- Multiple profiles (e.g. two target tracks) in one install.
- Out of scope unless asked: auto-applying, cover letters, contacting recruiters.

## Suggested order

1. Blockers 1–3 (de-personalize) and 6 (repo hygiene): the minimum to show anyone.
2. 8–10 and 17 (exclusions, company warning, liveness, cross-source duplicates): the
   morning-session failures.
3. 4, 5, 7 (consent, packaging, MCP prompts): make install and first run smooth.
4. 12 and 19 (daily run, evaluate by reference), building on discovery; then 18 (coverage).
5. 2 (role-agnostic) before announcing beyond engineering managers.
6. 11, 13–16 as usage reveals priorities.
