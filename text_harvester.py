#!/usr/bin/env python3
"""Text harvester - wanders outward from a list of seed URLs with no
domain boundary at all, collecting <p> text into an ever-growing corpus
file (intended as Markov chain source material).

Unlike every other tool in this project, this one is NOT scoped to
sites you own - it follows every link it finds, anywhere. Because of
that it respects robots.txt by default (--ignore-robots opts out), and
the only real stopping condition is --max-pages; a --max-pages-per-domain
cap keeps one large site from eating the whole run so the corpus stays
varied instead of becoming "one site's text, mostly."

The corpus file is appended to, not overwritten, across runs - both the
existing corpus (for paragraph-level dedup) and a small visited-URL log
(so a later run doesn't re-fetch pages already harvested) are read back
in at startup. --fresh discards both and starts clean.

Usage:
    python text_harvester.py https://example.com --max-pages 500
    python text_harvester.py seeds.txt --max-pages 2000 --max-pages-per-domain 40
"""

import argparse
import random
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from quick_scan import extract_links
from email_audit import fetch_page

_robots_cache = {}


def _translate_robots_pattern(pattern):
    """Google-style robots.txt pattern -> compiled regex: '*' matches
    any run of characters, a trailing '$' anchors to the end of the
    path, everything else matches literally as a prefix."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


class WildcardRobots:
    """A from-scratch robots.txt parser for just the '*' user-agent
    block, with '*'/'$' wildcard support. Python's own
    urllib.robotparser has NONE of that - it treats '*' as a literal
    character, so a rule as common as "Disallow: /search*" never
    matches a real URL and is silently ignored. For a tool whose whole
    reason to check robots.txt is "we're going everywhere, not just
    our own sites," a parser that quietly no-ops on wildcard rules
    defeats the purpose."""

    def __init__(self, text):
        self.rules = []  # (pattern_regex, allow, specificity)
        applies = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                applies = value == "*"
            elif applies and field in ("allow", "disallow") and value:
                self.rules.append((_translate_robots_pattern(value), field == "allow", len(value)))

    def can_fetch(self, path):
        best = None  # (specificity, allow) of the longest matching rule
        for pattern, allow, specificity in self.rules:
            if (best is None or specificity > best[0]) and pattern.match(path):
                best = (specificity, allow)
        return True if best is None else best[1]


def normalize_for_frontier(url):
    """Strips the fragment and lowercases the host, but - unlike
    email_audit.py's canonicalize() - keeps the query string. That tool
    can afford to drop queries because it stays on one known modern
    site; this one wanders across arbitrary, often much older sites
    where a query string (?id=, ?p=, ?page=) is frequently the ONLY
    thing identifying which distinct page it is. Stripping it would
    collapse hundreds of different pages into one and defeat the whole
    point of maximizing diversity. The --max-pages-per-domain cap, not
    query-canonicalization, is this tool's defense against a single
    trap-laden host (a calendar, a faceted search) burning the budget -
    it just gets capped out and skipped like any other exhausted host."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}"


def robots_allows(url, timeout):
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    rules = _robots_cache.get(root)
    if rules is None:
        try:
            resp = requests.get(root + "/robots.txt", timeout=timeout)
            rules = WildcardRobots(resp.text if resp.status_code < 400 else "")
        except Exception:
            rules = WildcardRobots("")
        _robots_cache[root] = rules

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return rules.can_fetch(path)


def extract_paragraphs(soup, min_words):
    paragraphs = []
    for tag in soup.find_all("p"):
        if tag.find_parent(["nav", "header", "footer", "aside", "script", "style"]):
            continue
        text = " ".join(tag.get_text(separator=" ", strip=True).split())
        if len(text.split()) >= min_words:
            paragraphs.append(text)
    return paragraphs


class CorpusWriter:
    """Flushes after every write, same as the other tools' live writers -
    an interrupted run shouldn't lose anything already harvested. Reads
    the existing corpus and visited-URL log back in at startup so
    dedup and "don't re-fetch this" both hold across separate runs, not
    just within one."""

    def __init__(self, outdir, fresh):
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.corpus_path = outdir / "corpus.txt"
        self.visited_path = outdir / ".harvested_urls.txt"
        self.frontier_path = outdir / ".harvest_frontier.txt"

        if fresh:
            for path in (self.corpus_path, self.visited_path, self.frontier_path):
                if path.is_file():
                    path.unlink()

        self.seen_paragraphs = set()
        if self.corpus_path.is_file():
            for line in self.corpus_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.seen_paragraphs.add(line)

        self.previously_visited = set()
        if self.visited_path.is_file():
            for line in self.visited_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.previously_visited.add(line)

        self.corpus_f = self.corpus_path.open("a", encoding="utf-8")
        self.visited_f = self.visited_path.open("a", encoding="utf-8")
        self.added_this_run = 0

    def add_paragraph(self, text):
        if text in self.seen_paragraphs:
            return False
        self.seen_paragraphs.add(text)
        self.corpus_f.write(text + "\n")
        self.corpus_f.flush()
        self.added_this_run += 1
        return True

    def mark_visited(self, url):
        self.visited_f.write(url + "\n")
        self.visited_f.flush()

    def close(self):
        self.corpus_f.close()
        self.visited_f.close()


def load_frontier(path):
    if path.is_file():
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return []


def save_frontier(path, queue):
    """Snapshots whatever's still queued but unfetched when the run
    ends - whether from hitting --max-pages or being interrupted - so
    the NEXT run picks up expanding from there instead of just the
    original seeds again. Without this, re-running with the same seed
    list would discover nothing new the moment the seeds themselves are
    already in the visited log, defeating "ever growing.\""""
    path.write_text("\n".join(queue) + ("\n" if queue else ""), encoding="utf-8")


def harvest(seeds, max_pages, max_pages_per_domain, min_words, delay_min, delay_max,
            timeout, max_fetch_seconds, ignore_robots, writer):
    session = requests.Session()
    visited = set(writer.previously_visited)
    per_domain_count = {}
    queue = deque()
    for leftover in load_frontier(writer.frontier_path):
        normalized = normalize_for_frontier(leftover)
        if normalized not in visited:
            queue.append(normalized)
    for seed in seeds:
        normalized = normalize_for_frontier(seed)
        if normalized not in visited:
            queue.append(normalized)

    pages_fetched = 0
    try:
        while queue:
            if max_pages and pages_fetched >= max_pages:
                print(f"Reached --max-pages limit ({max_pages}), stopping.")
                break

            url = queue.popleft()
            if url in visited:
                continue

            netloc = urlparse(url).netloc
            if max_pages_per_domain and per_domain_count.get(netloc, 0) >= max_pages_per_domain:
                visited.add(url)  # this host is tapped out - don't keep reconsidering its pages
                continue

            if not ignore_robots and not robots_allows(url, timeout):
                visited.add(url)
                continue

            visited.add(url)
            writer.mark_visited(url)
            pages_fetched += 1
            per_domain_count[netloc] = per_domain_count.get(netloc, 0) + 1
            print(f"  [{pages_fetched}] {url}")

            headers = {"User-Agent": random.choice(USER_AGENTS)}
            try:
                final_url, status, content_type, text = fetch_page(session, url, headers, timeout, max_fetch_seconds)
            except Exception as exc:
                print(f"  [ERROR] {url}: {exc}")
                time.sleep(random.uniform(delay_min, delay_max))
                continue
            time.sleep(random.uniform(delay_min, delay_max))

            if status >= 400 or "html" not in content_type:
                continue

            try:
                soup = BeautifulSoup(text, "html.parser")
            except Exception:
                continue

            added = sum(writer.add_paragraph(p) for p in extract_paragraphs(soup, min_words))
            if added:
                print(f"      +{added} paragraph(s)")

            page_links, _resources = extract_links(final_url, soup)
            for link in page_links:
                link = normalize_for_frontier(link)
                if link not in visited:
                    queue.append(link)
    finally:
        save_frontier(writer.frontier_path, queue)

    return pages_fetched


def load_seeds(inputs):
    """A single argument that's an existing file is read as one seed
    URL per line (blank lines and '#' comments ignored); otherwise
    every argument is treated as a seed URL directly."""
    if len(inputs) == 1:
        candidate = Path(inputs[0])
        if candidate.is_file():
            seeds = []
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    seeds.append(line)
            return seeds
    return inputs


def main():
    parser = argparse.ArgumentParser(
        description="Wander outward from seed URLs with no domain boundary, collecting <p> text into an ever-growing corpus file."
    )
    parser.add_argument("inputs", nargs="+", help="Seed URLs, or a single file with one URL per line")
    parser.add_argument("--max-pages", type=int, default=500, help="Total page fetch budget for this run (default 500) - the only real stopping condition, since there's no domain boundary")
    parser.add_argument("--max-pages-per-domain", type=int, default=25, help="Caps how many pages from any single host count against the budget per run (0 = unlimited, default 25) - keeps one site from crowding out diversity")
    parser.add_argument("--min-words", type=int, default=5, help="Skip <p> text shorter than this many words (default 5)")
    parser.add_argument("--delay-min", type=float, default=0.4, help="Min seconds between requests (default 0.4)")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max seconds between requests (default 1.2)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request connect/read timeout in seconds (default 10)")
    parser.add_argument("--max-fetch-seconds", type=float, default=30, help="Hard wall-clock cap per request regardless of how slowly the server sends data (default 30)")
    parser.add_argument("--output-dir", default=".", help="Directory for corpus.txt and the visited-URL log (default: current dir)")
    parser.add_argument("--ignore-robots", action="store_true", help="Skip robots.txt checks. Off by default, unlike this project's other tools - this one wanders onto sites you don't own, so it respects robots.txt unless told not to.")
    parser.add_argument("--fresh", action="store_true", help="Discard the existing corpus and visited-URL history and start a brand new corpus")
    args = parser.parse_args()

    seeds = load_seeds(args.inputs)
    writer = CorpusWriter(args.output_dir, args.fresh)
    frontier_size = len(load_frontier(writer.frontier_path))
    print(f"Corpus already has {len(writer.seen_paragraphs)} paragraph(s); "
          f"{len(writer.previously_visited)} URL(s) already harvested will be skipped; "
          f"{frontier_size} unexplored URL(s) left over from a previous run will be explored first.")

    started = time.time()
    try:
        pages_fetched = harvest(
            seeds, args.max_pages, args.max_pages_per_domain, args.min_words,
            args.delay_min, args.delay_max, args.timeout, args.max_fetch_seconds,
            args.ignore_robots, writer,
        )
    finally:
        writer.close()

    elapsed = time.time() - started
    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Pages fetched this run: {pages_fetched}")
    print(f"New paragraphs added this run: {writer.added_this_run}")
    print(f"Corpus total: {len(writer.seen_paragraphs)} paragraph(s)")
    print(f"Corpus file: {writer.corpus_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
