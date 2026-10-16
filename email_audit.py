#!/usr/bin/env python3
"""Email address audit - crawls a list of domains and collects every
email address found on their pages.

Single-threaded with a small randomized delay, same courtesy approach as
quick_scan.py. Only follows links within the domain being scanned -
external links are never fetched. A plain regex pass over each page's
raw HTML catches both visible text addresses and mailto: links in one
step. Writes a combined report plus a flat, deduped list of addresses.

Usage:
    python email_audit.py domains.txt
    python email_audit.py https://example.com https://other-site.com
    python email_audit.py domains.txt --max-pages 300 --output-dir reports
"""

import argparse
import heapq
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from quick_scan import normalize_netloc, same_domain, site_name, extract_links

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Things that match the regex but aren't emails - mostly @2x/@3x srcset
# filenames (logo@2x.png) where the "TLD" is actually a file extension.
FILE_EXT_FALSE_POSITIVES = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico",
    "css", "js", "json", "pdf", "zip",
}


def extract_emails(text):
    found = set()
    for match in EMAIL_RE.findall(text):
        email = match.rstrip(".,;:)>'\"")
        tld = email.rsplit(".", 1)[-1].lower()
        if tld in FILE_EXT_FALSE_POSITIVES:
            continue
        found.add(email.lower())
    return found


def canonicalize(url):
    """Drop the query string and fragment, and normalize the host, before
    a URL is used as a dedup key or queued. Query-string variants (sort
    order, filters, tracking params, calendar/event navigation, ...) are
    the classic crawler-trap source - a "small" site can still generate
    unlimited distinct URLs that way - and for finding emails they almost
    never point at genuinely different content, so it's not worth
    crawling each variant separately."""
    parsed = urlparse(url)
    netloc = normalize_netloc(parsed.netloc)
    path = parsed.path or "/"
    return f"{parsed.scheme}://{netloc}{path}"


# Path keywords for crawl prioritization, not for deciding what to visit
# - every in-domain page still gets crawled eventually. The point is
# ordering: on a huge site that will hit --max-pages long before the
# queue empties, a contact/about page should be crawled within the
# first few requests instead of however many hops deep it happens to be
# linked from, and an endless blog/news/event archive should be the
# first thing sacrificed when the cap hits.
HIGH_VALUE_PATH_HINTS = (
    "contact", "about", "team", "staff", "support", "help",
    "impressum", "imprint", "legal", "privacy", "terms",
    "connect", "reach", "get-in-touch", "who-we-are", "directory", "location",
)
LOW_VALUE_PATH_HINTS = (
    "blog", "news", "press", "event", "article", "product",
    "category", "categories", "tag", "archive",
)


def link_priority(url):
    """Lower sorts first. 0 is reserved for the seed page."""
    path = urlparse(url).path.lower()
    if any(hint in path for hint in HIGH_VALUE_PATH_HINTS):
        return 1
    if any(hint in path for hint in LOW_VALUE_PATH_HINTS):
        return 3
    return 2


def fetch_page(session, url, headers, timeout, max_fetch_seconds):
    """Streamed GET with a hard wall-clock deadline. A plain
    requests timeout isn't enough here: its read-timeout clock resets on
    every chunk received, so a server that trickles bytes slowly enough
    (a slow/overloaded host, or a stalled connection) never trips it and
    the request can hang indefinitely. This caps total time regardless
    of how the response arrives."""
    started = time.monotonic()
    with session.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as resp:
        final_url = resp.url
        status = resp.status_code
        content_type = resp.headers.get("Content-Type", "")
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=8192):
            body.extend(chunk)
            if time.monotonic() - started > max_fetch_seconds:
                raise TimeoutError(f"exceeded {max_fetch_seconds:.0f}s wall-clock fetch limit")
        text = body.decode(resp.encoding or "utf-8", errors="replace")
    return final_url, status, content_type, text


def crawl_domain(start_url, delay_min, delay_max, max_pages, timeout, max_fetch_seconds, on_email=None):
    netloc = urlparse(start_url).netloc
    session = requests.Session()

    seed = canonicalize(start_url)
    visited = set()
    found_on = {seed: "(seed)"}
    seq = 0
    queue = [(0, seq, seed)]  # (priority, insertion order, url) - lowest priority pops first
    emails = {}  # email -> first page URL it was found on

    def record_emails(url, text):
        for email in extract_emails(text):
            if email not in emails:
                emails[email] = url
                if on_email:
                    on_email(email, url)

    while queue:
        if max_pages and len(visited) >= max_pages:
            print(f"  reached --max-pages limit ({max_pages}), stopping {netloc}")
            break

        _priority, _seq, url = heapq.heappop(queue)
        if url in visited:
            continue
        visited.add(url)
        print(f"  [{len(visited)}] {url}")

        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            final_url, status, content_type, text = fetch_page(session, url, headers, timeout, max_fetch_seconds)
        except Exception as exc:
            print(f"  [ERROR] {url}: {exc}")
            time.sleep(random.uniform(delay_min, delay_max))
            continue
        time.sleep(random.uniform(delay_min, delay_max))

        if status >= 400:
            continue
        if "html" not in content_type:
            continue

        record_emails(url, text)

        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception:
            continue

        page_links, _resources = extract_links(final_url, soup)
        for link in page_links:
            link = canonicalize(link)
            if same_domain(link, netloc) and link not in found_on:
                found_on[link] = url
                seq += 1
                heapq.heappush(queue, (link_priority(link), seq, link))

    return visited, emails


def state_path_for(outdir):
    return Path(outdir) / ".email_audit_state.json"


def load_completed_domains(outdir, fresh):
    """Domains a previous run already finished (queue ran dry, or it hit
    --max-pages and moved on - either way there's nothing left to redo).
    Used so a resumed run skips them instead of re-crawling the whole
    list from scratch."""
    path = state_path_for(outdir)
    if fresh or not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("completed_domains", []))
    except Exception:
        return set()


def save_completed_domains(outdir, completed):
    path = state_path_for(outdir)
    path.write_text(json.dumps({"completed_domains": sorted(completed)}, indent=2), encoding="utf-8")


class LiveWriter:
    """Writes both report files as results come in and flushes after
    every write, so an interrupted run (Ctrl+C, crash, closed terminal)
    never loses emails already found - only whatever the current page
    fetch was in the middle of when it stopped.

    In resume mode, appends instead of overwriting, and preloads
    `all_emails` from the existing emails.txt so dedup still holds across
    runs (a re-crawled, previously-interrupted domain won't re-add the
    same address to the flat list, even though its own in-memory record
    of finding it again is brand new this run)."""

    def __init__(self, outdir, resume):
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.report_path = outdir / "email_audit_report.txt"
        self.flat_path = outdir / "emails.txt"

        self.all_emails = set()
        if resume and self.flat_path.is_file():
            for line in self.flat_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.all_emails.add(line)

        mode = "a" if resume else "w"
        self.report_f = self.report_path.open(mode, encoding="utf-8")
        self.flat_f = self.flat_path.open(mode, encoding="utf-8")

        if resume:
            self.report_f.write(f"\n--- resuming at {datetime.now().isoformat(timespec='seconds')} ---\n\n")
        else:
            self.report_f.write("Email Audit Report\n")
            self.report_f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
            self.report_f.write("(written as it goes - safe to read mid-run or after an interruption)\n")
            self.report_f.write("=" * 70 + "\n\n")
        self.report_f.flush()

    def start_domain(self, start_url):
        self.report_f.write(f"{start_url}\n")
        self.report_f.flush()

    def email_found(self, email, found_on_url):
        self.report_f.write(f"    {email}\n        found on: {found_on_url}\n")
        self.report_f.flush()
        if email not in self.all_emails:
            self.all_emails.add(email)
            self.flat_f.write(email + "\n")
            self.flat_f.flush()

    def finish_domain(self, visited_count, email_count):
        self.report_f.write(f"  ({visited_count} page(s) crawled, {email_count} email(s) found)\n\n")
        self.report_f.flush()

    def close(self):
        self.report_f.close()
        self.flat_f.close()


def load_domains(inputs):
    """A single argument that's an existing file is read as one domain
    per line (blank lines and '#' comments ignored); otherwise every
    argument is treated as a domain/URL directly."""
    if len(inputs) == 1:
        candidate = Path(inputs[0])
        if candidate.is_file():
            domains = []
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.append(line)
            return domains
    return inputs


def main():
    parser = argparse.ArgumentParser(
        description="Crawl a list of domains and collect email addresses found on them."
    )
    parser.add_argument("inputs", nargs="+", help="Domains/URLs, or a single file with one domain per line")
    parser.add_argument("--delay-min", type=float, default=0.4, help="Min seconds between requests (default 0.4)")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max seconds between requests (default 1.2)")
    parser.add_argument("--max-pages", type=int, default=150, help="Per-site safety cap on pages crawled (0 = unlimited, default 150)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request connect/read timeout in seconds (default 10)")
    parser.add_argument("--max-fetch-seconds", type=float, default=30, help="Hard wall-clock cap per request regardless of how slowly the server sends data (default 30)")
    parser.add_argument("--output-dir", default=".", help="Directory to write reports into (default: current dir)")
    parser.add_argument("--fresh", action="store_true", help="Ignore any saved progress from an interrupted run and start the whole list over (overwrites existing reports)")
    args = parser.parse_args()

    domains = load_domains(args.inputs)
    completed = load_completed_domains(args.output_dir, args.fresh)
    resume = bool(completed) and not args.fresh
    if resume:
        print(f"Resuming - {len(completed)} domain(s) already completed will be skipped.")

    writer = LiveWriter(args.output_dir, resume=resume)
    started = time.time()
    completed_fully = False

    try:
        for raw in domains:
            start_url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
            canonical_start = canonicalize(start_url)
            if canonical_start in completed:
                print(f"Skipping {start_url} (already completed in a previous run)")
                continue

            print(f"Scanning {start_url} ...")
            writer.start_domain(start_url)
            visited, emails = crawl_domain(
                start_url, args.delay_min, args.delay_max, args.max_pages, args.timeout,
                args.max_fetch_seconds, on_email=writer.email_found,
            )
            writer.finish_domain(len(visited), len(emails))
            print(f"  {len(visited)} page(s) crawled, {len(emails)} email(s) found")
            for email in sorted(emails):
                print(f"    {email}")

            completed.add(canonical_start)
            save_completed_domains(args.output_dir, completed)
        completed_fully = True
    finally:
        writer.close()

    if completed_fully:
        # Nothing left to resume - the whole list finished, so a later
        # run of the same domains.txt is a fresh audit, not a resume.
        state_path_for(args.output_dir).unlink(missing_ok=True)

    elapsed = time.time() - started
    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Total unique emails found: {len(writer.all_emails)}")
    print(f"Full report     : {writer.report_path}")
    print(f"Flat email list : {writer.flat_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
