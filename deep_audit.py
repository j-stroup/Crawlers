#!/usr/bin/env python3
"""Deep Dive - threaded, multi-site site auditor.

Not a vulnerability scanner (this isn't ZAP) - it's a much more thorough
housekeeping crawl than quick_scan.py: broken links and resources,
response codes worth a second look, exposed directory listings, missing
security headers, file types that shouldn't be publicly exposed, query
parameters worth a second look, and HTML/JS source scanned for obvious
leaked API keys/credentials. Meant to run for hours across one or many
sites in a single session.

Findings are tuned for signal over noise: secrets are tiered (a real
leaked AWS key vs a public-by-design Google Maps key, with placeholder/
low-entropy matches dropped), query params are flagged by dangerous
*value* (a URL-ish redirect target, path traversal, a debug toggle) not
just a scary name, sensitive file types separate always-bad (.sql/.env/
.bak) from context-dependent (.json/.xml only when the filename looks like
a dump), and 429/503 rate-limiting triggers per-host backoff instead of
being mislabelled as a broken link.

It also surfaces cheap extra signals seen while crawling: mixed content
(http sub-resources on an https page), cookies missing Secure/HttpOnly/
SameSite, version-disclosure headers, an inventory of third-party script
hosts, and target=_blank links missing rel=noopener. With
--probe-common-paths it will additionally request a small fixed list of
paths that shouldn't be public (.git/HEAD, .env, backup archives) even if
nothing links to them - the one opt-in active check.

<a href> pages within scope (same domain, or anywhere if --allow-external)
get fully crawled. External links (when not --allow-external) and any
<img>/<link rel=stylesheet|icon>/<iframe>/external <script> resource get
a status check but are never crawled further.

Crawl state (the frontier, the seen-URL set, findings and per-site stats)
lives in a SQLite file, not in RAM - see crawl_state.py. Two consequences
matter for the multi-day crawls this is built for:
  * Bounded memory - the frontier and visited-set are on disk, so a
    million-URL site doesn't grow the process until it's killed.
  * Resume after a crash - rerun the exact same command and it picks the
    frontier back up where it stopped instead of re-crawling the site.
    --fresh forces a clean start; --state-db points at a specific file.

Report files are also checkpointed periodically during the crawl, so an
interrupted run still leaves usable (if slightly stale) .txt/.html
reports.

URLs are canonicalized before dedup (lowercased host, default port and
tracking params like utm_*/fbclid/gclid stripped, query params sorted),
so the same page linked a dozen different ways is crawled once. Three
independent caps keep a huge or trap-laden site from running forever
without crawling any faster:
  * --max-per-path-prefix N  - at most N pages under the same leading
    path segment(s) (years of /bids/... postings).
  * --max-query-variants N   - at most N query-string variants per path
    (calendar/faceted-search traps: /events?date=..., /search?a=&b=...).
  * --max-depth N            - stop following links past N hops from a seed.

Examples:
    python deep_audit.py https://example.com
    python deep_audit.py https://example.com https://other-site.com --threads 15
    python deep_audit.py https://example.com --allow-external --max-pages 20000
    python deep_audit.py https://example.com --ignore-robots --proxy http://127.0.0.1:8080
    python deep_audit.py https://example.com --max-per-path-prefix 50 --max-query-variants 20
    python deep_audit.py https://example.com --max-depth 6
    python deep_audit.py https://example.com --fresh   # ignore any saved state
"""

import argparse
import hashlib
import json
import math
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, unquote
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from report_html import make_row, write_html_report
from crawl_state import CrawlState, PENDING, IN_PROGRESS, SCHEMA_VERSION

# --------------------------------------------------------------------------
# Classification tables. Heuristic by design - the point is "flag it for a
# human to look at", not "this is definitely a vulnerability". The tiering
# below exists so the genuinely-worrying findings don't drown in the
# expected-but-noisy ones (a real leaked AWS key vs a public Google Maps
# key that's meant to ship in the page).
# --------------------------------------------------------------------------

# Legacy/inaccessible document types - the original PDF pet peeve, still
# just as relevant on a deep audit as a quick one.
LEGACY_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")

# File types that should essentially never be publicly reachable - a hit
# is almost always a real accidental exposure regardless of filename.
HIGH_SENSITIVE_EXTENSIONS = (
    ".db", ".sqlite", ".sqlite3", ".sql", ".dump", ".bak", ".backup", ".old",
    ".swp", ".env", ".key", ".pem", ".log", ".conf", ".config", ".ini",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
)

# File types that are perfectly normal on a modern site (APIs, sitemaps,
# manifests, feeds) - flagging every one is pure noise. We only flag these
# when the filename itself looks like a data dump (see DUMPY_HINTS).
CONTEXT_SENSITIVE_EXTENSIONS = (
    ".txt", ".xml", ".json", ".yml", ".yaml", ".csv",
)

# Well-known benign filenames that use a context-sensitive extension but
# are supposed to be public - never flag these.
BENIGN_STEMS = {
    "sitemap", "sitemap_index", "sitemap-index", "manifest", "robots",
    "ads", "app-ads", "security", "humans", "opensearch", "browserconfig",
    "site", "feed", "rss", "atom", "composer", "package", "package-lock",
    "tsconfig", "asset-manifest", "ngsw", "swagger", "openapi",
}

# Filename fragments that make a .json/.csv/.xml/etc. look like an exported
# data dump rather than a normal API/config file.
DUMPY_HINTS = (
    "backup", "bak", "dump", "export", "users", "user", "account", "accounts",
    "customer", "member", "email", "password", "passwd", "credential",
    "secret", "private", "database", "_db", "db_", "records", "payroll",
    "ssn", "salary", "finance", "invoice", "orders", "transactions",
    "config", "settings", "old", "archive", "prod", "production",
)

# Status codes that aren't a plain 404 but are worth a second look:
# auth-related (401/403/407), or server-side errors that can leak stack
# traces / internals (5xx). 429 is deliberately NOT here - it means we're
# being rate-limited (see rate-limit handling), not that the page is broken.
SECURITY_STATUS_CODES = {401, 403, 407, 500, 501, 502, 503, 504, 505, 507, 511}

# Status codes that mean "slow down", handled with backoff + retry rather
# than recorded as broken links.
RATE_LIMIT_CODES = {429, 503}

# Query params whose *name* alone is worth a note when it carries a truthy
# value - debug/admin toggles that shouldn't be reachable in production.
TOGGLE_PARAM_NAMES = {"debug", "test", "admin", "trace", "verbose", "dev"}
TRUTHY_VALUES = {"1", "true", "yes", "on", "y", "enable", "enabled", "debug"}
# Params that commonly carry a redirect/callback target - a URL-ish value
# in one of these is a stronger open-redirect/SSRF signal.
REDIRECT_PARAM_NAMES = {
    "redirect", "redir", "return", "returnurl", "return_url", "returnto",
    "next", "url", "uri", "dest", "destination", "continue", "goto", "target",
    "out", "link", "callback", "cb", "u", "r", "to", "forward", "image_url",
    "img", "load", "page_url", "feed", "host", "domain", "site", "data",
}

# Secret detection. Each pattern carries a tier:
#   high    - structured, high-confidence (a real leak if genuine)
#   medium  - plausible but needs a look (generic assignments, raw JWTs)
#   info    - public-by-design keys that are SUPPOSED to ship in the page
#             (browser Google/Firebase keys, Stripe publishable keys)
# "generic" patterns capture the value in group(1) and are entropy-gated
# so `password = "password"` and `api_key = "your_api_key_here"` don't fire.
SECRET_PATTERNS = [
    ("AWS Access Key ID", "high", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API Key", "info", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Slack Token", "high", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,48}")),
    ("Stripe Live Secret Key", "high", re.compile(r"sk_live_[0-9a-zA-Z]{16,}")),
    ("Stripe Publishable Key", "info", re.compile(r"pk_(?:live|test)_[0-9a-zA-Z]{16,}")),
    ("Private Key Block", "high",
     re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PGP)?\s?PRIVATE KEY-----")),
    ("JWT", "medium",
     re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Generic API Key/Secret Assignment", "generic",
     re.compile(r"(?i)(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token)"
                r"\s*[:=]\s*['\"]([A-Za-z0-9_\-\.]{16,})['\"]")),
    ("Password Assignment", "generic",
     re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]")),
]

# Minimum Shannon entropy (bits/char) for a generic-pattern value to be
# treated as a real token rather than a word/placeholder.
GENERIC_ENTROPY_MIN = 3.0

# "Strong" placeholder markers: unambiguous example/template text. Safe to
# apply even to long structured keys (AKIA.../AIza...) - real keys don't
# contain the word "example" or "changeme".
STRONG_PLACEHOLDER_HINTS = (
    "example", "placeholder", "your_", "your-", "yourkey", "changeme",
    "change_me", "change-me", "insert", "xxxx", "<", "sample", "dummy",
    "todo", "replace", "redacted", "notset", "not_set",
    "akiaiosfodnn7example",
)

# "Weak" markers: common words / digit runs that reliably mean "fake" in a
# free-form assignment value, but would false-drop a real high-entropy key
# that happens to contain them by chance (e.g. a random blob containing
# "123456"). So these apply ONLY to generic-assignment matches, never to
# the structured high/info patterns.
WEAK_PLACEHOLDER_HINTS = (
    "test", "demo", "foobar", "password", "passwd", "secret", "apikey",
    "api_key", "token", "abc123", "123456", "qwerty", "n/a", "none",
    "null", "undefined",
)

# Directory-listing "Index of /" pages (Apache mod_autoindex, nginx
# autoindex, classic IIS) - almost always an accidental exposure.
DIR_LISTING_RE = re.compile(
    r"<title>\s*index of\s*/|<h1[^>]*>\s*index of\s*/|directory listing (?:for|--)\s*/",
    re.IGNORECASE,
)

# Baseline response headers worth having; checked once per site against
# the seed/homepage (depth 0) specifically - header config is normally
# set site-wide (web server/CDN), so one representative page is enough,
# and pinning it to the homepage avoids the old nondeterministic "whatever
# page a thread happened to finish first" behaviour.
SECURITY_HEADERS = (
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Strict-Transport-Security",
    "Referrer-Policy",
)

# Response headers that leak software/version info an attacker can use to
# look up known CVEs. Reported for information, on the homepage only.
VERSION_DISCLOSURE_HEADERS = (
    "Server", "X-Powered-By", "X-AspNet-Version", "X-AspNetMvc-Version",
    "X-Generator", "X-Runtime", "X-Drupal-Cache", "Via",
)
_HAS_DIGIT = re.compile(r"\d")

# HTML tags whose sub-resource, when loaded over http:// on an https page,
# is mixed content. (tag, attribute-holding-the-url)
MIXED_CONTENT_TAGS = (
    ("script", "src"), ("link", "href"), ("img", "src"), ("iframe", "src"),
    ("source", "src"), ("audio", "src"), ("video", "src"), ("embed", "src"),
    ("object", "data"), ("track", "src"),
)

# Paths that shouldn't be publicly reachable - only probed when the user
# opts in with --probe-common-paths (this is the one active check that
# requests URLs the site never linked to). security.txt is intentionally
# NOT here: it's supposed to be reachable.
COMMON_PROBE_PATHS = (
    "/.git/HEAD", "/.git/config", "/.env", "/.env.bak", "/.env.local",
    "/.svn/entries", "/.hg/store", "/.DS_Store", "/.htaccess",
    "/backup.zip", "/backup.tar.gz", "/backup.sql", "/database.sql",
    "/db.sql", "/dump.sql", "/config.php.bak", "/wp-config.php.bak",
    "/.aws/credentials", "/id_rsa",
)

DEFAULT_TIMEOUT = 15


def mask(secret):
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def shannon_entropy(s):
    """Bits of entropy per character - low for words/placeholders, high
    for random tokens. Used to keep generic `password = "..."` matches
    from firing on `password = "password"`."""
    if not s:
        return 0.0
    n = len(s)
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def is_placeholder_secret(value, weak_hints=True):
    """True if a captured secret is an obvious example/placeholder rather
    than a live credential. weak_hints=False applies only the unambiguous
    markers - used for long structured keys, which can contain a digit run
    like '123456' by pure chance without being fake."""
    v = value.strip().lower()
    if len(set(v)) <= 2:            # 'xxxxxxxx', '00000000', 'aaaa...'
        return True
    if any(hint in v for hint in STRONG_PLACEHOLDER_HINTS):
        return True
    if weak_hints and any(hint in v for hint in WEAK_PLACEHOLDER_HINTS):
        return True
    return False


def classify_secret(tier, value):
    """Return the effective tier for a secret match ('high'/'medium'/
    'info'), or None to drop it as a placeholder / low-entropy noise."""
    if tier == "generic":
        if is_placeholder_secret(value, weak_hints=True):
            return None
        if len(value) < 12 or shannon_entropy(value) < GENERIC_ENTROPY_MIN:
            return None
        return "medium"
    # structured high/medium/info key - only the unambiguous placeholders
    if is_placeholder_secret(value, weak_hints=False):
        return None
    return tier


def sensitive_verdict(url):
    """Classify a URL's file type: 'high' for types that should never be
    public, 'context' for a normally-benign type whose *filename* looks
    like a data dump, or None to not flag it."""
    path = urlparse(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    if path.endswith(HIGH_SENSITIVE_EXTENSIONS):
        return "high"
    if path.endswith(CONTEXT_SENSITIVE_EXTENSIONS):
        stem = name.rsplit(".", 1)[0]
        if stem in BENIGN_STEMS:
            return None
        if any(hint in name for hint in DUMPY_HINTS):
            return "context"
    return None


def _looks_like_url(value):
    v = value.strip().lower()
    return v.startswith(("http://", "https://", "//", "/\\", "\\\\")) or "://" in v


def analyze_params(url):
    """Value-aware query-param check. Returns a list of short reason
    strings for genuinely interesting params (a URL-ish redirect target,
    path traversal, a debug/admin toggle turned on), or [] - a plain
    `?id=5` or `?page=2` no longer trips anything."""
    reasons = []
    parsed = urlparse(url)
    if not parsed.query:
        return reasons
    for kv in parsed.query.split("&"):
        if not kv:
            continue
        name, _, raw = kv.partition("=")
        name_l = name.lower()
        value = unquote(raw)
        low = value.lower()

        if _looks_like_url(value):
            if name_l in REDIRECT_PARAM_NAMES:
                reasons.append(f"{name}= points at a URL ({value[:60]}) - open-redirect/SSRF candidate")
            else:
                reasons.append(f"{name}= carries a URL ({value[:60]}) - possible SSRF/redirect")
        if "../" in value or "..\\" in value or "%2e%2e" in raw.lower():
            reasons.append(f"{name}= contains path traversal ({value[:60]})")
        elif re.search(r"/(etc|proc|var|windows|boot\.ini|web\.config)/?", low):
            reasons.append(f"{name}= looks like a filesystem path ({value[:60]})")
        if name_l in TOGGLE_PARAM_NAMES and low in TRUTHY_VALUES:
            reasons.append(f"{name}={value} - debug/admin toggle enabled")
    return reasons


def normalize_netloc(netloc):
    """'www.example.com' and 'example.com' are the same site - strip a
    leading 'www.' before comparing/grouping so the crawler doesn't
    treat one as external, or split findings across two reports, just
    because a link happens to use the other."""
    netloc = netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


# Analytics / ad click-tracking parameters that never change which page
# you get back - only who referred you. Stripped before dedup so the same
# page linked with a dozen different utm_* tags is crawled once, not a
# dozen times.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "igsh", "_ga", "_gl", "yclid", "ttclid",
    "twclid", "vero_id", "oly_anon_id", "oly_enc_id", "s_cid", "ref_src",
    "spm", "scm", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src", "hsa_net",
}


def canonicalize_url(url):
    """Fold URLs that point at the same resource into one dedup key:
    lowercase scheme/host, drop the default port, collapse duplicate
    slashes, strip the fragment, remove tracking params, and sort the
    remaining query so parameter order doesn't create phantom duplicates.
    Deliberately conservative - trailing slashes are left alone, since
    some servers really do treat /foo and /foo/ as different pages."""
    try:
        p = urlparse(url)
    except Exception:
        return url

    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = re.sub(r"/{2,}", "/", p.path) or "/"

    if p.query:
        kept = [kv for kv in p.query.split("&")
                if kv and kv.split("=", 1)[0].lower() not in TRACKING_PARAMS]
        kept.sort()
        query = "&".join(kept)
    else:
        query = ""

    return urlunparse((scheme, netloc, path, p.params, query, ""))


def site_key(url):
    return normalize_netloc(urlparse(url).netloc)


def site_name(netloc):
    return netloc.replace(":", "_") or "site"


# --------------------------------------------------------------------------
# Per-site findings - now purely a container used to build a report. The
# crawl writes findings straight to SQLite; load_findings() reconstitutes
# one of these from the DB at report time.
# --------------------------------------------------------------------------

class SiteFindings:
    def __init__(self, netloc):
        self.netloc = netloc
        self.pages_crawled = 0
        self.robots_blocked = 0
        self.pattern_skipped = 0
        self.query_skipped = 0
        self.broken = []             # (url, status_or_error, found_on)
        self.broken_resources = []   # (url, status_or_error, found_on, kind)
        self.security_codes = []     # (url, status, found_on)
        self.directory_listings = [] # (url, status, found_on)
        self.legacy_files = []       # (url, status, found_on)
        self.sensitive_files = []    # (url, status, found_on)
        self.risky_params = []       # (url, [reasons], found_on)
        self.secrets = []            # (url, label, masked_value)   high/medium
        self.secrets_info = []       # (url, label, masked_value)   public-by-design
        self.rate_limited = []       # (url, detail, found_on)
        self.mixed_content = []      # (url, detail, found_on)
        self.cookie_flags = []       # (url, detail, found_on)
        self.exposed_paths = []      # (url, detail, found_on)
        self.version_disclosure = [] # (url, detail, found_on)
        self.third_party_scripts = []# (url, detail, found_on)
        self.tabnabbing = []         # (url, detail, found_on)
        self.header_checked_url = None
        self.missing_security_headers = []


def load_findings(db, netloc):
    """Rebuild a SiteFindings from the SQLite state for reporting."""
    f = SiteFindings(netloc)
    stats = db.fetch_stats(netloc)
    f.pages_crawled = stats["pages_crawled"]
    f.robots_blocked = stats["robots_blocked"]
    f.pattern_skipped = stats["pattern_skipped"]
    f.query_skipped = stats["query_skipped"]
    f.header_checked_url = stats["header_checked_url"]
    f.missing_security_headers = stats["missing_headers"]

    for url, detail, found_on, kind in db.fetch_findings(netloc, "broken"):
        f.broken.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "broken_resource"):
        f.broken_resources.append((url, detail, found_on, kind))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "security_code"):
        f.security_codes.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "directory_listing"):
        f.directory_listings.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "legacy_file"):
        f.legacy_files.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "sensitive_file"):
        f.sensitive_files.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "risky_param"):
        # detail is a single reason string; keep it intact as one item
        f.risky_params.append((url, [detail] if detail else [], found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "secret"):
        # stored as detail=masked, kind=label
        f.secrets.append((url, kind, detail))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "secret_info"):
        f.secrets_info.append((url, kind, detail))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "rate_limited"):
        f.rate_limited.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "mixed_content"):
        f.mixed_content.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "cookie_flag"):
        f.cookie_flags.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "exposed_path"):
        f.exposed_paths.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "version_disclosure"):
        f.version_disclosure.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "third_party_script"):
        f.third_party_scripts.append((url, detail, found_on))
    for url, detail, found_on, kind in db.fetch_findings(netloc, "tabnabbing"):
        f.tabnabbing.append((url, detail, found_on))
    return f


# --------------------------------------------------------------------------
# Robots.txt handling
# --------------------------------------------------------------------------

class RobotsCache:
    def __init__(self, ignore=False):
        self.ignore = ignore
        self.parsers = {}
        self.lock = threading.Lock()

    def can_fetch(self, url):
        if self.ignore:
            return True
        parsed = urlparse(url)
        netloc = parsed.netloc
        with self.lock:
            rp = self.parsers.get(netloc)
            if rp is None:
                rp = RobotFileParser()
                robots_url = f"{parsed.scheme}://{netloc}/robots.txt"
                try:
                    rp.set_url(robots_url)
                    rp.read()
                except Exception:
                    rp = False  # couldn't fetch -> treat as allow-all
                self.parsers[netloc] = rp
        if rp is False:
            return True
        return rp.can_fetch("*", url)


# --------------------------------------------------------------------------
# Per-host adaptive backoff
# --------------------------------------------------------------------------

class HostThrottle:
    """When a host answers 429/503, we back off *that host* for a cooldown
    window that every thread honours - so a rate-limited site makes the
    whole crawl politely slow down instead of hammering it (and instead of
    mislabelling the throttling as broken links). Cooldown grows on repeat
    offences and decays as the host behaves."""

    BASE_COOLDOWN = 2.0   # seconds, first offence
    MAX_COOLDOWN = 60.0

    def __init__(self):
        self.until = {}       # netloc -> wall-clock time it's ok to resume
        self.streak = {}      # netloc -> consecutive-offence count
        self.lock = threading.Lock()

    def wait(self, netloc):
        """Block until this host's cooldown (if any) has elapsed."""
        while True:
            with self.lock:
                resume_at = self.until.get(netloc, 0)
            remaining = resume_at - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    def penalize(self, netloc, retry_after=None):
        """Record a rate-limit hit; returns the cooldown seconds applied."""
        with self.lock:
            streak = self.streak.get(netloc, 0) + 1
            self.streak[netloc] = streak
            backoff = self.BASE_COOLDOWN * (2 ** (streak - 1))
            if retry_after is not None:
                backoff = max(backoff, retry_after)
            backoff = min(backoff, self.MAX_COOLDOWN)
            self.until[netloc] = time.time() + backoff
            return backoff

    def relax(self, netloc):
        """A clean response - reset the offence streak for this host."""
        with self.lock:
            if self.streak.get(netloc):
                self.streak[netloc] = 0


def parse_retry_after(resp):
    """Retry-After header in seconds (an HTTP-date form is ignored - we
    fall back to our own backoff for those)."""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Crawl context shared across worker threads
# --------------------------------------------------------------------------

class Context:
    def __init__(self, args, seed_netlocs, db):
        self.args = args
        self.db = db
        self.seed_netlocs = seed_netlocs
        self.allow_external = args.allow_external
        self.timeout = args.timeout
        self.delay_min = args.delay_min
        self.delay_max = args.delay_max
        self.ignore_robots = args.ignore_robots

        self.robots = RobotsCache(ignore=args.ignore_robots)
        self.throttle = HostThrottle()
        self.max_retries = args.max_retries
        self._local = threading.local()

        self.proxy_list = self._load_proxies(args.proxy, args.proxy_file)
        self.stop_event = threading.Event()
        self.processed_count = 0
        self.count_lock = threading.Lock()

        # Path-prefix bucket cap - keeps a site with a huge flat archive
        # (years of /bids/... postings, etc.) from eating the whole run.
        # The counts live in SQLite; the *_announced sets are just so the
        # "limit reached" line prints once per bucket per process.
        self.max_per_prefix = args.max_per_path_prefix
        self.prefix_depth = args.path_prefix_depth
        self.prefix_announced = set()
        self.prefix_lock = threading.Lock()

        # Trap caps (crawler-trap defenses that cost no extra requests).
        self.max_depth = args.max_depth                 # link distance from a seed
        self.max_query_variants = args.max_query_variants  # per-path query explosion
        self.query_announced = set()

        # De-dup keys for the "report each thing once per site" checks
        # (cookies, third-party script hosts, mixed-content URLs, ...).
        self._seen = set()
        self._seen_lock = threading.Lock()

    def mark_seen(self, kind, netloc, key):
        """Return True the first time (kind, netloc, key) is seen, False
        after - so a site-wide observation (a cookie, an external script
        host) is recorded once instead of on every page."""
        token = (kind, netloc, key)
        with self._seen_lock:
            if token in self._seen:
                return False
            self._seen.add(token)
            return True

    @staticmethod
    def _load_proxies(proxy, proxy_file):
        proxies = []
        if proxy:
            proxies.append(proxy)
        if proxy_file:
            with open(proxy_file, "r", encoding="utf-8") as f:
                proxies.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))
        return proxies

    def get_proxies(self):
        if not self.proxy_list:
            return None
        chosen = random.choice(self.proxy_list)
        return {"http": chosen, "https": chosen}

    def session(self):
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

    def enqueue(self, kind, url, source, depth, respect_domain=True):
        """Canonicalize, apply the scope + trap gates, then hand the URL
        to the DB frontier (which dedups by URL hash). respect_domain=
        False is for leaf checks (resources, external links) - status-
        checked wherever they point but never crawled further, so the
        scope/depth/prefix/query gates don't apply to them."""
        url = canonicalize_url(url)
        if respect_domain and not self.allow_external:
            if normalize_netloc(urlparse(url).netloc) not in self.seed_netlocs:
                return
        if respect_domain and self.max_depth and depth > self.max_depth:
            return
        if respect_domain and self.max_per_prefix and not self._allow_by_prefix(url):
            return
        if respect_domain and self.max_query_variants and not self._allow_by_query(url):
            return
        self.db.enqueue(kind, url, source, depth)

    def _allow_by_prefix(self, url):
        parsed = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        prefix = "/".join(segments[:self.prefix_depth])
        if not prefix:
            return True  # homepage / root - never bucket-limited
        netloc = normalize_netloc(parsed.netloc)
        if self.db.bump_prefix(netloc, prefix, self.max_per_prefix):
            return True
        with self.prefix_lock:
            if (netloc, prefix) not in self.prefix_announced:
                self.prefix_announced.add((netloc, prefix))
                print(f"  [PATTERN LIMIT] /{prefix} on {netloc} reached "
                      f"{self.max_per_prefix} pages crawled - skipping further matches")
        return False

    def _allow_by_query(self, url):
        """Cap distinct query-string variants of the same path - the
        classic calendar (?date=…→forever) / faceted-search
        (?color=&size=&sort=…) trap. Only URLs that actually carry a
        query are limited; a plain path is never capped here."""
        parsed = urlparse(url)
        if not parsed.query:
            return True
        netloc = normalize_netloc(parsed.netloc)
        if self.db.bump_path_query(netloc, parsed.path, self.max_query_variants):
            return True
        with self.prefix_lock:
            if (netloc, parsed.path) not in self.query_announced:
                self.query_announced.add((netloc, parsed.path))
                print(f"  [QUERY LIMIT] {parsed.path or '/'} on {netloc} reached "
                      f"{self.max_query_variants} query variants - skipping further ones")
        return False

    def bump_progress(self):
        with self.count_lock:
            self.processed_count += 1
            n = self.processed_count
        if n % 50 == 0:
            print(f"  ... {n} URLs processed so far")


# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------

def scan_for_secrets(text, url, netloc, db):
    seen = set()  # (label, value) - a minified bundle repeats the same hit a lot
    for label, base_tier, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            tier = classify_secret(base_tier, value)
            if tier is None:
                continue  # placeholder / low-entropy - dropped
            dedup = (label, value)
            if dedup in seen:
                continue
            seen.add(dedup)
            category = "secret_info" if tier == "info" else "secret"
            db.add_finding(netloc, category, url,
                           detail=mask(value), kind=f"{label} ({tier})")
            tag = "PUBLIC KEY" if tier == "info" else "SECRET?"
            print(f"  [{tag}] {label} ({tier}) on {url}")


def check_security_headers(resp, url, netloc, db):
    missing = [h for h in SECURITY_HEADERS if h not in resp.headers]
    if db.record_headers_once(netloc, url, missing) and missing:
        print(f"  [HEADERS] {url} missing: {', '.join(missing)}")


def check_response_extras(ctx, resp, url, netloc, depth):
    """Header/cookie-level checks that need the response object: cookie
    flags (once per cookie name per site) and version-disclosure headers
    (once per site, on the homepage)."""
    for cookie in resp.cookies:
        rest = {k.lower() for k in (cookie._rest or {})}
        missing = []
        if not cookie.secure:
            missing.append("Secure")
        if "httponly" not in rest:
            missing.append("HttpOnly")
        if "samesite" not in rest:
            missing.append("SameSite")
        if missing and ctx.mark_seen("cookie", netloc, cookie.name):
            ctx.db.add_finding(netloc, "cookie_flag", url,
                               f"cookie '{cookie.name}' missing: {', '.join(missing)}", url)
            print(f"  [COOKIE] '{cookie.name}' missing {', '.join(missing)} on {url}")

    if depth == 0:
        disclosed = []
        for header in VERSION_DISCLOSURE_HEADERS:
            value = resp.headers.get(header)
            if value and (header.lower().startswith("x-") or _HAS_DIGIT.search(value)):
                disclosed.append(f"{header}: {value}")
        if disclosed and ctx.mark_seen("ver", netloc, "_"):
            ctx.db.add_finding(netloc, "version_disclosure", url, "; ".join(disclosed), url)
            print(f"  [VERSION] {url} -> {'; '.join(disclosed)}")


def analyze_html_extras(ctx, url, soup, netloc):
    """Parse-time checks that cost no extra requests: mixed content on an
    https page, a third-party-script inventory, and target=_blank links
    missing rel=noopener."""
    page = urlparse(url)
    page_host = normalize_netloc(page.netloc)

    if page.scheme == "https":
        for tag_name, attr in MIXED_CONTENT_TAGS:
            for tag in soup.find_all(tag_name):
                value = tag.get(attr)
                if not value:
                    continue
                absu = urljoin(url, value)
                if absu.lower().startswith("http://") and ctx.mark_seen("mixed", netloc, absu):
                    ctx.db.add_finding(netloc, "mixed_content", absu,
                                       f"<{tag_name}> loaded over http on an https page", url)
                    print(f"  [MIXED CONTENT] {absu} on {url}")
        for form in soup.find_all("form", action=True):
            absu = urljoin(url, form["action"])
            if absu.lower().startswith("http://") and ctx.mark_seen("mixed", netloc, absu):
                ctx.db.add_finding(netloc, "mixed_content", absu,
                                   "<form> posts over http on an https page", url)
                print(f"  [MIXED CONTENT] form -> {absu} on {url}")

    for tag in soup.find_all("script", src=True):
        absu = urljoin(url, tag["src"])
        host = normalize_netloc(urlparse(absu).netloc)
        if host and host != page_host and ctx.mark_seen("tps", netloc, host):
            ctx.db.add_finding(netloc, "third_party_script", absu,
                               f"external script host: {host}", url)
            print(f"  [3RD-PARTY JS] {host} on {url}")

    for tag in soup.find_all("a", href=True):
        if (tag.get("target") or "").lower() != "_blank":
            continue
        rel = " ".join(tag.get("rel", [])).lower()
        if "noopener" in rel or "noreferrer" in rel:
            continue
        absu = urljoin(url, tag["href"])
        host = normalize_netloc(urlparse(absu).netloc)
        if host and host != page_host and ctx.mark_seen("tab", netloc, host):
            ctx.db.add_finding(netloc, "tabnabbing", absu,
                               "target=_blank without rel=noopener (cross-origin)", url)
            print(f"  [TABNAB] {absu} on {url}")


def process_probe(ctx, url, source):
    """Active check (opt-in): request a path the site never linked to and
    flag it if it's actually reachable."""
    netloc = site_key(url)
    result = check_only(ctx, url)
    ctx.bump_progress()
    if isinstance(result, Exception):
        return
    status = result.status_code
    if status in RATE_LIMIT_CODES:
        return
    if status < 400:
        ctx.db.add_finding(netloc, "exposed_path", url, f"{status} reachable", source)
        print(f"  [EXPOSED PATH] {status} {url}")
    elif status in (401, 403):
        ctx.db.add_finding(netloc, "exposed_path", url, f"{status} present but protected", source)
        print(f"  [EXPOSED PATH] {status} {url}")


# --------------------------------------------------------------------------
# Lightweight status-only check (resources + external links)
# --------------------------------------------------------------------------

def check_only(ctx, url):
    """HEAD first, falling back to a streamed GET closed without reading
    the body. Used for resources (images/css/iframes/external scripts)
    and external links: we want a status code, not their contents, and
    we're never going to crawl them further. Honours per-host backoff and
    retries on 429/503."""
    host = normalize_netloc(urlparse(url).netloc)
    proxies = ctx.get_proxies()
    session = ctx.session()
    attempts = ctx.max_retries + 1
    resp = None
    for attempt in range(attempts):
        ctx.throttle.wait(host)
        time.sleep(random.uniform(ctx.delay_min, ctx.delay_max))
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            resp = session.head(url, headers=headers, timeout=ctx.timeout,
                                proxies=proxies, allow_redirects=True)
            if resp.status_code not in RATE_LIMIT_CODES and (
                    resp.status_code in (403, 405, 501) or resp.status_code >= 500):
                resp = session.get(url, headers=headers, timeout=ctx.timeout,
                                   proxies=proxies, allow_redirects=True, stream=True)
                resp.close()
        except Exception as exc:
            return exc
        if resp.status_code in RATE_LIMIT_CODES and attempt < attempts - 1:
            cooldown = ctx.throttle.penalize(host, parse_retry_after(resp))
            print(f"  [RATE LIMIT] {resp.status_code} on {host} - backing off {cooldown:.1f}s")
            continue
        ctx.throttle.relax(host)
        break
    return resp


def process_check_only(ctx, url, source, kind):
    # Findings are attributed to the site that *referenced* the link,
    # not the (often third-party) domain the link points to.
    netloc = site_key(source)
    result = check_only(ctx, url)
    ctx.bump_progress()

    if isinstance(result, Exception):
        ctx.db.add_finding(netloc, "broken_resource", url, f"ERROR: {result}", source, kind)
        print(f"  [ERROR] ({kind}) {url} -> {result}")
        return

    status = result.status_code
    if status in RATE_LIMIT_CODES:
        ctx.db.add_finding(netloc, "rate_limited", url,
                           f"{status} after retries (could not verify)", source)
        print(f"  [RATE LIMITED] ({kind}) {url}")
        return
    if status >= 400:
        ctx.db.add_finding(netloc, "broken_resource", url, status, source, kind)
        print(f"  [{status}] ({kind}) {url}")

    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(LEGACY_EXTENSIONS):
        ctx.db.add_finding(netloc, "legacy_file", url, status, source)
        print(f"  [LEGACY FILE] {url}")
    verdict = sensitive_verdict(url)
    if verdict:
        detail = status if verdict == "high" else f"{status} (dump-like filename)"
        ctx.db.add_finding(netloc, "sensitive_file", url, detail, source)
        print(f"  [SENSITIVE FILE] ({verdict}) {url}")


# --------------------------------------------------------------------------
# Core per-page processing (full crawl)
# --------------------------------------------------------------------------

def extract_links(ctx, url, soup):
    """Returns (page_links, resource_links). page_links are <a href> and
    same-scope <script src> URLs meant to be fully crawled. resource_links
    are (url, kind) pairs - <img>, <link rel=stylesheet|icon>, <iframe>,
    and out-of-scope <a>/<script> - meant for a status check only."""
    page_links = set()
    resource_links = []

    for tag in soup.find_all("a", href=True):
        link = urljoin(url, tag["href"]).split("#")[0]
        if not link.startswith(("http://", "https://")):
            continue
        if ctx.allow_external or normalize_netloc(urlparse(link).netloc) in ctx.seed_netlocs:
            page_links.add(link)
        else:
            resource_links.append((link, "external link"))

    for tag in soup.find_all("script", src=True):
        link = urljoin(url, tag["src"]).split("#")[0]
        if not link.startswith(("http://", "https://")):
            continue
        if ctx.allow_external or normalize_netloc(urlparse(link).netloc) in ctx.seed_netlocs:
            page_links.add(link)  # fetched fully so it can be secret-scanned
        else:
            resource_links.append((link, "script"))

    for tag in soup.find_all("img", src=True):
        link = urljoin(url, tag["src"]).split("#")[0]
        if link.startswith(("http://", "https://")):
            resource_links.append((link, "image"))

    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel", [])).lower()
        if "stylesheet" in rel or "icon" in rel:
            link = urljoin(url, tag["href"]).split("#")[0]
            if link.startswith(("http://", "https://")):
                resource_links.append((link, "stylesheet"))

    for tag in soup.find_all("iframe", src=True):
        link = urljoin(url, tag["src"]).split("#")[0]
        if link.startswith(("http://", "https://")):
            resource_links.append((link, "iframe"))

    return page_links, resource_links


def process_url(ctx, url, found_on, depth):
    netloc = site_key(url)

    if not ctx.robots.can_fetch(url):
        ctx.db.incr_stat(netloc, "robots_blocked")
        print(f"  [ROBOTS] blocked by robots.txt: {url}")
        ctx.bump_progress()
        return

    host = normalize_netloc(urlparse(url).netloc)
    proxies = ctx.get_proxies()
    attempts = ctx.max_retries + 1
    resp = None
    for attempt in range(attempts):
        ctx.throttle.wait(host)
        time.sleep(random.uniform(ctx.delay_min, ctx.delay_max))
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            resp = ctx.session().get(
                url, headers=headers, timeout=ctx.timeout,
                proxies=proxies, allow_redirects=True,
            )
        except Exception as exc:
            ctx.db.add_finding(netloc, "broken", url, f"ERROR: {exc}", found_on)
            print(f"  [ERROR] {url} -> {exc}")
            ctx.bump_progress()
            return
        if resp.status_code in RATE_LIMIT_CODES and attempt < attempts - 1:
            cooldown = ctx.throttle.penalize(host, parse_retry_after(resp))
            print(f"  [RATE LIMIT] {resp.status_code} on {host} - backing off {cooldown:.1f}s")
            continue
        ctx.throttle.relax(host)
        break

    status = resp.status_code
    ctx.db.incr_stat(netloc, "pages_crawled")

    if status in RATE_LIMIT_CODES:
        # Still throttled after every retry - flag it as "couldn't verify",
        # never as a broken link, and don't parse the throttle page.
        ctx.db.add_finding(netloc, "rate_limited", url,
                           f"{status} after {attempts} tries (could not verify)", found_on)
        print(f"  [RATE LIMITED] {url}")
        ctx.bump_progress()
        return

    if status == 404 or status == 410:
        ctx.db.add_finding(netloc, "broken", url, status, found_on)
        print(f"  [{status}] {url}")
    elif status in SECURITY_STATUS_CODES:
        ctx.db.add_finding(netloc, "security_code", url, status, found_on)
        print(f"  [{status}] (security-watch) {url}")
    elif status >= 400:
        ctx.db.add_finding(netloc, "broken", url, status, found_on)
        print(f"  [{status}] {url}")

    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(LEGACY_EXTENSIONS):
        ctx.db.add_finding(netloc, "legacy_file", url, status, found_on)
        print(f"  [LEGACY FILE] {url}")
    verdict = sensitive_verdict(url)
    if verdict:
        detail = status if verdict == "high" else f"{status} (dump-like filename)"
        ctx.db.add_finding(netloc, "sensitive_file", url, detail, found_on)
        print(f"  [SENSITIVE FILE] ({verdict}) {url}")

    for reason in analyze_params(url):
        ctx.db.add_finding(netloc, "risky_param", url, reason, found_on)
        print(f"  [RISKY PARAM] {url} -> {reason}")

    ctx.bump_progress()

    if status >= 400:
        return

    check_response_extras(ctx, resp, url, netloc, depth)
    if depth == 0:
        check_security_headers(resp, url, netloc, ctx.db)

    content_type = resp.headers.get("Content-Type", "")
    is_html = "html" in content_type
    is_js = path_lower.endswith(".js") or "javascript" in content_type

    if not (is_html or is_js):
        return

    text = resp.text
    scan_for_secrets(text, url, netloc, ctx.db)

    if not is_html:
        return

    if DIR_LISTING_RE.search(text):
        ctx.db.add_finding(netloc, "directory_listing", url, status, found_on)
        print(f"  [DIR LISTING] {url}")

    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return

    analyze_html_extras(ctx, url, soup, netloc)

    page_links, resource_links = extract_links(ctx, url, soup)

    for link in page_links:
        ctx.enqueue("page", link, url, depth + 1, respect_domain=True)
    for link, kind in resource_links:
        ctx.enqueue(kind, link, url, depth + 1, respect_domain=False)


def worker(ctx):
    db = ctx.db
    while not ctx.stop_event.is_set():
        row = db.claim()
        if row is None:
            # Nothing to claim. If nothing is pending AND nothing is
            # in-flight anywhere, the crawl is genuinely finished. An
            # in-progress row means another worker may still enqueue
            # children (it marks its row done only after enqueuing), so
            # we wait rather than exit early.
            pending, active = db.pending_and_active()
            if pending == 0 and active == 0:
                return
            ctx.stop_event.wait(0.2)
            continue
        urlhash, kind, url, source, depth = row
        try:
            if kind == "page":
                process_url(ctx, url, source, depth)
            elif kind == "probe":
                process_probe(ctx, url, source)
            else:
                process_check_only(ctx, url, source, kind)
        except Exception as exc:
            print(f"  [WORKER ERROR] {url} -> {exc}")
        finally:
            db.mark_done(urlhash)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _write_section(f, title, rows, formatter):
    f.write(f"{title} ({len(rows)})\n")
    f.write("-" * 70 + "\n")
    if rows:
        for row in rows:
            f.write(formatter(row))
    else:
        f.write("None found.\n")
    f.write("\n")


def write_report(findings, elapsed, outdir, args, partial=False):
    name = site_name(findings.netloc)
    path = Path(outdir) / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        f.write(f"Deep Dive Audit Report - {findings.netloc}\n")
        if partial:
            f.write("*** PARTIAL / IN-PROGRESS CHECKPOINT - crawl was still running when this was written ***\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"Pages crawled: {findings.pages_crawled}\n")
        f.write(f"Robots.txt blocked: {findings.robots_blocked}"
                f"{' (robots.txt ignored)' if args.ignore_robots else ''}\n")
        if args.max_per_path_prefix:
            f.write(f"Skipped by path-prefix limit (--max-per-path-prefix {args.max_per_path_prefix}): "
                    f"{findings.pattern_skipped}\n")
        if args.max_query_variants:
            f.write(f"Skipped by query-variant limit (--max-query-variants {args.max_query_variants}): "
                    f"{findings.query_skipped}\n")
        f.write(f"Duration: {elapsed:.1f}s\n")
        f.write("=" * 70 + "\n\n")

        _write_section(
            f, "BROKEN LINKS (404/410/other errors)", findings.broken,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "BROKEN RESOURCES (images/css/iframes/scripts/external links)", findings.broken_resources,
            lambda r: f"[{r[1]}] ({r[3]}) {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "SECURITY-WATCH STATUS CODES (401/403/5xx)", findings.security_codes,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "EXPOSED PATHS (probed - .git/.env/backups/etc.)", findings.exposed_paths,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "RATE-LIMITED / COULD NOT VERIFY (429/503 after retries)", findings.rate_limited,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "DIRECTORY LISTINGS EXPOSED", findings.directory_listings,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "MIXED CONTENT (http resources on an https page)", findings.mixed_content,
            lambda r: f"{r[0]}\n    {r[1]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "COOKIES MISSING SECURITY FLAGS", findings.cookie_flags,
            lambda r: f"{r[1]}\n    seen on: {r[2]}\n",
        )

        f.write(f"MISSING SECURITY HEADERS (checked once, against {findings.header_checked_url or 'n/a'})\n")
        f.write("-" * 70 + "\n")
        if findings.missing_security_headers:
            for h in findings.missing_security_headers:
                f.write(f"{h}\n")
        elif findings.header_checked_url:
            f.write("None missing.\n")
        else:
            f.write("Not checked (no successful page load).\n")
        f.write("\n")

        _write_section(
            f, "LEGACY/INACCESSIBLE FILE TYPES (pdf/doc/ppt/xls)", findings.legacy_files,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "SENSITIVE/DATA-EXPOSURE FILE TYPES", findings.sensitive_files,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "URLS WITH SUSPICIOUS PARAMETER VALUES (redirect/SSRF/traversal/debug)", findings.risky_params,
            lambda r: f"{r[0]}\n    {'; '.join(r[1])}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "POSSIBLE LEAKED API KEYS / CREDENTIALS", findings.secrets,
            lambda r: f"[{r[1]}] {r[2]}\n    found on: {r[0]}\n",
        )
        _write_section(
            f, "PUBLIC / LOW-CONFIDENCE KEYS (informational - meant to be public)", findings.secrets_info,
            lambda r: f"[{r[1]}] {r[2]}\n    found on: {r[0]}\n",
        )
        _write_section(
            f, "VERSION-DISCLOSURE HEADERS (informational)", findings.version_disclosure,
            lambda r: f"{r[1]}\n    on: {r[0]}\n",
        )
        _write_section(
            f, "THIRD-PARTY SCRIPT HOSTS (informational - supply-chain surface)", findings.third_party_scripts,
            lambda r: f"{r[1]}\n    e.g. {r[0]}\n    seen on: {r[2]}\n",
        )
        _write_section(
            f, "target=_blank WITHOUT rel=noopener (informational - tabnabbing)", findings.tabnabbing,
            lambda r: f"{r[0]}\n    found on: {r[2]}\n",
        )

    return path


def build_rows(findings):
    rows = []
    for url, status, found_on in findings.broken:
        rows.append(make_row("Broken Link", status, url, found_on))
    for url, status, found_on, kind in findings.broken_resources:
        rows.append(make_row(f"Broken Resource ({kind})", status, url, found_on))
    for url, status, found_on in findings.security_codes:
        rows.append(make_row("Security Watch", status, url, found_on))
    for url, detail, found_on in findings.exposed_paths:
        rows.append(make_row("Exposed Path", detail, url, found_on))
    for url, detail, found_on in findings.rate_limited:
        rows.append(make_row("Rate Limited", detail, url, found_on))
    for url, status, found_on in findings.directory_listings:
        rows.append(make_row("Directory Listing", status, url, found_on))
    for url, detail, found_on in findings.mixed_content:
        rows.append(make_row("Mixed Content", detail, url, found_on))
    for url, detail, found_on in findings.cookie_flags:
        rows.append(make_row("Cookie Flag", detail, url, found_on))
    for header in findings.missing_security_headers:
        rows.append(make_row("Missing Security Header", header, findings.header_checked_url))
    for url, status, found_on in findings.legacy_files:
        rows.append(make_row("Legacy File", status, url, found_on))
    for url, status, found_on in findings.sensitive_files:
        rows.append(make_row("Sensitive File", status, url, found_on))
    for url, reasons, found_on in findings.risky_params:
        rows.append(make_row("Risky Parameter", "; ".join(reasons), url, found_on))
    for url, label, masked in findings.secrets:
        rows.append(make_row("Possible Secret", f"{label}: {masked}", url))
    for url, label, masked in findings.secrets_info:
        rows.append(make_row("Public/Low-Confidence Key", f"{label}: {masked}", url))
    for url, detail, found_on in findings.version_disclosure:
        rows.append(make_row("Version Disclosure", detail, url, found_on))
    for url, detail, found_on in findings.third_party_scripts:
        rows.append(make_row("Third-Party Script", detail, url, found_on))
    for url, detail, found_on in findings.tabnabbing:
        rows.append(make_row("Tabnabbing Link", detail, url, found_on))
    return rows


def write_html(findings, elapsed, outdir, args, partial=False):
    rows = build_rows(findings)
    summary_lines = [
        ("Pages crawled", str(findings.pages_crawled)),
        ("Robots.txt blocked", str(findings.robots_blocked)),
    ]
    if args.max_per_path_prefix:
        summary_lines.append(("Skipped by path-prefix limit", str(findings.pattern_skipped)))
    if args.max_query_variants:
        summary_lines.append(("Skipped by query-variant limit", str(findings.query_skipped)))
    meta = {
        "title": f"Deep Dive Audit Report - {findings.netloc}",
        "subtitle": f"Generated {datetime.now().isoformat(timespec='seconds')} · {elapsed:.1f}s",
        "partial": partial,
        "summary_lines": summary_lines,
    }
    return write_html_report(rows, meta, outdir, site_name(findings.netloc))


def write_all_reports(db, elapsed, args, partial=False):
    netlocs = db.netlocs_with_data()
    for netloc in netlocs:
        findings = load_findings(db, netloc)
        write_report(findings, elapsed, args.output_dir, args, partial=partial)
        write_html(findings, elapsed, args.output_dir, args, partial=partial)
    return netlocs


def checkpoint_writer(db, args, started, interval, stop_event):
    """Periodically flush the current DB state to .txt/.html reports so a
    crash leaves usable reports, not just a database. (The DB itself is
    always durable; this is for the human-readable deliverable.)"""
    while not stop_event.wait(interval):
        try:
            netlocs = write_all_reports(db, time.time() - started, args, partial=True)
        except Exception as exc:
            print(f"  [CHECKPOINT ERROR] {exc}")
            continue
        if netlocs:
            print(f"  [CHECKPOINT] wrote {len(netlocs)} report(s) to {args.output_dir}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def normalize_seed(url):
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def scope_signature(args, seeds):
    return json.dumps(
        {"seeds": sorted(seeds), "allow_external": bool(args.allow_external),
         "probe": bool(args.probe_common_paths), "schema": SCHEMA_VERSION},
        sort_keys=True,
    )


def default_state_path(output_dir, seeds):
    h = hashlib.sha1("\n".join(sorted(seeds)).encode("utf-8")).hexdigest()[:12]
    return Path(output_dir) / ".crawl_state" / f"audit_{h}.db"


def open_state(args, seeds, seed_netlocs):
    """Open (and if needed seed) the SQLite crawl state, deciding between
    a fresh crawl and resuming an interrupted one. Returns (db, resuming)."""
    state_path = Path(args.state_db) if args.state_db else default_state_path(args.output_dir, seeds)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    db = CrawlState(state_path)
    db.max_pages = args.max_pages
    sig = scope_signature(args, seeds)

    existing_sig = db.get_meta("scope")
    completed = db.get_meta("completed_at")
    by_state = db.counts_by_state()
    has_unfinished = (by_state[PENDING] + by_state[IN_PROGRESS]) > 0

    resuming = False
    if args.fresh:
        db.reset()
    elif existing_sig is None:
        pass  # brand-new database
    elif existing_sig != sig:
        print(f"ERROR: state db {state_path} was built for a different set of "
              f"seeds/scope.\n       Use --fresh to overwrite it, or --state-db "
              f"PATH to keep a separate file for this crawl.")
        db.close()
        sys.exit(1)
    elif completed:
        print(f"A previous crawl for this scope completed on {completed}; starting fresh.")
        db.reset()
    elif has_unfinished:
        resuming = True

    db.set_meta("scope", sig)

    if resuming:
        stale = db.requeue_in_progress()
        pending = db.counts_by_state()[PENDING]
        print(f"Resuming from {state_path}")
        print(f"  {pending} URLs still pending "
              f"({stale} reset from the interrupted run, "
              f"{by_state[2]} already done).")
    else:
        db.set_meta("started_at", datetime.now().isoformat(timespec="seconds"))
        for seed in seeds:
            db.enqueue("page", seed, "(seed)", 0)
        if args.probe_common_paths:
            roots = set()
            for seed in seeds:
                p = urlparse(seed)
                roots.add(f"{p.scheme}://{p.netloc}")
            probes = 0
            for root in sorted(roots):
                for probe_path in COMMON_PROBE_PATHS:
                    if db.enqueue("probe", root + probe_path, "(probe)", 0):
                        probes += 1
            print(f"Probing {probes} common sensitive paths across {len(roots)} host(s) (--probe-common-paths)")
        print(f"State file: {state_path}")

    return db, resuming


def main():
    parser = argparse.ArgumentParser(description="Deep Dive threaded multi-site auditor.")
    parser.add_argument("urls", nargs="+", help="One or more starting URLs (one report per site)")
    parser.add_argument("--threads", type=int, default=8, help="Number of worker threads (default 8)")
    parser.add_argument("--delay-min", type=float, default=0.1, help="Min seconds between requests per thread (default 0.1)")
    parser.add_argument("--delay-max", type=float, default=0.6, help="Max seconds between requests per thread (default 0.6)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=2,
                         help="Times to retry a URL that answers 429/503 (rate-limited) before giving up, "
                              "with growing per-host backoff (default 2). Rate-limited URLs are reported as "
                              "'could not verify', never as broken links.")
    parser.add_argument("--max-pages", type=int, default=5000,
                         help="Safety cap on total URLs discovered across all sites, pages and resources combined "
                              "(0 = unlimited, default 5000)")
    parser.add_argument("--ignore-robots", action="store_true", help="Ignore robots.txt disallow rules")
    parser.add_argument("--allow-external", action="store_true",
                         help="Follow links outside the starting domain(s) instead of treating each seed as its own site")
    parser.add_argument("--proxy", default=None, help="Single proxy URL, e.g. http://user:pass@host:port")
    parser.add_argument("--proxy-file", default=None, help="File with one proxy URL per line, rotated randomly per request")
    parser.add_argument("--output-dir", default="reports", help="Directory to write report files into (default: ./reports)")
    parser.add_argument("--checkpoint-interval", type=float, default=120,
                         help="Seconds between periodic report checkpoints during the crawl, so a crash or a "
                              "killed session doesn't lose everything (0 disables, default 120)")
    parser.add_argument("--max-per-path-prefix", type=int, default=0,
                         help="Cap on how many pages get fully crawled under the same path prefix, e.g. "
                              "/bids/... with years of near-identical postings (0 = unlimited/disabled). "
                              "Pages beyond the cap are skipped, not fetched - resources/links on pages "
                              "already crawled are still checked normally.")
    parser.add_argument("--path-prefix-depth", type=int, default=1,
                         help="Number of leading path segments that define a bucket for "
                              "--max-per-path-prefix (default 1, e.g. '/bids' regardless of what follows it)")
    parser.add_argument("--max-depth", type=int, default=0,
                         help="Max link distance from a seed to crawl (0 = unlimited). A seed is depth 0, "
                              "links found on it depth 1, and so on - a cheap cap on how deep a templated "
                              "or paginated tree is followed.")
    parser.add_argument("--max-query-variants", type=int, default=0,
                         help="Max distinct query-string variants to crawl per path (0 = unlimited/disabled). "
                              "Defuses calendar and faceted-search traps like /events?date=... or "
                              "/search?color=&size=&sort=... that otherwise spin out endless near-identical URLs.")
    parser.add_argument("--state-db", default=None,
                         help="Path to the SQLite crawl-state file (default: <output-dir>/.crawl_state/audit_<hash>.db). "
                              "The frontier + findings live here so a crash can be resumed and memory stays bounded.")
    parser.add_argument("--fresh", action="store_true",
                         help="Ignore any saved state for this scope and start the crawl over from scratch")
    parser.add_argument("--probe-common-paths", action="store_true",
                         help="Also actively request a small fixed list of paths that shouldn't be public "
                              "(.git/HEAD, .env, backup archives, etc.) even if nothing links to them. This is "
                              "the one check that fetches URLs the site never advertised - off by default.")
    args = parser.parse_args()

    seeds = [canonicalize_url(normalize_seed(u)) for u in args.urls]
    seed_netlocs = {normalize_netloc(urlparse(u).netloc) for u in seeds}

    db, resuming = open_state(args, seeds, seed_netlocs)
    ctx = Context(args, seed_netlocs, db)

    print(f"Starting deep audit of {len(seeds)} site(s) with {args.threads} threads...")
    print(f"Seeds: {', '.join(seeds)}")
    if args.allow_external:
        print("Crawl scope: unrestricted (--allow-external set)")
    else:
        print(f"Crawl scope: restricted to {', '.join(sorted(seed_netlocs))}")

    started = time.time()
    threads = [threading.Thread(target=worker, args=(ctx,), daemon=True) for _ in range(args.threads)]
    for t in threads:
        t.start()

    checkpoint_stop = threading.Event()
    checkpoint_thread = None
    if args.checkpoint_interval:
        checkpoint_thread = threading.Thread(
            target=checkpoint_writer,
            args=(db, args, started, args.checkpoint_interval, checkpoint_stop),
            daemon=True,
        )
        checkpoint_thread.start()
        print(f"Checkpointing partial reports every {args.checkpoint_interval:.0f}s to {args.output_dir}")

    interrupted = False
    try:
        while True:
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                break
            for t in alive:
                t.join(timeout=0.3)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted - progress is saved to the state file; rerun the same "
              "command to resume. Writing reports for progress so far...")
        ctx.stop_event.set()
    finally:
        ctx.stop_event.set()
        for t in threads:
            t.join(timeout=3)
        checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=3)

    elapsed = time.time() - started
    by_state = db.counts_by_state()
    finished = (not interrupted) and by_state[PENDING] == 0 and by_state[IN_PROGRESS] == 0
    if finished:
        db.set_meta("completed_at", datetime.now().isoformat(timespec="seconds"))

    print()
    print(f"Crawl {'finished' if finished else 'stopped'} in {elapsed:.1f}s. Writing reports...")
    netlocs = db.netlocs_with_data()
    for netloc in netlocs:
        findings = load_findings(db, netloc)
        report_path = write_report(findings, elapsed, args.output_dir, args)
        html_path = write_html(findings, elapsed, args.output_dir, args)
        print(f"  {netloc}: {findings.pages_crawled} pages -> {report_path} / {html_path}")
        print(f"     {len(findings.broken)} broken, {len(findings.broken_resources)} broken resources, "
              f"{len(findings.security_codes)} security-watch, {len(findings.exposed_paths)} exposed paths, "
              f"{len(findings.rate_limited)} rate-limited, {len(findings.directory_listings)} dir listings, "
              f"{len(findings.legacy_files)} legacy, {len(findings.sensitive_files)} sensitive files, "
              f"{len(findings.risky_params)} risky params, {len(findings.secrets)} secrets "
              f"({len(findings.secrets_info)} public/low-conf)")
        print(f"     {len(findings.mixed_content)} mixed-content, {len(findings.cookie_flags)} cookie-flag, "
              f"{len(findings.version_disclosure)} version-disclosure, "
              f"{len(findings.third_party_scripts)} 3rd-party-script hosts, {len(findings.tabnabbing)} tabnab; "
              f"{findings.pattern_skipped} path-prefix / {findings.query_skipped} query-variant skips")

    if not finished and not interrupted:
        print("\nNote: crawl stopped with work still pending (likely --max-pages). "
              "Rerun the same command to continue, or --fresh to restart.")
    db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
