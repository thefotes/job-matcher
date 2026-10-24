<p align="center">
  <img src="docs/hero.png" alt="Job Matcher (MCP): your resume plus your preferences gives better job matches" width="100%">
</p>

# Job Matcher

A local MCP server (and JSON CLI) that gives your AI agent (Claude Code, Claude Desktop,
Cursor, Codex) a memory and a judgment for your job search:

- **Profile once.** Your resume, target roles, location, salary and dealbreakers, plus an
  agent-led interview about experience your resume leaves out. Stored locally in SQLite.
- **Find jobs** on public job boards (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee,
  Workday) for the companies you add. No browser, no LinkedIn scraping.
- **Evaluate and rank** postings against your profile with TypeSafe's Jev model, under a
  strictness dial you choose, with blockers and concerns spelled out.
- **Remember** what you have seen, saved, applied to or dismissed, so nothing resurfaces.
- **Daily run**: one tool call that finds new postings, evaluates them and returns a
  ranked report.

Browsing, scheduling and applying stay with you and your agent.

**You need** Python 3.12+, [uv](https://docs.astral.sh/uv/) and a TypeSafe API key.
Evaluation is billed to your key (roughly 5,000 input tokens per job) and is the only
step that sends anything off your machine; see [Consent](#consent-what-leaves-your-machine).

**Note:** the interview topics and qualification weights currently assume engineering
roles (people-management experience is weighted heavily) and are not yet role-aware;
see `docs/open-source-readiness.md`.

Pairs well with [connections-finder](https://github.com/thefotes/connections-finder),
whose `data/02-boards.jsonl` can be imported as job boards (`import_job_boards`).

## Get started

```sh
git clone https://github.com/thefotes/job-matcher.git
cd job-matcher
uv sync --no-editable --extra dev
cp .env.example .env
```

Set `TYPESAFE_API_KEY` in `.env`. An existing environment variable takes precedence.
The key is never stored in SQLite. No live TypeSafe call is made during onboarding.

Onboard interactively with a UTF-8 text version of your resume:

```sh
uv run --no-sync job-matcher init --resume /absolute/path/to/resume.txt
```

Or onboard through JSON (also available as MCP tools):

```sh
uv run --no-sync job-matcher onboarding
uv run --no-sync job-matcher profile-save --input /absolute/path/to/profile.json
```

Start with `examples/profile.json` (placeholder values only — replace everything),
put the resume in `resume_text`, and set your own preferences. The profile ships
empty: there are no built-in roles, location, salary or authorization assumptions.
No experience is invented. A profile can be saved as a draft, but evaluation refuses
until the required fields are present: `resume_text`, `target_roles`, and either a
`location` or `remote_only=true`. The error lists exactly what is missing; the same
gate applies to `daily_run` (nothing is evaluated until onboarding is complete).
PDF/DOCX extraction is delegated to the calling agent; this evaluator accepts plain
text.

The interactive wizard reviews name, location, salary, roles, preferences and
dealbreakers. JSON/MCP onboarding exposes all fields, including remote requirements,
authorization, currency and decision thresholds. Saving replaces the complete profile;
read it first when making an update.

## Consent: what leaves your machine

Evaluation is the only step that sends personal data anywhere. It sends your saved profile
(name, resume, confirmed interview answers, target roles, salary floor and target, location,
remote and authorization requirements, preferences, dealbreakers) and the job posting to
TypeSafe, billed to your own key (roughly 5,000 input tokens per job in testing). Strictness,
job history, statuses and job boards never leave your computer; job-board discovery calls only
public job-board APIs and sends nothing personal.

Evaluation refuses until you have consented. The `init` wizard asks after saving the profile;
otherwise run `job-matcher consent grant` (shows the disclosure and asks), or an agent shows
`get_onboarding`'s `consent.disclosure` and, only if you agree, calls `record_consent`.
`job-matcher consent show` and `consent revoke` (MCP: `revoke_consent`) inspect or withdraw it;
revoking stops evaluation and keeps everything stored locally. Consent is stored with a
timestamp and disclosure version; if what is sent ever changes, you are asked again.
Evaluation results include the consent record they relied on, and the evaluate tools say
consent is required, which gives client-side safety reviewers (for example Codex auto-review)
the authorization evidence they look for.

## Interview beyond the resume

Once the MCP is connected, give your agent your resume and use the
[onboarding interview prompt](docs/onboarding-agent-prompt.md). It reads the saved
profile, reviews your constraints, then asks focused questions about experience your
resume may omit. Existing defaults are examples for engineering leadership; new users
should review every preference rather than accept them silently.

`get_onboarding` returns a resumable interview plan with nine topics: engineering and
management tenure, leadership scope, hands-on work, technical scope, developer tooling,
AI-assisted development, production AI products, and domain experience. Your agent
records explicit statements or summaries you confirm through `save_experience_answers`.
Answers are stored separately from the resume, with `user_reported` provenance, and
can be `reported`, `no_experience`, or `skipped`. A missing/skipped answer never means
you lack that experience. Confirmation is the caller's attestation, not independent
verification by the server. No model infers interview facts during onboarding.

The tool merges by topic and preserves your resume/preferences; a new answer replaces
only that topic. Review prior detail when refining an answer. Resume readiness (`ready`)
and interview completion (`interview.complete`) are separate. Completion means every
topic was addressed or explicitly skipped; you can evaluate before completion.

For JSON CLI users, run `onboarding` for the plan, then save an array of confirmed
answers with `experience-save --input answers.json`. The basic `init` wizard does not
conduct the agent interview. Evaluation sends the resume **and interview evidence**
to TypeSafe; changing either invalidates cached rankings while preserving job statuses.
More evidence does not remove the current confidence gates or guarantee an apply result.

## Evaluate and rank

`evaluate` accepts one job object. `batch` accepts an array of 1–100 jobs:

```sh
uv run --no-sync job-matcher batch --input examples/jobs.json
uv run --no-sync job-matcher evaluate < /absolute/path/to/job.json
uv run --no-sync job-matcher shortlist
uv run --no-sync job-matcher history --limit 100
uv run --no-sync job-matcher status JOB_ID shown
uv run --no-sync job-matcher status JOB_ID applied
uv run --no-sync job-matcher status JOB_ID dismissed --reason "Office only"
```

A job requires `title`, `company`, and `description`. Optional fields: `url`, `source`,
`source_id` (requires `source`), and `posted_at`. The tool does not fetch URLs.
Job-board fields from `find_jobs` are also accepted: `apply_url`, `location`, `remote`,
`compensation` and `updated_at`. They are sent to TypeSafe as evidence with the description.
All command results and errors are JSON on stdout; interactive prompts use stderr.
A batch continues after individual failures and returns errors with their input indices;
its exit status is nonzero if any item failed. `--force` bypasses cached evaluations.
`--data-dir /absolute/private/path` goes before the CLI subcommand.

Each evaluation includes:

- `recommendation`: `apply`, `investigate`, or `skip`.
- `fit_percent` and `qualification_strength_percent`: ranking scores, **not a probability
  of getting an interview or offer**. Hiring probabilities need outcome data and validation.
- `stretch`, confirmed `dealbreakers`, and material uncertainties in `investigate`.
- Per-dimension signals, model confidence, raw distributions, model name, token usage,
  profile hash, and scoring version.
- `job_id`, status, duplicate indicator, and cache indicator.

Jev answers ten independent questions: role, remote arrangement, geographic eligibility,
annual base salary, authorization, dealbreakers, leadership experience, technical/domain
experience, seniority, and preferences. Descriptions are evidence, never instructions.
See [TypeSafe's API](https://docs.typesafe.ai/api) and
[atomic decision guidance](https://docs.typesafe.ai/concepts/how-to-build-with-system-one).

Qualification strength weights leadership 40%, technical/domain 35%, and seniority 25%.
Fit weights qualification strength 80%, salary satisfaction 15%, and preferences 5%.
Scores use five ordered levels (0–4), normalized to 0–100. Salary preference values:
below floor 0, overlapping floor 50, meets floor 75, meets target 100, unknown 50.
The unknown value is neutral for ranking; it does not prove salary compatibility.

### Strictness: how much benefit of the doubt

Jev measures; strictness decides. Each user picks one of five stops (stored in their profile,
asked during the interview, changed any time with `set_strictness` or `job-matcher strictness`):

| Stop | Qualification bar (stretch / strong) | A doubtful job is… | Details the posting omits |
|---|---|---|---|
| `wide_net` | 40 / 60 | applied, with notes | notes |
| `generous` | 45 / 65 | applied, with notes | notes |
| `balanced` (default) | 50 / 70 | investigated | notes |
| `selective` | 60 / 72 | investigated | count as doubt |
| `strict` | 75 / 75, no stretches | hidden | count as doubt |

A *doubt* is a material chance of a hard conflict, a posting silent on remote work for a
remote-only candidate, an unresolved dealbreaker, or a qualification rating Jev has not settled
within one adjacent level. Stricter stops also treat smaller conflict probabilities as doubt
(50% at `wide_net` down to 10% at `strict`) and need less certainty before a likely conflict
counts as confirmed. Every knob moves the same way, so a stricter stop never upgrades a
recommendation (this is property-tested). **Confirmed conflicts are skipped at every stop.**
Details a posting omits (salary, eligible states, authorization) and a salary range spanning
your floor are returned as `checks` to resolve with the recruiter. Individual knobs can be
overridden (`set_strictness` with `overrides`, e.g. `{"apply_threshold": 80}`).

Strictness is never sent to Jev. Jev sees the resume, interview evidence and preferences
(`Profile.evidence()`); the dial is applied afterward to Jev's saved answers. Stored
evaluations are re-scored whenever they are read, so changing the dial, or the scoring rules,
takes effect immediately with no new API calls. The reported confidence is the minimum of the
dimension confidences, not a calibrated confidence in the combined recommendation. Score
formula and stop values are initial heuristics; refine them with real jobs and outcomes.

## History and deduplication

SQLite stores the profile, job identities, status and evaluation history. Default storage
is `.job-matcher/` in the checkout (or the working directory for an installed package).
Set `JOB_MATCHER_DATA_DIR` to an absolute path for stable MCP storage.

Identity uses source ID, canonical URL (including LinkedIn job ID), and normalized exact
company/title/description content. Tracking parameters are removed. A changed job under the
same URL or source ID is reevaluated. Distinct job IDs with identical text can collapse into
one entry; vague semantic similarities are deliberately not used to merge separate openings.
Conflicting existing identities require review instead of silently merging records.

Cache keys include substantive job text, full profile, requested model and scoring version.
URL tracking changes do not incur another API call. Changing profile preferences invalidates
the cache. `jev-latest` can change upstream; use `--force` or pin `TYPESAFE_DEFAULT_MODEL` when
freshness/reproducibility matters. Calls are sequential in v1, with SDK retry behavior.

Each job is in one state, derived from stored facts (see
[the matching-states design](docs/design-matching-states.md)):

| State | Meaning |
|---|---|
| `new` | Never shown to you |
| `shown` | Shown at least once, no decision yet (every showing is logged) |
| `applied` | Your decision, or imported from your application tracker |
| `dismissed` | Your decision, with an optional reason |
| `closed` | The posting is gone (an application stays `applied` after it closes) |

`set_job_status` (or `job-matcher status`) records them: `shown` logs a showing and never
changes a decision; `applied` and `dismissed` set your decision; `closed` marks the posting
gone; `new` resets the job (decision, closure and showings cleared). The older names still
work: `surfaced`/`saved` mean `shown`, `interview`/`in_progress` mean `applied`. Databases
from earlier versions are migrated in place when first opened. History rows report the
state, when it began (`state_since`), and how often and when a job was shown.

Showing a job records a fact; it never hides the job. `get_shortlist` (CLI `shortlist`)
returns two views under the current profile and strictness, each sorted by fit: **New**
(never shown) and **Still open** (shown before, no decision, first shown within the
profile's `open_window_days`, default 30; `include_stale=true` drops the window). Applied,
dismissed and closed jobs never appear, and `excluded` counts everything left out and why.
Change the window with `set_strictness` overrides, e.g. `{"open_window_days": 14}`; like
strictness it is local and never sent to Jev. `batch.shortlist` ranks only that batch.
Historical imports never appear in the views. Profile changes preserve job states.

Import observed application history without a resume or TypeSafe calls:

```sh
uv run --no-sync job-matcher history-import --input /absolute/path/to/tracker.json
```

The input is an array of 1–100 entries, each with `title`, `company`, `tracker_stage`
(`applied`, `interview`, or `in_progress`), and a URL or `source`/`source_id`.
Description is optional. Repeated imports deduplicate and preserve full descriptions.
Every stage records the job as `applied` (the latest `tracker_stage` stays in the job data);
an import never overwrites your own decision.
Each failed entry returns its input index; successful entries are still saved.
A clicked Apply button maps to `in_progress`, never proof of submission.

See [the recurring agent prompt](docs/recurring-agent-prompt.md) for the daily routine,
including the optional LinkedIn tracker import.

## Find jobs on public job boards

Discovery reads the public job-board APIs that applicant tracking systems (ATS) publish for
their customers' careers pages, so no browser or login is needed. You save the boards of
companies you care about once, then `find_jobs` returns fresh postings with full plain-text
descriptions, ready for `evaluate_jobs`. Finding never evaluates: TypeSafe is called only
when you (or your agent) explicitly evaluate the results.

| Provider | Board URL | Notes |
| --- | --- | --- |
| Greenhouse | `job-boards.greenhouse.io/<token>` | full descriptions in one request |
| Ashby | `jobs.ashbyhq.com/<token>` | remote flag and compensation summary |
| Lever | `jobs.lever.co/<token>` | workplace type and salary range |
| SmartRecruiters | `careers.smartrecruiters.com/<token>` | one extra request per matching posting |
| Recruitee | `<token>.recruitee.com` | remote/hybrid flags and salary |
| Workday | `<tenant>.wdN.myworkdayjobs.com/<site>` | add by URL only; paged and capped |

Add boards by careers URL (any careers page, job page or API URL on those hosts), by
company name, or by importing a file:

```sh
uv run --no-sync job-matcher board-add --url https://job-boards.greenhouse.io/example
uv run --no-sync job-matcher board-add --company "Example Co"
uv run --no-sync job-matcher board-add --company "Example Co" \
  --url https://example.wd5.myworkdayjobs.com/en-US/Careers
uv run --no-sync job-matcher boards-import --input /absolute/path/to/boards.jsonl
uv run --no-sync job-matcher boards
uv run --no-sync job-matcher board-remove 3            # or --company NAME, --deactivate
```

A company name is turned into likely board tokens and probed on Greenhouse, Ashby and
Lever, then SmartRecruiters and Recruitee. A response is not proof of ownership (a guessed
`capital` board is not Capital One), so a guess is saved only if the token is the exact
normalized company name on a trusted provider, or a trusted provider names that company.
Otherwise the tool returns candidates with board names and sample titles, and the agent asks
you to confirm one by its careers URL. A careers URL you supply is trusted as your choice;
a board whose own name differs returns a warning. Workday tenants and custom career domains
are never guessed. No language model is involved in verification.

SmartRecruiters and Recruitee always require confirmation by careers URL, even when a
board's displayed company name matches the requested employer. On upgrade, boards previously
verified only by a low-trust display-name match become inactive until re-added by careers
URL. Existing URL confirmations and explicit imports retain their recorded verification.

Discovery accepts at most 64 MiB per HTTP response, allowing headroom for large boards
that include full job descriptions. Responses exceeding a provider's requested page size
are rejected to keep pagination accurate. The 150-second discovery deadline cancels
queued work; running workers check cancellation before retries, subsequent requests and
cache writes. Cancellation is cooperative, so in-flight I/O may finish before the worker
observes it. Cached employer links pass the same HTTP(S)/hostname validation
as supplied jobs, and dynamic report text and link destinations are escaped for Markdown.

Imports accept JSON Lines, one board per line, with `company`, `provider` (or `ats`), `token`
(or `board_token`) and, for Workday, `shard`/`site` (or `workday_shard`/`workday_site`).
Rows with `"verification": {"match": false}` are skipped; other rows keep their prior
verification method. Imports make no network requests.

`boards-from-history --limit N` (MCP `add_boards_from_history`) adds boards for companies in
your application history, most recent first, skipping companies that already have a saved
board. It calls only public job-board APIs and sends nothing personal; only verified boards
are stored, and unverified candidates and unknown companies are reported back for you.

Boards that answer HTTP 404 or 410 on every fetch are retired automatically: three
consecutive misses deactivate a board (it stays listed, but `find_jobs` skips it), any
successful fetch resets the counter, and other errors such as timeouts or HTTP 500 never
count. The boards deactivated in a run are listed in daily-run coverage as
`deactivated_boards`; re-add a board with `board-add` to revive it.

```sh
uv run --no-sync job-matcher find --since-days 7 --keyword "Engineering Manager" \
  --keyword "Director of Engineering" --remote-only --location "United States"
```

`find_jobs` (CLI `find`) reads all active boards concurrently (six boards and eight requests
at a time, 20-second timeouts, one retry on rate limits or server errors) and keeps:

- postings published (or, without a publish date, updated) within `since_days`;
  undated postings are kept and listed last;
- titles containing every word of any keyword, in any order (`Engineering Manager` matches
  `Manager, Software Engineering` and `Sr. Eng Manager`); keywords default to the
  profile's `target_roles`, or no title filter without a profile;
- with `remote_only` (default: the profile's setting), postings the ATS or location text
  marks remote, plus postings with no location at all; on-site, hybrid and city-only
  postings are dropped;
- with `location_keywords`, postings whose location names any keyword (or has none).

Results are newest first, up to `limit` (default 25, at most 100), each with its `job`, the
`board_id`, and `known`: the history entry (`job_id`, `status`, `historical`, `evaluated`)
when the posting matches a saved job by the same source ID, URL or exact-content identity
used for deduplication. `exclude_known` drops those. Board failures are listed in `errors`
and do not stop other boards; postings without a usable description are counted, not
returned. Listings are cached in SQLite for 6 hours (`refresh` bypasses this) and per-posting
details for 72 hours. SmartRecruiters and Workday descriptions are fetched only for
postings that pass the title and date filters, at most 40 per board per call
(`details_deferred` reports the rest; the next call continues). Workday listings stop paging
once a whole page is older than `since_days`, and never exceed 500 postings per board.

Privacy: discovery calls only public, unauthenticated ATS endpoints with a generic
`job-matcher` user agent. Requests contain board tokens and posting IDs, never your resume,
profile, keywords or history; all filtering happens locally. Boards and cached postings live
in your local SQLite database alongside your history.

## Daily run

`daily_run` (CLI `daily`) is the whole daily routine in one call, so a scheduled agent does
not have to improvise it:

```sh
uv run --no-sync job-matcher daily --since-days 7 --max-evaluations 15 --top 10
```

1. Runs `find_jobs` on your saved boards with your profile's roles and remote setting.
2. Skips postings already in history with any status other than `new`. A posting from
   another source with the same company and normalized title as a history record within 45
   days is a likely duplicate: it is not evaluated, and is listed under `duplicates` with
   its board link as `employer_url`. The records are not merged, but the link is kept on
   the record, so later runs count that posting as known instead of listing it again.
3. Evaluates new postings, newest first, up to `max_new_evaluations`. Without recorded
   consent nothing is evaluated: the postings come back as `unevaluated`, with the consent
   disclosure and what to do next.
4. Checks each board job against the listing it just read. A posting no longer listed is
   recorded as closed (`gone`); one that reappears is reopened (`reopened`). Your
   decisions are never changed.
5. Returns two views as compact summaries under your current strictness: up to `top_n`
   **New** matches (never shown, whenever found) and up to `open_n` **Still open** ones
   (shown before, no decision yet). When nothing new qualified it says so instead of
   padding the list. A job seen on LinkedIn and on a board carries both links.
6. Returns `report_markdown` (both views as tables with the posted salary range, and
   LinkedIn and employer links) and
   coverage: boards read and failed, discovered, skipped, duplicates, evaluated, errors,
   deferred, input tokens, strictness and `excluded` counts.

It never records showings itself: the agent marks `shown_ids` as `shown` and then puts
`report_markdown` in its final reply (see `next_step`).
[The recurring agent prompt](docs/recurring-agent-prompt.md) is built around it.

## MCP

Nineteen tools. Profile: `get_onboarding`, `save_profile`, `save_experience_answers`,
`set_strictness`, `record_consent`, `revoke_consent`. Evaluation: `evaluate_job`,
`evaluate_jobs`, `get_shortlist`, `get_history`, `set_job_status`,
`import_application_history`. Discovery: `add_job_board`, `add_boards_from_history`,
`list_job_boards`, `remove_job_board`, `import_job_boards`, `find_jobs`. Routine:
`daily_run`. Tool input
schemas and read/write/external annotations are included. Evaluation sends the resume,
user-reported interview evidence and job text to TypeSafe (never the strictness policy).
Discovery calls only public job-board APIs (see above). `get_history`, `get_shortlist` and
`get_onboarding` are compact by default; pass `detail=true` or
`include_resume=true`/`include_schema=true` when you need the full records (for example
`include_resume=true` before a full `save_profile` so the resume is preserved).

### Registering the MCP server

Replace `/path/to/job-matcher` with the absolute path of your checkout everywhere below.
Your key stays in `.env`; registrations store only its file path.

**Claude Code:**

```sh
claude mcp add job-matcher \
  --env "JOB_MATCHER_ENV_FILE=/path/to/job-matcher/.env" \
  -- /path/to/job-matcher/.venv/bin/job-matcher-mcp --data-dir /path/to/job-matcher/.job-matcher
```

**Claude Desktop, Cursor, and other clients that accept `mcpServers` JSON** — add to
the client's MCP configuration (Claude Desktop: Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "job-matcher": {
      "command": "/path/to/job-matcher/.venv/bin/job-matcher-mcp",
      "args": ["--data-dir", "/path/to/job-matcher/.job-matcher"],
      "env": {
        "JOB_MATCHER_ENV_FILE": "/path/to/job-matcher/.env"
      }
    }
  }
}
```

**Codex** (CLI and desktop app share `~/.codex/config.toml`):

```sh
codex mcp add job-matcher \
  --env "JOB_MATCHER_ENV_FILE=/path/to/job-matcher/.env" \
  -- /path/to/job-matcher/.venv/bin/job-matcher-mcp --data-dir /path/to/job-matcher/.job-matcher
```

In the Codex desktop app, open Settings → MCP servers and restart the server to load
the new configuration; use `/mcp` in a new chat to check its connection. No HTTP
server or tunnel is needed for a local stdio connection. See the
[official MCP setup guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

For local HTTP:

```sh
uv run --no-sync job-matcher-mcp --transport streamable-http --port 8765
```

Endpoint: `http://127.0.0.1:8765/mcp`. It binds to loopback only and has no public OAuth
layer. Use stdio locally or a private Secure MCP Tunnel for ChatGPT. Do not publish this
personal server through an unauthenticated public tunnel: it exposes profile/history and
profile/status mutations. A public deployment needs authentication first.

Hosted ChatGPT connection is a separate setup step. Current official documentation describes
Developer mode and either a public HTTPS endpoint or Secure MCP Tunnel; it does not imply
that a Claude-style local `mcpServers` configuration can be pasted into ChatGPT.
Account/workspace access must be checked when connecting:
[Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt) and
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
No tunnel, deployment, account connection, or scheduled task has been created.

## Data and development

After changing source code, reinstall with `uv sync --no-editable --extra dev
--reinstall-package job-matcher` so CLI/MCP subprocesses use the new code.

The `.env`, local database and virtual environment are ignored by Git. SQLite is not
encrypted; keep its directory private. The default data directory is created with owner-only
permissions and the database file is set to 0600. MCP clients can read the saved profile
and history. Avoid SDK debug logging because SDK request logs may contain resume/job text.
This is a single-user tool; it has no multi-tenant isolation.

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
```

Discovery tests cover every provider adapter, URL inference, board verification, imports,
caching, filters and history marking with a stubbed HTTP transport; pytest makes no network
calls. Tests exercise recommendation rules, missing evidence, deduplication, cache invalidation,
status persistence, retry after provider failures, the actual TypeSafe SDK through a mock
HTTP transport, CLI JSON output, and both MCP transports. Tests make no paid API calls.
Live integration checks with the saved resume and TypeSafe key have also been run.
