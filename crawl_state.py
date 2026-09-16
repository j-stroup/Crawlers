"""SQLite-backed crawl state for deep_audit.py.

Holds the frontier (every URL seen, and whether it's still pending), the
findings, and per-site stats in a single SQLite file so that a long
crawl:

  * survives a crash - rerun the same command and it picks the frontier
    back up where it stopped instead of starting the whole site over;
  * stays within bounded memory - the frontier and the "have I seen this
    URL" set live on disk, not in a Python set that grows until the OS
    kills the process on a million-URL site.

Thread model: one connection opened with check_same_thread=False, every
access guarded by a single lock. DB operations are microseconds and the
crawl is network-bound (per-request delays measured in tenths of a
second), so that lock is effectively never contended - far simpler and
safer than juggling one connection per worker against WAL writers.

The frontier doubles as the visited-set: a URL is inserted exactly once
(INSERT OR IGNORE on its hash), so "already seen" is just "the insert
changed nothing". Each row carries a state:
    0 PENDING      - discovered, not yet fetched
    1 IN_PROGRESS  - claimed by a worker right now
    2 DONE         - fully processed
On resume, any IN_PROGRESS rows (claimed but never finished because the
process died) are reset to PENDING so they get retried.
"""

import hashlib
import json
import sqlite3
import threading

SCHEMA_VERSION = 2

PENDING = 0
IN_PROGRESS = 1
DONE = 2

# Fixed whitelist - these are the only column names ever interpolated
# into an UPDATE string, so it can never carry caller/site-controlled text.
_COUNTER_FIELDS = ("pages_crawled", "robots_blocked", "pattern_skipped")


def url_hash(url):
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()


class CrawlState:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        # In-memory mirror of the frontier row count, so the --max-pages
        # cap doesn't require a COUNT(*) over a huge table on every
        # enqueue. Seeded from disk here so it's correct across a resume.
        self._frontier_count = self.conn.execute(
            "SELECT COUNT(*) FROM frontier").fetchone()[0]
        self.max_pages = 0  # 0 = unlimited; set by the caller after open

    # -- schema / lifecycle -------------------------------------------------

    def _create_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS frontier (
                    urlhash TEXT PRIMARY KEY,
                    kind    TEXT NOT NULL,
                    url     TEXT NOT NULL,
                    source  TEXT,
                    depth   INTEGER NOT NULL DEFAULT 0,
                    state   INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_frontier_state ON frontier(state);
                CREATE TABLE IF NOT EXISTS findings (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    netloc   TEXT NOT NULL,
                    category TEXT NOT NULL,
                    url      TEXT,
                    detail   TEXT,
                    found_on TEXT,
                    kind     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_findings_netloc ON findings(netloc);
                CREATE TABLE IF NOT EXISTS site_stats (
                    netloc             TEXT PRIMARY KEY,
                    pages_crawled      INTEGER NOT NULL DEFAULT 0,
                    robots_blocked     INTEGER NOT NULL DEFAULT 0,
                    pattern_skipped    INTEGER NOT NULL DEFAULT 0,
                    header_checked_url TEXT,
                    missing_headers    TEXT
                );
                CREATE TABLE IF NOT EXISTS prefix_counts (
                    netloc TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    count  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (netloc, prefix)
                );
                CREATE TABLE IF NOT EXISTS path_query_counts (
                    netloc TEXT NOT NULL,
                    path   TEXT NOT NULL,
                    count  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (netloc, path)
                );
                """
            )
            self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Additive column migrations for state files created by an
        older schema version. Cheap and idempotent - a fresh DB just
        finds the columns already present."""
        with self.lock:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(site_stats)")]
            if "query_skipped" not in cols:
                self.conn.execute(
                    "ALTER TABLE site_stats ADD COLUMN query_skipped INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def reset(self):
        """Wipe crawl data for a fresh start, keeping the file/schema."""
        with self.lock:
            self.conn.executescript(
                "DELETE FROM frontier; DELETE FROM findings; "
                "DELETE FROM site_stats; DELETE FROM prefix_counts; "
                "DELETE FROM path_query_counts; "
                "DELETE FROM meta WHERE key='completed_at';"
            )
            self.conn.commit()
            self._frontier_count = 0

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self.conn.commit()

    # -- frontier -----------------------------------------------------------

    def enqueue(self, kind, url, source, depth):
        """Insert a URL to crawl. Returns True if it was newly added,
        False if it was already known (dedup) or the --max-pages cap is
        hit. The hash PK makes 'already known' a no-op insert."""
        with self.lock:
            if self.max_pages and self._frontier_count >= self.max_pages:
                return False
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO frontier(urlhash,kind,url,source,depth,state) "
                "VALUES(?,?,?,?,?,0)",
                (url_hash(url), kind, url, source, depth))
            self.conn.commit()
            if cur.rowcount:
                self._frontier_count += 1
                return True
            return False

    def claim(self):
        """Atomically take the next PENDING row and mark it IN_PROGRESS.
        Returns (urlhash, kind, url, source, depth) or None."""
        with self.lock:
            row = self.conn.execute(
                "SELECT urlhash,kind,url,source,depth FROM frontier "
                "WHERE state=0 LIMIT 1").fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE frontier SET state=1 WHERE urlhash=?", (row[0],))
            self.conn.commit()
            return row

    def mark_done(self, urlhash):
        with self.lock:
            self.conn.execute(
                "UPDATE frontier SET state=2 WHERE urlhash=?", (urlhash,))
            self.conn.commit()

    def pending_and_active(self):
        with self.lock:
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM frontier WHERE state=0").fetchone()[0]
            active = self.conn.execute(
                "SELECT COUNT(*) FROM frontier WHERE state=1").fetchone()[0]
        return pending, active

    def requeue_in_progress(self):
        """Reset rows a dead run left claimed. Returns how many."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE frontier SET state=0 WHERE state=1")
            self.conn.commit()
            return cur.rowcount

    def counts_by_state(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT state, COUNT(*) FROM frontier GROUP BY state").fetchall()
        by = {PENDING: 0, IN_PROGRESS: 0, DONE: 0}
        for state, n in rows:
            by[state] = n
        return by

    # -- findings -----------------------------------------------------------

    def add_finding(self, netloc, category, url, detail=None, found_on=None, kind=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO findings(netloc,category,url,detail,found_on,kind) "
                "VALUES(?,?,?,?,?,?)",
                (netloc, category, url,
                 "" if detail is None else str(detail), found_on, kind))
            self.conn.commit()

    def fetch_findings(self, netloc, category):
        """DISTINCT so a page re-crawled after a resume can't show the
        same finding twice."""
        with self.lock:
            return self.conn.execute(
                "SELECT DISTINCT url, detail, found_on, kind FROM findings "
                "WHERE netloc=? AND category=? ORDER BY url",
                (netloc, category)).fetchall()

    # -- per-site stats -----------------------------------------------------

    def _ensure_site(self, netloc):
        self.conn.execute(
            "INSERT OR IGNORE INTO site_stats(netloc) VALUES(?)", (netloc,))

    def incr_stat(self, netloc, field, n=1):
        if field not in _COUNTER_FIELDS:
            raise ValueError(f"unknown stat field: {field}")
        with self.lock:
            self._ensure_site(netloc)
            self.conn.execute(
                f"UPDATE site_stats SET {field}={field}+? WHERE netloc=?",
                (n, netloc))
            self.conn.commit()

    def record_headers_once(self, netloc, url, missing_list):
        """Record the missing-security-header result for a site, but only
        the first time (header config is site-wide). Returns True if this
        call is the one that recorded it."""
        with self.lock:
            self._ensure_site(netloc)
            row = self.conn.execute(
                "SELECT header_checked_url FROM site_stats WHERE netloc=?",
                (netloc,)).fetchone()
            if row and row[0]:
                return False
            self.conn.execute(
                "UPDATE site_stats SET header_checked_url=?, missing_headers=? "
                "WHERE netloc=?",
                (url, json.dumps(missing_list), netloc))
            self.conn.commit()
            return True

    def bump_prefix(self, netloc, prefix, cap):
        """Path-prefix bucket cap. Returns True if still under the cap
        (and counts this hit), False once the bucket is full."""
        with self.lock:
            self._ensure_site(netloc)
            row = self.conn.execute(
                "SELECT count FROM prefix_counts WHERE netloc=? AND prefix=?",
                (netloc, prefix)).fetchone()
            count = row[0] if row else 0
            if count >= cap:
                self.conn.execute(
                    "UPDATE site_stats SET pattern_skipped=pattern_skipped+1 "
                    "WHERE netloc=?", (netloc,))
                self.conn.commit()
                return False
            if row:
                self.conn.execute(
                    "UPDATE prefix_counts SET count=count+1 "
                    "WHERE netloc=? AND prefix=?", (netloc, prefix))
            else:
                self.conn.execute(
                    "INSERT INTO prefix_counts(netloc,prefix,count) VALUES(?,?,1)",
                    (netloc, prefix))
            self.conn.commit()
            return True

    def bump_path_query(self, netloc, path, cap):
        """Query-variant trap cap. Counts how many distinct query-string
        variants of the same path have been enqueued; returns True while
        under the cap, False once it's full (a calendar / faceted-search
        trap that would otherwise spin out thousands of near-identical
        URLs). Counts a skip against query_skipped."""
        with self.lock:
            self._ensure_site(netloc)
            row = self.conn.execute(
                "SELECT count FROM path_query_counts WHERE netloc=? AND path=?",
                (netloc, path)).fetchone()
            count = row[0] if row else 0
            if count >= cap:
                self.conn.execute(
                    "UPDATE site_stats SET query_skipped=query_skipped+1 "
                    "WHERE netloc=?", (netloc,))
                self.conn.commit()
                return False
            if row:
                self.conn.execute(
                    "UPDATE path_query_counts SET count=count+1 "
                    "WHERE netloc=? AND path=?", (netloc, path))
            else:
                self.conn.execute(
                    "INSERT INTO path_query_counts(netloc,path,count) VALUES(?,?,1)",
                    (netloc, path))
            self.conn.commit()
            return True

    # -- reporting ----------------------------------------------------------

    def netlocs_with_data(self):
        with self.lock:
            a = {r[0] for r in self.conn.execute("SELECT netloc FROM site_stats")}
            b = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT netloc FROM findings")}
        return sorted(a | b)

    def fetch_stats(self, netloc):
        with self.lock:
            row = self.conn.execute(
                "SELECT pages_crawled, robots_blocked, pattern_skipped, "
                "header_checked_url, missing_headers, query_skipped FROM site_stats "
                "WHERE netloc=?", (netloc,)).fetchone()
        if not row:
            return {
                "pages_crawled": 0, "robots_blocked": 0, "pattern_skipped": 0,
                "header_checked_url": None, "missing_headers": [], "query_skipped": 0,
            }
        return {
            "pages_crawled": row[0],
            "robots_blocked": row[1],
            "pattern_skipped": row[2],
            "header_checked_url": row[3],
            "missing_headers": json.loads(row[4]) if row[4] else [],
            "query_skipped": row[5],
        }
