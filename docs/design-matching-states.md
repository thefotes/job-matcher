# Matching states: facts, views, controller

job-matcher matches a person with roles; it is not an application tracker. It follows a
model-view-controller split:

- **Model (facts):** postings from job boards and LinkedIn, the user's profile and interview
  evidence, Jev scores (a fact: "Jev judged this posting under this profile, with this model,
  at this time"), when a job was shown, the user's decisions, and when a posting closed.
- **Controller:** the agent, through MCP tools. `daily_run` is a ready-made controller action.
- **View:** recommendations, the New and Still open lists, the report table. Always derived
  from facts plus the user's settings; never stored as truth.

**Rule: showing a job records a fact; it never hides the job. Only the user's own decisions
(applied, dismissed) and the posting closing take a job out of the views. Every view reports
what it excluded and why.**

## States

| State | Set by | Meaning |
|---|---|---|
| `new` | derived: never shown | Not yet shown to the user |
| `shown` | derived from the show log | Shown at least once; no decision. Implicitly "saved". |
| `applied` | the user, or a tracker import | Final for matching |
| `dismissed` | the user (optional reason) | Final for matching |
| `closed` | the world: the posting is gone | Final for matching |

Tracker stages (`interview`, `in_progress`, offers) are outside matching; imported ones count as
`applied`, and the observed `tracker_stage` stays in the job data. Ignoring a job is not a
decision.

## Storage contract

`jobs` gains columns (migrated in place; the old `status` column stays as a cache kept in sync):

| Column | Values |
|---|---|
| `decision` | NULL, `applied`, `dismissed` |
| `decision_origin` | NULL, `user`, `import` |
| `decision_reason` | NULL or free text (dismiss reasons feed later calibration) |
| `decided_at` | ISO timestamp or NULL |
| `closed_at` | ISO timestamp or NULL |

New table `shown(id INTEGER PRIMARY KEY, job_id TEXT REFERENCES jobs(id), shown_at TEXT)`, one
row per time a job was shown.

Migration from the old `status` (idempotent, runs once): `surfaced`/`saved` → a `shown` row at
`last_seen`; `applied`/`interview`/`in_progress` → decision `applied` (origin `import` if
`status_origin` was `import`, else `user`), `decided_at` = `last_seen`; `dismissed` → decision
`dismissed` (`user`); `closed` → `closed_at` = `last_seen`; `new` → nothing.

## Row contract

Every row from `Store.history` / `Service.history` (full detail) carries, besides today's keys:

| Key | Meaning |
|---|---|
| `status` | derived: `decision` if set (an application stays `applied` after the posting closes), else `closed` if `closed_at`, else `shown` if shown at least once, else `new` |
| `times_shown`, `first_shown_at`, `last_shown_at` | from the show log (0 / NULL when never shown) |
| `decision`, `decision_origin`, `decision_reason`, `decided_at`, `closed_at` | as stored |
| `state_since` | when the job entered its current state: `decided_at`, `closed_at`, `first_shown_at` or `first_seen` |

## Writes

`set_job_status` (single `job_id` or `job_ids`) accepts `new`, `shown`, `applied`, `dismissed`,
`closed`, plus legacy aliases `surfaced`/`saved` → `shown` and `interview`/`in_progress` →
`applied`. An optional `reason` is stored with `dismissed`.

- `shown` appends a show-log row; it never changes a decision.
- `applied` / `dismissed` set the decision (origin `user`) and `decided_at`.
- `closed` sets `closed_at`. The system may also set and clear `closed_at` from job-board
  liveness (a posting that reappears is reopened).
- `new` resets the job: clears decision and `closed_at` and deletes its show-log rows.

Imports never overwrite a user decision.

## Views

Pure functions in `views.py` over rows (already re-scored under the current strictness):

- Candidates: not historical, status `new` or `shown`, evaluation current for this profile and
  job text, recommendation not `skip`.
- **New:** status `new`; fit descending, then most recently posted/found.
- **Still open:** status `shown` with `first_shown_at` inside `policy.open_window_days`
  (default 30); fit descending, then most recently shown. `include_stale=true` drops the window.
- **Excluded counts** by reason: `decided`, `closed`, `historical`, `stale`, `skip`,
  `not_evaluated` (no evaluation, or one for an older profile or job text).

`open_window_days` lives in the profile policy (local, never sent to Jev). Defaults are the
starting point; a decay function can replace the sliding window later without changing facts.
