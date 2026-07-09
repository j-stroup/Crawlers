#!/usr/bin/env python3
"""Quick n Dirty - single-domain broken-link and legacy-file scanner.

Crawls one site (pages never leave the domain), single-threaded with a
small randomized delay between requests. <a href> links within the
domain get fully crawled; external <a href> links and any
<img>/<link rel=stylesheet|icon>/<iframe>/<script> resource on a page
(wherever they point) get a status check but are never crawled further.
Flags any response >= 400 and any link pointing at a .pdf/.txt/.doc/.docx
file. Writes a plain-text report to {site_name}.txt

Usage:
    python quick_scan.py https://example.com
    python quick_scan.py https://example.com --delay-min 0.5 --delay-max 1.5
"""

import argparse
import random
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from report_html import make_row, write_html_report

FLAGGED_EXTENSIONS = (".pdf", ".txt", ".doc", ".docx")
TIMEOUT = 10


def normalize_netloc(netloc):
    """'www.example.com' and 'example.com' are the same site - strip a
    leading 'www.' before comparing so the crawler doesn't treat one as
    external just because a link happens to use the other."""
    netloc = netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def same_domain(url, netloc):
    return normalize_netloc(urlparse(url).netloc) == normalize_netloc(netloc)


def site_name(start_url):
    return urlparse(start_url).netloc.replace(":", "_") or "site"


def fetch(session, url):
    """Full GET - used for pages we're actually going to crawl."""
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        return session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except Exception as exc:
        return exc


def check_only(session, url):
    """Lightweight status check - HEAD first, falling back to a streamed
    GET that's closed without reading the body. Used for resources
    (images/css/iframes/scripts) and external links: we want a status
    code, not their contents, and we're never going to crawl them."""
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = session.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
            resp = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True)
            resp.close()
        return resp
    except Exception as exc:
        return exc


def extract_links(url, soup):
    """Returns (page_links, resource_links) found on an HTML page.

    page_links: absolute http(s) URLs from <a href>.
    resource_links: (url, kind) pairs from <img>, <link rel=stylesheet|
    icon>, <iframe>, and <script> - checked for a status code but never
    parsed for further links.
    """
    page_links = set()
    for tag in soup.find_all("a", href=True):
        link = urljoin(url, tag["href"]).split("#")[0]
        if link.startswith(("http://", "https://")):
            page_links.add(link)

    resource_links = []
    for tag in soup.find_all("img", src=True):
        resource_links.append((urljoin(url, tag["src"]).split("#")[0], "image"))
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel", [])).lower()
        if "stylesheet" in rel or "icon" in rel:
            resource_links.append((urljoin(url, tag["href"]).split("#")[0], "stylesheet"))
    for tag in soup.find_all("iframe", src=True):
        resource_links.append((urljoin(url, tag["src"]).split("#")[0], "iframe"))
    for tag in soup.find_all("script", src=True):
        resource_links.append((urljoin(url, tag["src"]).split("#")[0], "script"))

    resource_links = [(u, k) for u, k in resource_links if u.startswith(("http://", "https://"))]
    return page_links, resource_links


def crawl(start_url, delay_min, delay_max, max_pages):
    netloc = urlparse(start_url).netloc
    session = requests.Session()

    visited_pages = set()   # same-domain pages fully crawled
    checked = set()         # any URL already status-checked (page, resource, or external link)
    found_on = {start_url: "(seed)"}
    queue = deque([start_url])

    broken = []    # (url, status_or_error, found_on, kind)
    flagged = []   # (url, status, found_on, kind)

    def record(url, status, source, kind):
        bad = (not isinstance(status, int)) or status >= 400
        if bad:
            broken.append((url, status, source, kind))
            print(f"  [{status}] ({kind}) {url}")
        if urlparse(url).path.lower().endswith(FLAGGED_EXTENSIONS):
            flagged.append((url, status, source, kind))
            if not bad:
                print(f"  [FILE] ({kind}) {url}")

    def check_and_record(url, source, kind):
        if url in checked:
            return
        checked.add(url)
        result = check_only(session, url)
        time.sleep(random.uniform(delay_min, delay_max))
        if isinstance(result, Exception):
            record(url, f"ERROR: {result}", source, kind)
        else:
            record(url, result.status_code, source, kind)

    while queue:
        if max_pages and len(visited_pages) >= max_pages:
            print(f"Reached --max-pages limit ({max_pages}), stopping.")
            break

        url = queue.popleft()
        if url in visited_pages:
            continue
        visited_pages.add(url)
        checked.add(url)

        result = fetch(session, url)
        time.sleep(random.uniform(delay_min, delay_max))

        if isinstance(result, Exception):
            record(url, f"ERROR: {result}", found_on.get(url, "?"), "page")
            continue

        status = result.status_code
        record(url, status, found_on.get(url, "?"), "page")

        path_lower = urlparse(url).path.lower()
        if status >= 400 or path_lower.endswith(FLAGGED_EXTENSIONS):
            continue

        content_type = result.headers.get("Content-Type", "")
        if "html" not in content_type:
            continue

        try:
            soup = BeautifulSoup(result.text, "html.parser")
        except Exception:
            continue

        page_links, resource_links = extract_links(url, soup)

        for link in page_links:
            if same_domain(link, netloc):
                if link not in found_on:
                    found_on[link] = url
                    queue.append(link)
            else:
                check_and_record(link, url, "external link")

        for link, kind in resource_links:
            check_and_record(link, url, kind)

    return visited_pages, checked, broken, flagged


def write_report(start_url, visited_pages, checked, broken, flagged, elapsed, outdir):
    name = site_name(start_url)
    path = Path(outdir) / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        f.write(f"Quick Scan Report - {start_url}\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"Pages crawled: {len(visited_pages)}\n")
        f.write(f"Links/resources checked: {len(checked)}\n")
        f.write(f"Duration: {elapsed:.1f}s\n")
        f.write("=" * 70 + "\n\n")

        f.write(f"BROKEN LINKS / RESOURCES / ERROR RESPONSES ({len(broken)})\n")
        f.write("-" * 70 + "\n")
        if broken:
            for url, status, found, kind in broken:
                f.write(f"[{status}] ({kind}) {url}\n    found on: {found}\n")
        else:
            f.write("None found.\n")
        f.write("\n")

        f.write(f"FLAGGED FILE TYPES - .pdf/.txt/.doc/.docx ({len(flagged)})\n")
        f.write("-" * 70 + "\n")
        if flagged:
            for url, status, found, kind in flagged:
                f.write(f"[{status}] ({kind}) {url}\n    found on: {found}\n")
        else:
            f.write("None found.\n")

    return path


def build_rows(broken, flagged):
    rows = []
    for url, status, found, kind in broken:
        category = "Broken Link" if kind == "page" else f"Broken Resource ({kind})"
        rows.append(make_row(category, status, url, found))
    for url, status, found, kind in flagged:
        rows.append(make_row(f"Flagged File ({kind})", status, url, found))
    return rows


def write_html(start_url, visited_pages, checked, broken, flagged, elapsed, outdir):
    rows = build_rows(broken, flagged)
    meta = {
        "title": f"Quick Scan Report - {start_url}",
        "subtitle": f"Generated {datetime.now().isoformat(timespec='seconds')} · {elapsed:.1f}s",
        "partial": False,
        "summary_lines": [
            ("Pages crawled", str(len(visited_pages))),
            ("Links/resources checked", str(len(checked))),
            ("Broken", str(len(broken))),
            ("Flagged files", str(len(flagged))),
        ],
    }
    return write_html_report(rows, meta, outdir, site_name(start_url))


def main():
    parser = argparse.ArgumentParser(description="Quick single-domain broken-link/legacy-file scan.")
    parser.add_argument("url", help="Starting URL, e.g. https://example.com")
    parser.add_argument("--delay-min", type=float, default=0.4, help="Min seconds between requests (default 0.4)")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max seconds between requests (default 1.2)")
    parser.add_argument("--max-pages", type=int, default=0, help="Safety cap on pages crawled (0 = unlimited)")
    parser.add_argument("--output-dir", default=".", help="Directory to write the report into (default: current dir)")
    args = parser.parse_args()

    start_url = args.url if args.url.startswith(("http://", "https://")) else f"https://{args.url}"

    print(f"Scanning {start_url} ...")
    started = time.time()
    visited_pages, checked, broken, flagged = crawl(start_url, args.delay_min, args.delay_max, args.max_pages)
    elapsed = time.time() - started

    report_path = write_report(start_url, visited_pages, checked, broken, flagged, elapsed, args.output_dir)
    html_path = write_html(start_url, visited_pages, checked, broken, flagged, elapsed, args.output_dir)

    print()
    print(f"Pages crawled       : {len(visited_pages)}")
    print(f"Links/resources checked: {len(checked)}")
    print(f"Broken links/resources : {len(broken)}")
    print(f"Flagged files        : {len(flagged)}")
    print(f"Report written to {report_path}")
    print(f"Sortable report      : {html_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
