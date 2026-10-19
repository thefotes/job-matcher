import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


# Referral/tracking query parameters that never identify a posting. gh_src and
# lever-source/-origin are added by job boards to links shared from LinkedIn and elsewhere.
TRACKING_PARAMS = {
    "trk",
    "trackingid",
    "refid",
    "ref",
    "source",
    "src",
    "gh_src",
    "lever-source",
    "lever-origin",
}


def identities(job):
    keys = []
    if job.source_id:
        if job.source.casefold() == "linkedin" and job.source_id.isdigit():
            keys.append("linkedin:" + job.source_id)
        keys.append("source:" + digest([job.source.casefold(), job.source_id]))
    if job.url:
        url = urlsplit(job.url)
        linkedin = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", url.path)
        if url.hostname in {"linkedin.com", "www.linkedin.com"} and linkedin:
            keys.append("linkedin:" + linkedin[1])
        else:
            query = [
                (k, v)
                for k, v in parse_qsl(url.query)
                if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
            ]
            canonical = urlunsplit(
                (
                    url.scheme.lower(),
                    url.netloc.lower(),
                    url.path.rstrip("/"),
                    urlencode(sorted(query)),
                    "",
                )
            )
            keys.append("url:" + digest(canonical))
    # Exact normalized content catches cross-source copies without merging distinct roles
    # simply because they share a company and title.
    if job.description:
        keys.append(
            "content:"
            + digest(
                [
                    " ".join(value.casefold().split())
                    for value in (job.company, job.title, job.description)
                ]
            )
        )
    return keys


# A job's state (docs/design-matching-states.md), derived from facts in this order: the
# user's (or a tracker's) decision, the posting closed, shown at least once, else new.
# jobs.status caches it for raw readers and is refreshed on every write.
DERIVED_STATUS = """CASE WHEN decision IS NOT NULL THEN decision
    WHEN closed_at IS NOT NULL THEN 'closed'
    WHEN EXISTS(SELECT 1 FROM shown s WHERE s.job_id=jobs.id) THEN 'shown'
    ELSE 'new' END"""
SCHEMA_VERSION = 1  # 1: decisions, closed_at and the show log replace stored statuses


def derived_status(row):
    """DERIVED_STATUS for a history row, with its time in that state. The user's decision
    wins over closure: an application stays "applied" after the posting comes down."""
    if row["decision"]:
        return row["decision"], row["decided_at"]
    if row["closed_at"]:
        return "closed", row["closed_at"]
    if row["times_shown"]:
        return "shown", row["first_shown_at"]
    return "new", row["first_seen"]


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "history.sqlite3"
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identities (
                    key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id));
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id), cache_key TEXT NOT NULL,
                    result TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evaluation_cache
                    ON evaluations(job_id, cache_key, id DESC);
                CREATE TABLE IF NOT EXISTS consents (
                    provider TEXT PRIMARY KEY, version TEXT NOT NULL,
                    disclosure TEXT NOT NULL, granted_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shown (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    shown_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS shown_job ON shown(job_id, shown_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, definition in {
                "historical": "INTEGER NOT NULL DEFAULT 0",
                # Legacy: who set the old status. Read only by the status migration.
                "status_origin": "TEXT NOT NULL DEFAULT 'user'",
                "decision": "TEXT",  # NULL, applied, dismissed
                "decision_origin": "TEXT",  # NULL, user, import
                "decision_reason": "TEXT",
                "decided_at": "TEXT",
                "closed_at": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            self.create_board_tables(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(boards)")}
            if "not_found_count" not in columns:
                db.execute(
                    "ALTER TABLE boards ADD COLUMN not_found_count INTEGER NOT NULL DEFAULT 0"
                )
            self.migrate(db)
        self.path.chmod(0o600)

    @staticmethod
    def migrate(db):
        """Turn old stored statuses into facts, once; PRAGMA user_version records it."""
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        db.execute(
            "INSERT INTO shown(job_id, shown_at) "
            "SELECT id, last_seen FROM jobs WHERE status IN ('surfaced', 'saved')"
        )
        db.execute(
            "UPDATE jobs SET decision='applied', decided_at=last_seen, decision_origin="
            "CASE status_origin WHEN 'import' THEN 'import' ELSE 'user' END "
            "WHERE status IN ('applied', 'interview', 'in_progress')"
        )
        db.execute(
            "UPDATE jobs SET decision='dismissed', decision_origin='user', "
            "decided_at=last_seen WHERE status='dismissed'"
        )
        db.execute("UPDATE jobs SET closed_at=last_seen WHERE status='closed'")
        db.execute(f"UPDATE jobs SET status={DERIVED_STATUS}")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def sync_status(db, job_id):
        """Refresh the cached status of one job and return it."""
        db.execute(f"UPDATE jobs SET status={DERIVED_STATUS} WHERE id=?", (job_id,))
        return db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def profile(self):
        with closing(self.connect()) as db:
            row = db.execute("SELECT data FROM profiles WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def save_profile(self, profile):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO profiles VALUES(1, ?) ON CONFLICT(id) DO UPDATE "
                "SET data=excluded.data",
                (profile.model_dump_json(),),
            )

    def consent(self, provider):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM consents WHERE provider=?", (provider,)).fetchone()
            return dict(row) if row else None

    def save_consent(self, provider, version, disclosure):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO consents VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "version=excluded.version, disclosure=excluded.disclosure, "
                "granted_at=excluded.granted_at",
                (provider, version, disclosure, now()),
            )
        return self.consent(provider)

    def revoke_consent(self, provider):
        with closing(self.connect()) as db, db:
            return bool(db.execute("DELETE FROM consents WHERE provider=?", (provider,)).rowcount)

    def ingest(self, job, historical=False):
        keys = identities(job)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            matches = db.execute(
                "SELECT DISTINCT job_id FROM identities WHERE key IN "
                f"({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
            if len(matches) > 1:
                raise ValueError("Conflicting job identities; review source_id and URL")
            job_id = matches[0][0] if matches else digest(keys[0])[:24]
            existing = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            timestamp = now()
            data = job.model_dump()
            if existing:
                old = json.loads(existing["data"])
                if historical:
                    data = {**old, **{k: v for k, v in data.items() if v is not None}}
                elif "tracker_stage" in old:
                    data["tracker_stage"] = old["tracker_stage"]
            db.execute(
                "INSERT INTO jobs(id,data,first_seen,last_seen,status_origin) "
                "VALUES(?,?,?,?,'auto') "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                "last_seen=excluded.last_seen",
                (job_id, json.dumps(data), timestamp, timestamp),
            )
            for key in keys:
                db.execute("INSERT OR IGNORE INTO identities VALUES(?,?)", (key, job_id))
            if historical:
                # Every tracker stage counts as applied. A decision already made (by the
                # user, or an earlier import) stands; the latest stage stays in data.
                db.execute(
                    "UPDATE jobs SET historical=1, decision=COALESCE(decision, 'applied'), "
                    "decision_origin=CASE WHEN decision IS NULL THEN 'import' "
                    "ELSE decision_origin END, decided_at=COALESCE(decided_at, ?) WHERE id=?",
                    (timestamp, job_id),
                )
            status = self.sync_status(db, job_id)
            for key in keys:
                if key.startswith("linkedin:") and any(
                    k.startswith("linkedin:") and k != key for k in keys
                ):
                    raise ValueError("Conflicting LinkedIn source_id and URL")
            return job_id, status, bool(existing)

    def fill_job_fields(self, job_id, fields):
        """Set job fields that are currently empty; never overwrite observed values."""
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            missing = {k: v for k, v in fields.items() if v and not data.get(k)}
            if missing:
                db.execute(
                    "UPDATE jobs SET data=? WHERE id=?", (json.dumps(data | missing), job_id)
                )
            return bool(missing)

    def is_historical(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT historical FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row[0])

    def cached(self, job_id, cache_key):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT result FROM evaluations WHERE job_id=? AND cache_key=? "
                "ORDER BY id DESC LIMIT 1",
                (job_id, cache_key),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_evaluation(self, job_id, cache_key, result):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO evaluations(job_id,cache_key,result,created_at) VALUES(?,?,?,?)",
                (job_id, cache_key, json.dumps(result), now()),
            )
            return cursor.lastrowid

    def set_status(self, job_id, status, reason=None):
        """Apply a user's status write (a canonical state; see the design doc): shown logs
        a showing and never changes a decision; applied and dismissed are the user's
        decision, dismissed with an optional reason; closed marks the posting gone; new
        resets the job to never shown. Returns the job's resulting state."""
        with closing(self.connect()) as db, db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise ValueError("Unknown job_id")
            timestamp = now()
            if status == "shown":
                db.execute("INSERT INTO shown(job_id, shown_at) VALUES(?,?)", (job_id, timestamp))
            elif status in {"applied", "dismissed"}:
                # Repeating the same decision keeps when it was first made.
                db.execute(
                    "UPDATE jobs SET decided_at=CASE WHEN decision=? THEN decided_at ELSE ? END, "
                    "decision=?, decision_origin='user', decision_reason=? WHERE id=?",
                    (status, timestamp, status, reason, job_id),
                )
            elif status == "closed":
                db.execute(
                    "UPDATE jobs SET closed_at=COALESCE(closed_at, ?) WHERE id=?",
                    (timestamp, job_id),
                )
            elif status == "new":
                db.execute(
                    "UPDATE jobs SET decision=NULL, decision_origin=NULL, decision_reason=NULL, "
                    "decided_at=NULL, closed_at=NULL WHERE id=?",
                    (job_id,),
                )
                db.execute("DELETE FROM shown WHERE job_id=?", (job_id,))
            else:
                raise ValueError(f"Unknown status: {status}")
            return {"job_id": job_id, "status": self.sync_status(db, job_id)}

    def record_shown(self, job_ids):
        """Log one showing of each known job; returns the IDs recorded. Decisions and
        closure are unchanged."""
        timestamp, recorded = now(), []
        with closing(self.connect()) as db, db:
            for job_id in dict.fromkeys(job_ids):
                if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                    db.execute(
                        "INSERT INTO shown(job_id, shown_at) VALUES(?,?)", (job_id, timestamp)
                    )
                    self.sync_status(db, job_id)
                    recorded.append(job_id)
        return recorded

    def set_closed(self, job_id, closed):
        """Record that a posting closed (keeping the first time seen closed) or reopen it
        when it reappears. Returns the job's resulting state."""
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "UPDATE jobs SET closed_at=CASE WHEN ? THEN COALESCE(closed_at, ?) END WHERE id=?",
                (bool(closed), now(), job_id),
            )
            if not cursor.rowcount:
                raise ValueError("Unknown job_id")
            return self.sync_status(db, job_id)

    def history(self, limit=100):
        with closing(self.connect()) as db:
            rows = db.execute(
                """
                SELECT j.*, e.id evaluation_id, e.result, e.created_at evaluated_at,
                    COALESCE(s.times_shown, 0) times_shown, s.first_shown_at, s.last_shown_at
                FROM jobs j LEFT JOIN evaluations e ON e.id=(
                    SELECT MAX(id) FROM evaluations WHERE job_id=j.id)
                LEFT JOIN (
                    SELECT job_id, COUNT(*) times_shown, MIN(shown_at) first_shown_at,
                        MAX(shown_at) last_shown_at
                    FROM shown GROUP BY job_id) s ON s.job_id=j.id
                ORDER BY j.last_seen DESC LIMIT ?
            """,
                (limit,),
            ).fetchall()
            results = []
            for row in rows:
                status, since = derived_status(row)
                results.append(
                    {
                        "job_id": row["id"],
                        "job": json.loads(row["data"]),
                        "status": status,
                        "historical": bool(row["historical"]),
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "evaluation_id": row["evaluation_id"],
                        "evaluated_at": row["evaluated_at"],
                        "evaluation": json.loads(row["result"]) if row["result"] else None,
                        "times_shown": row["times_shown"],
                        "first_shown_at": row["first_shown_at"],
                        "last_shown_at": row["last_shown_at"],
                        "decision": row["decision"],
                        "decision_origin": row["decision_origin"],
                        "decision_reason": row["decision_reason"],
                        "decided_at": row["decided_at"],
                        "closed_at": row["closed_at"],
                        "state_since": since,
                    }
                )
            return results

    # Job-board discovery: per-user boards plus a fetch cache. All created empty.

    BOARD_COLUMNS = (
        "id, company, provider, token, shard, site, board_url, board_name, verified, "
        "verification_method, added_at, last_fetched_at, last_job_count, last_error, active, "
        "not_found_count"
    )

    @staticmethod
    def create_board_tables(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS boards (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
                provider TEXT NOT NULL, token TEXT NOT NULL COLLATE NOCASE,
                shard TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                board_url TEXT NOT NULL, board_name TEXT,
                verified INTEGER NOT NULL DEFAULT 0, verification_method TEXT,
                added_at TEXT NOT NULL, last_fetched_at TEXT, last_job_count INTEGER,
                last_error TEXT, active INTEGER NOT NULL DEFAULT 1,
                not_found_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(provider, token, shard, site));
            CREATE TABLE IF NOT EXISTS board_listings (
                board_id INTEGER PRIMARY KEY REFERENCES boards(id) ON DELETE CASCADE,
                fetched_at TEXT NOT NULL, horizon_days INTEGER,
                complete INTEGER NOT NULL, postings TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS board_postings (
                board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL, fetched_at TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(board_id, source_id));
        """)

    def save_board(self, board, *, board_url, board_name, verified, method):
        """Insert or refresh a board by provider/token/shard/site; returns (row, created)."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            key = (board["provider"], board["token"], board["shard"], board["site"])
            existing = db.execute(
                "SELECT id FROM boards WHERE provider=? AND token=? AND shard=? AND site=?", key
            ).fetchone()
            values = (board["company"], board_url, board_name, int(verified), method)
            if existing:
                db.execute(
                    "UPDATE boards SET company=?, board_url=?, board_name=?, verified=?, "
                    "verification_method=?, active=1, not_found_count=0 WHERE id=?",
                    (*values, existing["id"]),
                )
                board_id = existing["id"]
            else:
                board_id = db.execute(
                    "INSERT INTO boards(company, board_url, board_name, verified, "
                    "verification_method, provider, token, shard, site, added_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (*values, *key, now()),
                ).lastrowid
            row = db.execute(
                f"SELECT {self.BOARD_COLUMNS} FROM boards WHERE id=?", (board_id,)
            ).fetchone()
            return self._board(row), not existing

    @staticmethod
    def _board(row):
        board = dict(row)
        board["verified"] = bool(board["verified"])
        board["active"] = bool(board["active"])
        return board

    def boards(self, active_only=False):
        with closing(self.connect()) as db:
            rows = db.execute(
                f"SELECT {self.BOARD_COLUMNS} FROM boards "
                + ("WHERE active=1 " if active_only else "")
                + "ORDER BY company COLLATE NOCASE, id"
            ).fetchall()
            return [self._board(row) for row in rows]

    def remove_boards(self, board_ids, deactivate=False):
        """Delete (with cached postings) or deactivate boards; returns affected rows."""
        if not board_ids:
            return []
        marks = ",".join("?" for _ in board_ids)
        with closing(self.connect()) as db, db:
            rows = db.execute(
                f"SELECT {self.BOARD_COLUMNS} FROM boards WHERE id IN ({marks})", board_ids
            ).fetchall()
            if deactivate:
                db.execute(f"UPDATE boards SET active=0 WHERE id IN ({marks})", board_ids)
            else:
                db.execute(f"DELETE FROM boards WHERE id IN ({marks})", board_ids)
            return [self._board(row) for row in rows]

    def board_listing(self, board_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT fetched_at, horizon_days, complete, postings FROM board_listings "
                "WHERE board_id=?",
                (board_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "fetched_at": row["fetched_at"],
                "horizon_days": row["horizon_days"],
                "complete": bool(row["complete"]),
                "postings": json.loads(row["postings"]),
            }

    def save_board_listing(self, board_id, postings, *, horizon_days, complete, job_count):
        timestamp = now()
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO board_listings VALUES(?,?,?,?,?) ON CONFLICT(board_id) DO UPDATE "
                "SET fetched_at=excluded.fetched_at, horizon_days=excluded.horizon_days, "
                "complete=excluded.complete, postings=excluded.postings",
                (board_id, timestamp, horizon_days, int(complete), json.dumps(postings)),
            )
            db.execute(
                "UPDATE boards SET last_fetched_at=?, last_job_count=?, last_error=NULL, "
                "not_found_count=0 WHERE id=?",
                (timestamp, job_count, board_id),
            )
        return timestamp

    def board_error(self, board_id, error, missing=False):
        """Record a fetch error. A missing board (HTTP 404/410) counts toward automatic
        retirement: at three consecutive misses the board is deactivated, with the error
        message kept. Returns whether this call deactivated the board."""
        with closing(self.connect()) as db, db:
            if not missing:
                db.execute("UPDATE boards SET last_error=? WHERE id=?", (error, board_id))
                return False
            row = db.execute(
                "SELECT not_found_count FROM boards WHERE id=?", (board_id,)
            ).fetchone()
            count = ((row["not_found_count"] if row else 0) or 0) + 1
            db.execute(
                "UPDATE boards SET last_error=?, not_found_count=?, "
                "active=CASE WHEN ?>=3 THEN 0 ELSE active END WHERE id=?",
                (error, count, count, board_id),
            )
            return count >= 3

    def board_postings(self, board_id, source_ids, fresh_after):
        if not source_ids:
            return {}
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT source_id, data FROM board_postings WHERE board_id=? AND fetched_at>=? "
                f"AND source_id IN ({','.join('?' for _ in source_ids)})",
                (board_id, fresh_after, *source_ids),
            ).fetchall()
            return {row["source_id"]: json.loads(row["data"]) for row in rows}

    def save_board_posting(self, board_id, source_id, data):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO board_postings VALUES(?,?,?,?) ON CONFLICT(board_id, source_id) "
                "DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data",
                (board_id, source_id, now(), json.dumps(data)),
            )

    def known(self, jobs):
        """History marks for jobs, by the same identities ingest() uses; never writes."""
        marks = []
        with closing(self.connect()) as db:
            for job in jobs:
                keys = identities(job)
                row = db.execute(
                    "SELECT j.id, j.status, j.historical, EXISTS("
                    "SELECT 1 FROM evaluations e WHERE e.job_id=j.id) evaluated "
                    "FROM identities i JOIN jobs j ON j.id=i.job_id "
                    f"WHERE i.key IN ({','.join('?' for _ in keys)}) LIMIT 1",
                    keys,
                ).fetchone()
                marks.append(
                    {
                        "job_id": row["id"],
                        "status": row["status"],
                        "historical": bool(row["historical"]),
                        "evaluated": bool(row["evaluated"]),
                    }
                    if row
                    else None
                )
        return marks
