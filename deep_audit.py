#!/usr/bin/env python3
"""Deep Dive - threaded, multi-site site auditor.

Not a vulnerability scanner (this isn't ZAP) - it's a much more thorough
housekeeping crawl than quick_scan.py: broken links and resources,
response codes worth a second look, exposed directory listings, missing
security headers, file types that shouldn't be publicly exposed, query
parameters worth a second look, and HTML/JS source scanned for obvious
leaked API keys/credentials. Meant to run for hours across one or many
sites in a single session.

<a href> pages within scope (same domain, or anywhere if --allow-external)
get fully crawled. External links (when not --allow-external) and any
<img>/<link rel=stylesheet|icon>/<iframe>/external <script> resource get
a status check but are never crawled further.

Report files are checkpointed periodically during the crawl (not just at
the end), so an hours-long run that gets killed or crashes still leaves
a usable (if slightly stale) report instead of nothing. Path-prefix
bucketing (--max-per-path-prefix) caps how many pages get fully crawled
under the same leading path segment(s), so a giant flat archive (years
of /bids/... postings, thousands of near-identical blog posts) doesn't
eat the whole run on pages that are all going to look the same.

Examples:
    python deep_audit.py https://example.com
    python deep_audit.py https://example.com https://other-site.com --threads 15
    python deep_audit.py https://example.com --allow-external --max-pages 20000
    python deep_audit.py https://example.com --ignore-robots --proxy http://127.0.0.1:8080
    python deep_audit.py https://example.com --max-per-path-prefix 50
"""

import argparse
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from report_html import make_row, write_html_report

# --------------------------------------------------------------------------
# Classification tables. Heuristic by design - the point is "flag it for a
# human to look at", not "this is definitely a vulnerability".
# --------------------------------------------------------------------------

# Legacy/inaccessible document types - the original PDF pet peeve, still
# just as relevant on a deep audit as a quick one.
LEGACY_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")

# File types that have no business being publicly reachable - config,
# database, backup, and data-dump files most often leaked by accident.
SENSITIVE_EXTENSIONS = (
    ".txt", ".db", ".sqlite", ".sqlite3", ".xml", ".json", ".yml", ".yaml",
    ".env", ".log", ".bak", ".backup", ".old", ".swp", ".config", ".ini",
    ".conf", ".csv", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".key",
    ".pem", ".sql", ".dump",
)

# Status codes that aren't a plain 404 but are worth a second look:
# auth-related (401/403/407), or server-side errors that can leak stack
# traces / internals (5xx).
SECURITY_STATUS_CODES = {401, 403, 407, 500, 501, 502, 503, 504, 505, 507, 511}

# Query-string parameter names that commonly show up in IDOR, open
# redirect, path traversal, and debug-mode findings. Noisy by nature -
# treat matches as "worth a glance", not confirmed issues.
RISKY_PARAM_NAMES = {
    "id", "uid", "user", "userid", "admin", "debug", "token", "key",
    "apikey", "api_key", "secret", "password", "pwd", "auth", "session",
    "sessionid", "redirect", "redir", "return", "returnurl", "return_url",
    "next", "url", "uri", "path", "file", "filename", "include",
    "template", "cmd", "exec", "command", "action", "mode", "sql",
    "source", "src", "callback", "dest", "destination",
}

# (label, regex) pairs for scanning HTML/JS bodies for obvious leaked
# credentials. Not exhaustive - just the common, high-signal formats.
SECRET_PATTERNS = [
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,48}")),
    ("Stripe Live Secret Key", re.compile(r"sk_live_[0-9a-zA-Z]{16,}")),
    ("Stripe Live Publishable Key", re.compile(r"pk_live_[0-9a-zA-Z]{16,}")),
    ("Private Key Block", re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PGP)?\s?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Generic API Key/Secret Assignment",
     re.compile(r"(?i)(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token)"
                r"\s*[:=]\s*['\"]([A-Za-z0-9_\-\.]{16,})['\"]")),
    ("Password Assignment",
     re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]")),
]

# Directory-listing "Index of /" pages (Apache mod_autoindex, nginx
# autoindex, classic IIS) - almost always an accidental exposure.
DIR_LISTING_RE = re.compile(
    r"<title>\s*index of\s*/|<h1[^>]*>\s*index of\s*/|directory listing (?:for|--)\s*/",
    re.IGNORECASE,
)

# Baseline response headers worth having; checked once per site against
# the first successfully-loaded page rather than every page, since
# header config is normally set site-wide (web server/CDN), not per-page.
SECURITY_HEADERS = (
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Strict-Transport-Security",
    "Referrer-Policy",
)

DEFAULT_TIMEOUT = 15


def mask(secret):
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def normalize_netloc(netloc):
    """'www.example.com' and 'example.com' are the same site - strip a
    leading 'www.' before comparing/grouping so the crawler doesn't
    treat one as external, or split findings across two reports, just
    because a link happens to use the other."""
    netloc = netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def site_key(url):
    return normalize_netloc(urlparse(url).netloc)


def site_name(netloc):
    return netloc.replace(":", "_") or "site"


# --------------------------------------------------------------------------
# Per-site findings
# --------------------------------------------------------------------------

class SiteFindings:
    def __init__(self, netloc):
        self.netloc = netloc
        self.pages_crawled = 0
        self.robots_blocked = 0
        self.pattern_skipped = 0
        self.broken = []             # (url, status_or_error, found_on)
        self.broken_resources = []   # (url, status_or_error, found_on, kind)
        self.security_codes = []     # (url, status, found_on)
        self.directory_listings = [] # (url, status, found_on)
        self.legacy_files = []       # (url, status, found_on)
        self.sensitive_files = []    # (url, status, found_on)
        self.risky_params = []       # (url, [params], found_on)
        self.secrets = []            # (url, label, masked_value)
        self.header_check_done = False
        self.header_checked_url = None
        self.missing_security_headers = []
        self.lock = threading.Lock()

    def snapshot(self):
        """A point-in-time copy, safe to write to disk while other
        threads keep appending to the live findings (checkpointing)."""
        with self.lock:
            copy = SiteFindings(self.netloc)
            copy.pages_crawled = self.pages_crawled
            copy.robots_blocked = self.robots_blocked
            copy.pattern_skipped = self.pattern_skipped
            copy.broken = list(self.broken)
            copy.broken_resources = list(self.broken_resources)
            copy.security_codes = list(self.security_codes)
            copy.directory_listings = list(self.directory_listings)
            copy.legacy_files = list(self.legacy_files)
            copy.sensitive_files = list(self.sensitive_files)
            copy.risky_params = list(self.risky_params)
            copy.secrets = list(self.secrets)
            copy.header_check_done = self.header_check_done
            copy.header_checked_url = self.header_checked_url
            copy.missing_security_headers = list(self.missing_security_headers)
            return copy


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
# Crawl context shared across worker threads
# --------------------------------------------------------------------------

class Context:
    def __init__(self, args, seed_netlocs):
        self.args = args
        self.seed_netlocs = seed_netlocs
        self.allow_external = args.allow_external
        self.max_pages = args.max_pages
        self.timeout = args.timeout
        self.delay_min = args.delay_min
        self.delay_max = args.delay_max
        self.ignore_robots = args.ignore_robots

        self.robots = RobotsCache(ignore=args.ignore_robots)
        self.queue = Queue()
        self.visited = set()
        self.visited_lock = threading.Lock()
        self.findings = {}
        self.findings_lock = threading.Lock()
        self._local = threading.local()

        self.proxy_list = self._load_proxies(args.proxy, args.proxy_file)
        self.stop_event = threading.Event()
        self.processed_count = 0
        self.count_lock = threading.Lock()

        # Path-prefix bucket cap - keeps a site with a huge flat archive
        # (years of /bids/... postings, thousands of near-identical blog
        # posts, etc.) from eating the whole run on pages that are all
        # going to look the same.
        self.max_per_prefix = args.max_per_path_prefix
        self.prefix_depth = args.path_prefix_depth
        self.prefix_counts = {}
        self.prefix_announced = set()
        self.prefix_lock = threading.Lock()

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

    def findings_for(self, netloc):
        with self.findings_lock:
            f = self.findings.get(netloc)
            if f is None:
                f = SiteFindings(netloc)
                self.findings[netloc] = f
            return f

    def should_enqueue(self, link, respect_domain=True):
        """respect_domain=True is for pages we're going to fully crawl
        (obeys --allow-external / seed-domain scoping, and the
        path-prefix bucket cap). respect_domain=False is for leaf checks
        - resources and external links - which get a status check
        wherever they point, but are never crawled further, so neither
        restriction applies to them."""
        if respect_domain and not self.allow_external:
            if normalize_netloc(urlparse(link).netloc) not in self.seed_netlocs:
                return False
        if respect_domain and not self._allow_by_prefix(link):
            return False
        with self.visited_lock:
            if link in self.visited:
                return False
            if self.max_pages and len(self.visited) >= self.max_pages:
                return False
            self.visited.add(link)
            return True

    def _allow_by_prefix(self, link):
        if not self.max_per_prefix:
            return True
        parsed = urlparse(link)
        segments = [s for s in parsed.path.split("/") if s]
        prefix = "/".join(segments[:self.prefix_depth])
        if not prefix:
            return True  # homepage / root - never bucket-limited
        netloc = normalize_netloc(parsed.netloc)
        key = (netloc, prefix)
        with self.prefix_lock:
            count = self.prefix_counts.get(key, 0)
            if count >= self.max_per_prefix:
                if key not in self.prefix_announced:
                    self.prefix_announced.add(key)
                    print(f"  [PATTERN LIMIT] /{prefix} on {netloc} reached "
                          f"{self.max_per_prefix} pages crawled - skipping further matches")
                findings = self.findings_for(netloc)
                with findings.lock:
                    findings.pattern_skipped += 1
                return False
            self.prefix_counts[key] = count + 1
            return True

    def bump_progress(self):
        with self.count_lock:
            self.processed_count += 1
            n = self.processed_count
        if n % 50 == 0:
            print(f"  ... {n} URLs processed so far")


# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------

def scan_for_secrets(text, url, findings):
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            with findings.lock:
                findings.secrets.append((url, label, mask(value)))
            print(f"  [SECRET?] {label} on {url}")


def check_security_headers(resp, url, findings):
    with findings.lock:
        if findings.header_check_done:
            return
        findings.header_check_done = True
        missing = [h for h in SECURITY_HEADERS if h not in resp.headers]
        findings.missing_security_headers = missing
        findings.header_checked_url = url
    if missing:
        print(f"  [HEADERS] {url} missing: {', '.join(missing)}")


# --------------------------------------------------------------------------
# Lightweight status-only check (resources + external links)
# --------------------------------------------------------------------------

def check_only(ctx, url):
    """HEAD first, falling back to a streamed GET closed without reading
    the body. Used for resources (images/css/iframes/external scripts)
    and external links: we want a status code, not their contents, and
    we're never going to crawl them further."""
    time.sleep(random.uniform(ctx.delay_min, ctx.delay_max))
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    proxies = ctx.get_proxies()
    session = ctx.session()
    try:
        resp = session.head(url, headers=headers, timeout=ctx.timeout, proxies=proxies, allow_redirects=True)
        if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
            resp = session.get(url, headers=headers, timeout=ctx.timeout, proxies=proxies,
                                allow_redirects=True, stream=True)
            resp.close()
        return resp
    except Exception as exc:
        return exc


def process_check_only(ctx, url, source, kind):
    # Findings are attributed to the site that *referenced* the link,
    # not the (often third-party) domain the link points to.
    findings = ctx.findings_for(site_key(source))

    result = check_only(ctx, url)
    ctx.bump_progress()

    if isinstance(result, Exception):
        with findings.lock:
            findings.broken_resources.append((url, f"ERROR: {result}", source, kind))
        print(f"  [ERROR] ({kind}) {url} -> {result}")
        return

    status = result.status_code
    if status >= 400:
        with findings.lock:
            findings.broken_resources.append((url, status, source, kind))
        print(f"  [{status}] ({kind}) {url}")

    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(LEGACY_EXTENSIONS):
        with findings.lock:
            findings.legacy_files.append((url, status, source))
        print(f"  [LEGACY FILE] {url}")
    if path_lower.endswith(SENSITIVE_EXTENSIONS):
        with findings.lock:
            findings.sensitive_files.append((url, status, source))
        print(f"  [SENSITIVE FILE] {url}")


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


def process_url(ctx, url, found_on):
    if not ctx.robots.can_fetch(url):
        findings = ctx.findings_for(site_key(url))
        with findings.lock:
            findings.robots_blocked += 1
        print(f"  [ROBOTS] blocked by robots.txt: {url}")
        ctx.bump_progress()
        return

    time.sleep(random.uniform(ctx.delay_min, ctx.delay_max))
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    proxies = ctx.get_proxies()

    findings = ctx.findings_for(site_key(url))

    try:
        resp = ctx.session().get(
            url, headers=headers, timeout=ctx.timeout,
            proxies=proxies, allow_redirects=True,
        )
    except Exception as exc:
        with findings.lock:
            findings.broken.append((url, f"ERROR: {exc}", found_on))
        print(f"  [ERROR] {url} -> {exc}")
        ctx.bump_progress()
        return

    status = resp.status_code
    with findings.lock:
        findings.pages_crawled += 1

    if status == 404 or status == 410:
        with findings.lock:
            findings.broken.append((url, status, found_on))
        print(f"  [{status}] {url}")
    elif status in SECURITY_STATUS_CODES:
        with findings.lock:
            findings.security_codes.append((url, status, found_on))
        print(f"  [{status}] (security-watch) {url}")
    elif status >= 400:
        with findings.lock:
            findings.broken.append((url, status, found_on))
        print(f"  [{status}] {url}")

    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(LEGACY_EXTENSIONS):
        with findings.lock:
            findings.legacy_files.append((url, status, found_on))
        print(f"  [LEGACY FILE] {url}")
    if path_lower.endswith(SENSITIVE_EXTENSIONS):
        with findings.lock:
            findings.sensitive_files.append((url, status, found_on))
        print(f"  [SENSITIVE FILE] {url}")

    query = urlparse(url).query
    if query:
        params = {p.split("=")[0].lower() for p in query.split("&") if p}
        matched = sorted(params & RISKY_PARAM_NAMES)
        if matched:
            with findings.lock:
                findings.risky_params.append((url, matched, found_on))
            print(f"  [RISKY PARAM] {url} -> {matched}")

    ctx.bump_progress()

    if status >= 400:
        return

    check_security_headers(resp, url, findings)

    content_type = resp.headers.get("Content-Type", "")
    is_html = "html" in content_type
    is_js = path_lower.endswith(".js") or "javascript" in content_type

    if not (is_html or is_js):
        return

    text = resp.text
    scan_for_secrets(text, url, findings)

    if not is_html:
        return

    if DIR_LISTING_RE.search(text):
        with findings.lock:
            findings.directory_listings.append((url, status, found_on))
        print(f"  [DIR LISTING] {url}")

    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return

    page_links, resource_links = extract_links(ctx, url, soup)

    for link in page_links:
        if ctx.should_enqueue(link, respect_domain=True):
            ctx.queue.put(("page", link, url))

    for link, kind in resource_links:
        if ctx.should_enqueue(link, respect_domain=False):
            ctx.queue.put((kind, link, url))


def worker(ctx):
    while not ctx.stop_event.is_set():
        try:
            kind, url, source = ctx.queue.get(timeout=1)
        except Empty:
            continue
        try:
            if kind == "page":
                process_url(ctx, url, source)
            else:
                process_check_only(ctx, url, source, kind)
        except Exception as exc:
            print(f"  [WORKER ERROR] {url} -> {exc}")
        finally:
            ctx.queue.task_done()


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
            f, "DIRECTORY LISTINGS EXPOSED", findings.directory_listings,
            lambda r: f"[{r[1]}] {r[0]}\n    found on: {r[2]}\n",
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
            f, "URLS WITH POSSIBLY RISKY PARAMETERS", findings.risky_params,
            lambda r: f"{r[0]}\n    params: {', '.join(r[1])}\n    found on: {r[2]}\n",
        )
        _write_section(
            f, "POSSIBLE LEAKED API KEYS / CREDENTIALS", findings.secrets,
            lambda r: f"[{r[1]}] {r[2]}\n    found on: {r[0]}\n",
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
    for url, status, found_on in findings.directory_listings:
        rows.append(make_row("Directory Listing", status, url, found_on))
    for header in findings.missing_security_headers:
        rows.append(make_row("Missing Security Header", header, findings.header_checked_url))
    for url, status, found_on in findings.legacy_files:
        rows.append(make_row("Legacy File", status, url, found_on))
    for url, status, found_on in findings.sensitive_files:
        rows.append(make_row("Sensitive File", status, url, found_on))
    for url, params, found_on in findings.risky_params:
        rows.append(make_row("Risky Parameter", ", ".join(params), url, found_on))
    for url, label, masked in findings.secrets:
        rows.append(make_row("Possible Secret", f"{label}: {masked}", url))
    return rows


def write_html(findings, elapsed, outdir, args, partial=False):
    rows = build_rows(findings)
    summary_lines = [
        ("Pages crawled", str(findings.pages_crawled)),
        ("Robots.txt blocked", str(findings.robots_blocked)),
    ]
    if args.max_per_path_prefix:
        summary_lines.append(("Skipped by path-prefix limit", str(findings.pattern_skipped)))
    meta = {
        "title": f"Deep Dive Audit Report - {findings.netloc}",
        "subtitle": f"Generated {datetime.now().isoformat(timespec='seconds')} · {elapsed:.1f}s",
        "partial": partial,
        "summary_lines": summary_lines,
    }
    return write_html_report(rows, meta, outdir, site_name(findings.netloc))


def checkpoint_writer(ctx, args, started, interval, stop_event):
    """Periodically writes every site's current findings to disk so a
    crash, a killed terminal, or a lost remote session after hours of
    crawling doesn't throw away everything - worst case you lose the
    last `interval` seconds of progress, not the whole run."""
    while not stop_event.wait(interval):
        with ctx.findings_lock:
            sites = list(ctx.findings.items())
        if not sites:
            continue
        elapsed = time.time() - started
        for netloc, findings in sites:
            try:
                snap = findings.snapshot()
                write_report(snap, elapsed, args.output_dir, args, partial=True)
                write_html(snap, elapsed, args.output_dir, args, partial=True)
            except Exception as exc:
                print(f"  [CHECKPOINT ERROR] {netloc}: {exc}")
        print(f"  [CHECKPOINT] wrote {len(sites)} report(s) to {args.output_dir}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def normalize_seed(url):
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def main():
    parser = argparse.ArgumentParser(description="Deep Dive threaded multi-site auditor.")
    parser.add_argument("urls", nargs="+", help="One or more starting URLs (one report per site)")
    parser.add_argument("--threads", type=int, default=8, help="Number of worker threads (default 8)")
    parser.add_argument("--delay-min", type=float, default=0.1, help="Min seconds between requests per thread (default 0.1)")
    parser.add_argument("--delay-max", type=float, default=0.6, help="Max seconds between requests per thread (default 0.6)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout in seconds")
    parser.add_argument("--max-pages", type=int, default=5000,
                         help="Safety cap on total URLs processed across all sites, pages and resources combined "
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
    args = parser.parse_args()

    seeds = [normalize_seed(u) for u in args.urls]
    seed_netlocs = {normalize_netloc(urlparse(u).netloc) for u in seeds}

    ctx = Context(args, seed_netlocs)
    for seed in seeds:
        ctx.visited.add(seed)
        ctx.queue.put(("page", seed, "(seed)"))

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
            args=(ctx, args, started, args.checkpoint_interval, checkpoint_stop),
            daemon=True,
        )
        checkpoint_thread.start()
        print(f"Checkpointing partial reports every {args.checkpoint_interval:.0f}s to {args.output_dir}")

    try:
        ctx.queue.join()
    except KeyboardInterrupt:
        print("\nInterrupted - writing reports for progress made so far...")
    except Exception as exc:
        print(f"\nUnexpected error ({exc}) - writing reports for progress made so far...")
    finally:
        ctx.stop_event.set()
        for t in threads:
            t.join(timeout=2)
        checkpoint_stop.set()
        if checkpoint_thread:
            checkpoint_thread.join(timeout=2)

    elapsed = time.time() - started

    print()
    print(f"Crawl finished in {elapsed:.1f}s. Writing reports...")
    for netloc, findings in ctx.findings.items():
        path = write_report(findings, elapsed, args.output_dir, args)
        html_path = write_html(findings, elapsed, args.output_dir, args)
        print(f"  {netloc}: {findings.pages_crawled} pages, "
              f"{len(findings.broken)} broken, {len(findings.broken_resources)} broken resources, "
              f"{len(findings.security_codes)} security-watch, {len(findings.directory_listings)} dir listings, "
              f"{len(findings.legacy_files)} legacy files, {len(findings.sensitive_files)} sensitive files, "
              f"{len(findings.risky_params)} risky params, {len(findings.secrets)} possible secrets, "
              f"{findings.pattern_skipped} skipped by path-prefix limit "
              f"-> {path} / {html_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
