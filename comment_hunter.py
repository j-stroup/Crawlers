#!/usr/bin/env python3
"""Comment & Easter-egg hunter - wanders outward from seed URLs with no
domain boundary, digging through page *source* for the human things
people leave behind: hand-written HTML comments (jokes, apologies, TODOs,
hiring messages, confessions), ASCII art, console-message greetings,
jokey response headers (the famous X-Clacks-Overhead), and humans.txt
credits.

Same family as text_harvester.py: single-threaded, respects robots.txt
by default (it roams onto sites you don't own), appends to its output
files and reads them back at startup so it's resumable and ever-growing.
The hard part isn't finding comments - it's that ~99% of HTML comments
are boilerplate (`<!-- end header -->`, WordPress block markers, IE
conditionals, analytics snippets). So each candidate is scored, and only
the ones that read like a human wrote them on purpose are kept.

Outputs (all in --output-dir, all append-only / rebuilt as it goes):
    finds.jsonl             - one JSON record per find (the durable log)
    comment_hunter.txt      - a plain-text running log, easy to skim/paste
    gallery.html            - a dark, monospaced gallery (open it and watch)

Usage:
    python comment_hunter.py https://example.com --max-pages 500
    python comment_hunter.py seeds.txt --max-pages 5000 --max-pages-per-domain 30
    python comment_hunter.py seeds.txt --fresh
"""

import argparse
import html as html_module
import json
import random
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from quick_scan import extract_links
from text_harvester import (
    robots_allows, normalize_for_frontier, load_frontier, save_frontier, load_seeds,
)

# --------------------------------------------------------------------------
# Comment scoring - the whole point. Reject boilerplate, keep the gems.
# --------------------------------------------------------------------------

# Hard skips: if a comment contains any of these it's machine-generated
# plumbing, never a human note. (Substrings, matched case-insensitively.)
HARD_SKIP = (
    "[if ", "endif]", "![endif]",                 # IE conditional comments
    "wp:", "/wp:",                                # WordPress block markers
    "yoast", "aioseo", "all in one seo", "rank math", "seopress",
    "google tag manager", "gtm-", "gtag", "global site tag",
    "google analytics", "facebook pixel", "fbq(", "hotjar", "clarity",
    "doubleclick", "adsense", "adservice", "taboola", "outbrain",
    "optimized with", "powered by wp", "wp rocket", "w3 total",
    "litespeed", "autoptimize", "nitropack", "wp super cache",
    "page cache", "cached page", "performance optimized",
    "elementor", "et_pb", "wpb_", "vc_row", "fusion-",
    "json-ld", "schema.org", "application/ld",
    "begintoggle", "googleoff", "googleon",
    # Cookie-consent / privacy-banner plumbing
    "consent", "cookiebot", "onetrust", "usercentrics", "cookieyes",
    "gdpr", "ccpa", "cookie banner", "cookie notice",
    # Ubiquitous third-party library banners (ship identically everywhere,
    # so they're boilerplate, not a site's own voice)
    "font awesome", "fontawesome", "jquery", "bootstrap", "modernizr",
    "normalize.css", "swiper", "owl carousel", "fancybox", "magnific",
    "gsap ", "animate.css", "slick carousel", "select2", "lightbox",
    "isotope", "masonry", "splide", "aos.js", "wow.js", "flickity",
)

# Minified third-party library banners nearly always pair a version number
# with a license/copyright line - a generic way to drop the ones not named
# above without touching a human's actual sentence.
_LIB_BANNER_RE = re.compile(
    r"\bv?\d+\.\d+(?:\.\d+)?\b.*\b(?:license|licence|copyright|\(c\))\b|"
    r"\b(?:license|licence|copyright|\(c\))\b.*\bv?\d+\.\d+(?:\.\d+)?\b",
    re.DOTALL,
)

# Strong signals - any one of these auto-keeps the comment (these are the
# finds worth crawling for), each also tagged for the gallery.
STRONG_SIGNALS = {
    "hiring": ("hiring", "we're hiring", "we are hiring", "join our team",
               "join us", "work with us", "careers", "jobs@", "now hiring",
               "apply now", "come work"),
    "greeting": ("if you're reading this", "if you are reading this",
                 "reading this", "you found", "nothing to see here",
                 "move along", "hello there", "hi there", "hey you",
                 "welcome, ", "welcome!", "congrats", "congratulations",
                 "curious", "snooping", "peeking", "caught you"),
    "easter-egg": ("easter egg", "konami", "secret", "you win", "cheat code",
                   "achievement unlocked", "unlock", "hidden message",
                   "psst", "the cake is a lie"),
    "credit": ("made with love", "made with ❤", "built with love",
               "crafted with", "handcrafted", "designed & built",
               "designed and built", "coded by", "handmade", "lovingly",
               "with ♥", "built by hand"),
    "confession": ("i'm sorry", "i am sorry", "apologies", "forgive me",
                   "here be dragons", "don't touch", "do not touch",
                   "don't remove", "do not remove", "i have no idea",
                   "no idea why", "this is a hack", "it's a hack",
                   "god help", "pray", "cursed", "haunted", "i hate this",
                   "please don't", "why does this work", "do not ask",
                   "touch this and", "if it works", "don't ask"),
}

# Softer dev-voice words: each adds to the score but doesn't auto-keep.
DEV_VOICE = (
    "todo", "fixme", "hack", "kludge", "workaround", "temporary", "temp fix",
    "quick fix", "for now", "spaghetti", "duct tape", "band-aid", "bandaid",
    "magic", "voodoo", "wtf", "ugh", "sigh", "yikes", "oops", "whoops",
    "legacy", "deprecated", "gross", "nasty", "ridiculous", "stupid",
    "i think", "i guess", "probably", "hopefully", "fingers crossed",
    "note to self", "warning", "careful", "beware", "danger",
)

# Kaomoji / emoticons - a strong "a human typed this" tell.
KAOMOJI = ("¯\\_(ツ)_/¯", "(ツ)", "(╯", "ಠ_ಠ", "¯\\", ":-)", ":)", ":-(", ";-)",
           "<3", "❤", "♥", ">_<", "^_^", "o_o", "0_0", "d:", ":d")

# Characters that make up ASCII art.
ART_CHARS = set("/\\|_-=+*#@~^<>(){}[].:;'\"`")
ART_CHARS |= set("─│┌┐└┘├┤┬┴┼"
                 "█▀▄░▒▓═║╬╔╗"
                 "╚╝■●•·")

KEEP_THRESHOLD = 3


def _collapse(text):
    return " ".join(text.split())


def looks_like_code(text):
    """Commented-out markup or JS - common and boring, never a human note."""
    if re.search(r"<\w+[\s/>]", text) or "</" in text:
        return True
    if re.search(r"\b(var|function|const|let)\b|=>|\)\s*;|\{\s*\}|\$\(", text):
        return True
    return False


def detect_ascii_art(text):
    """True if a block is ASCII art rather than prose: a recognizable
    kaomoji, or several lines carrying a high proportion of drawing
    characters. Commented-out HTML/markup is explicitly excluded - it's
    code, not art."""
    low = text.lower()
    if any(k in low for k in ("¯\\_(ツ)", "(╯", "ಠ_ಠ", ">_<", "^_^", "o_o")):
        return True
    if re.search(r"</?\w+[\s/>]", text):          # commented-out markup, not art
        return False
    lines = [ln for ln in text.strip("\n").splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    artish = 0
    for ln in lines:
        core = ln.strip()
        if len(core) < 4:
            continue
        if sum(1 for ch in core if ch in ART_CHARS) / len(core) >= 0.4:
            artish += 1
    return artish >= 3 and artish / len(lines) >= 0.45


def classify_comment(raw):
    """Decide what, if anything, an HTML comment is worth. Returns
    (kind, score, tags) or None to drop it. kind is 'ascii_art' or
    'html_comment'."""
    text = raw.strip()
    collapsed = _collapse(text)
    low = collapsed.lower()

    if detect_ascii_art(raw):
        return ("ascii_art", 6, ["ascii-art"])

    if len(low) < 10:
        return None
    if any(bad in low for bad in HARD_SKIP):
        return None
    if looks_like_code(text):
        return None
    if _LIB_BANNER_RE.search(low):            # minified library banner
        return None

    words = low.split()
    wc = len(words)
    tags = []
    score = 0

    for tag, needles in STRONG_SIGNALS.items():
        if any(n in low for n in needles):
            tags.append(tag)
    if tags:
        score += 5  # a strong signal is an auto-keep

    hits = [w for w in DEV_VOICE if w in low]
    if hits:
        tags.append("dev-voice")
        score += min(len(hits), 3)

    if any(k in collapsed for k in KAOMOJI) or any(k in low for k in KAOMOJI):
        tags.append("emoticon")
        score += 2

    if wc >= 4:
        score += 1
    if wc >= 9:
        score += 1
    if re.search(r"[a-z].*[.!?]", low) and wc >= 5:
        score += 1              # reads like an actual sentence
    if re.search(r"\b(i|we|you|my|our|your|me|us)\b", low):
        score += 1

    # A short fragment with no human signal at all is almost certainly a
    # section marker ("main content", "end sidebar") - drop it.
    if not tags and wc <= 4:
        return None

    if score < KEEP_THRESHOLD:
        return None
    return ("html_comment", score, tags or ["prose"])


# Inline console.* greetings (console.log("👋 we're hiring!"), the %c
# branded banners, self-XSS "Stop!" warnings...).
_CONSOLE_RE = re.compile(
    r"""console\.(?:log|info|warn|error)\s*\(\s*(['"`])(.*?)\1""",
    re.DOTALL,
)


def extract_console_eggs(soup):
    out = []
    for tag in soup.find_all("script"):
        if tag.get("src"):
            continue
        code = tag.string or tag.get_text() or ""
        if "console" not in code:
            continue
        for _q, msg in _CONSOLE_RE.findall(code):
            msg = msg.strip()
            low = msg.lower()
            clean = msg.replace("%c", "").strip()
            interesting = (
                any(k in low for k in (
                    "hiring", "stop", "warning", "wait", "hello", "hey",
                    "welcome", "hi ", "self-xss", "self xss", "scam", "join",
                    "careers", "console", "developer", "psst", "secret",
                    "🛑", "👋", "✋", "⚠", "🚀", "❤", "👀"))
                or (len(clean) >= 18 and " " in clean and "%c" not in msg[:3])
            )
            if interesting and len(clean) >= 4:
                out.append(clean)
    return out


# Response headers worth keeping: the known memorials/jokes, plus any odd
# X- header whose value reads like a sentence (and isn't boring plumbing).
BORING_HEADER_VALUE_HINTS = ("gzip", "max-age", "charset", "nosniff", "deny",
                             "sameorigin", "no-cache", "bytes", "keep-alive")
BORING_HEADERS = {
    "x-powered-by", "x-frame-options", "x-content-type-options",
    "x-xss-protection", "x-cache", "x-served-by", "x-request-id",
    "x-runtime", "x-ua-compatible", "x-aspnet-version", "x-aspnetmvc-version",
    "x-varnish", "x-timer", "x-cache-hits", "x-fastly-request-id",
    "x-amz-cf-id", "x-amz-cf-pop", "x-amz-request-id", "x-github-request-id",
    "x-cdn", "x-edge-location", "x-download-options", "x-permitted-cross-domain-policies",
    "x-dns-prefetch-control", "x-redirect-by", "x-pingback", "x-robots-tag",
}


def extract_header_eggs(headers):
    out = []
    for name, value in headers.items():
        low_name = name.lower()
        if low_name == "x-clacks-overhead":
            out.append(f"{name}: {value}")          # GNU Terry Pratchett 🙏
            continue
        if not low_name.startswith("x-"):
            continue
        if low_name in BORING_HEADERS:
            continue
        v = value.strip()
        vl = v.lower()
        if any(b in vl for b in BORING_HEADER_VALUE_HINTS):
            continue
        # Reads like a human wrote it: has a space and some letters, and
        # isn't just an id/hash/token.
        if " " in v and re.search(r"[a-zA-Z]{3,}\s+[a-zA-Z]{3,}", v) and len(v) <= 120:
            out.append(f"{name}: {v}")
    return out


def extract_humans_txt(session, scheme, host, timeout):
    """Fetch /humans.txt once per host - it's where devs credit themselves,
    usually with personality. Returns the text (trimmed) or None."""
    url = f"{scheme}://{host}/humans.txt"
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "html" in ctype:        # soft-404 served an HTML page, not a real humans.txt
        return None
    text = resp.text.strip()
    if len(text) < 15 or "<html" in text.lower():
        return None
    return text[:4000]


# --------------------------------------------------------------------------
# Output: durable JSONL + plain-text log + the HTML gallery
# --------------------------------------------------------------------------

KIND_LABELS = {
    "html_comment": "HTML comment",
    "ascii_art": "ASCII art",
    "console": "console message",
    "header": "jokey header",
    "humans_txt": "humans.txt",
}


def _find_key(kind, text):
    return kind + "\x00" + _collapse(text).lower()[:300]


class FindStore:
    """Append-only finds.jsonl + a plain-text log, read back at startup so
    the same comment isn't recorded twice across runs and the gallery can
    be rebuilt from scratch. Flushes after every write."""

    def __init__(self, outdir, fresh):
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = outdir / "finds.jsonl"
        self.log_path = outdir / "comment_hunter.txt"
        self.gallery_path = outdir / "gallery.html"

        if fresh:
            for p in (self.jsonl_path, self.log_path, self.gallery_path):
                if p.is_file():
                    p.unlink()

        self.seen = set()
        self.finds = []           # all finds (dicts), for the gallery
        self.per_domain = {}
        if self.jsonl_path.is_file():
            for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                self.seen.add(_find_key(rec["kind"], rec["text"]))
                self.finds.append(rec)
                self.per_domain[rec["domain"]] = self.per_domain.get(rec["domain"], 0) + 1

        self.jsonl_f = self.jsonl_path.open("a", encoding="utf-8")
        self.log_f = self.log_path.open("a", encoding="utf-8")
        self.added_this_run = 0

    def already_have(self, kind, text):
        return _find_key(kind, text) in self.seen

    def domain_count(self, domain):
        return self.per_domain.get(domain, 0)

    def add(self, kind, text, url, domain, score, tags):
        key = _find_key(kind, text)
        if key in self.seen:
            return False
        self.seen.add(key)
        rec = {
            "kind": kind, "text": text, "url": url, "domain": domain,
            "score": score, "tags": tags,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self.finds.append(rec)
        self.per_domain[domain] = self.per_domain.get(domain, 0) + 1
        self.jsonl_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.jsonl_f.flush()

        label = KIND_LABELS.get(kind, kind)
        tagstr = (" [" + ", ".join(tags) + "]") if tags else ""
        self.log_f.write(f"--- {label}{tagstr}  ({domain})\n")
        self.log_f.write(f"    {url}\n")
        for ln in text.splitlines() or [text]:
            self.log_f.write(f"    | {ln}\n")
        self.log_f.write("\n")
        self.log_f.flush()
        self.added_this_run += 1

        preview = _collapse(text)
        print(f"      ★ [{label}]{tagstr} {preview[:90]}")
        return True

    def close(self):
        self.jsonl_f.close()
        self.log_f.close()

    def write_gallery(self):
        try:
            self.gallery_path.write_text(render_gallery(self.finds), encoding="utf-8")
        except Exception as exc:
            print(f"  [GALLERY ERROR] {exc}")


def _esc(text):
    return html_module.escape(str(text), quote=True)


def render_gallery(finds):
    total = len(finds)
    by_kind = {}
    for rec in finds:
        by_kind.setdefault(rec["kind"], []).append(rec)

    def card(rec):
        host = _esc(rec["domain"])
        url = _esc(rec["url"])
        tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in rec.get("tags", []))
        label = _esc(KIND_LABELS.get(rec["kind"], rec["kind"]))
        return (
            '<div class="card">'
            f'<div class="meta"><span class="kind">{label}</span>{tags}'
            f'<span class="score">score {rec.get("score","")}</span></div>'
            f'<pre>{_esc(rec["text"])}</pre>'
            f'<div class="src"><a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{host}</a></div>'
            '</div>'
        )

    hall = sorted(finds, key=lambda r: (-r.get("score", 0), r.get("ts", "")))[:40]
    recent = list(reversed(finds))[:80]

    sections = []
    sections.append('<h2>⭐ Hall of Fame <span class="n">top ' + str(len(hall)) + '</span></h2>')
    sections.append("".join(card(r) for r in hall) or '<p class="empty">nothing yet - let it crawl</p>')
    sections.append('<h2>\U0001f552 Most recent <span class="n">last ' + str(len(recent)) + '</span></h2>')
    sections.append("".join(card(r) for r in recent) or "")
    for kind, label in KIND_LABELS.items():
        items = by_kind.get(kind, [])
        if not items:
            continue
        sections.append(f'<h2>{_esc(label)} <span class="n">{len(items)}</span></h2>')
        sections.append("".join(card(r) for r in list(reversed(items))[:120]))

    counts = " · ".join(
        f"{len(v)} {KIND_LABELS.get(k, k)}" for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
    )
    domains = len({r["domain"] for r in finds})
    generated = datetime.now().isoformat(timespec="seconds")

    return _GALLERY_TEMPLATE.replace("__TOTAL__", str(total)) \
        .replace("__DOMAINS__", str(domains)) \
        .replace("__COUNTS__", _esc(counts)) \
        .replace("__GENERATED__", _esc(generated)) \
        .replace("__BODY__", "".join(sections))


_GALLERY_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Comment & Easter-Egg Hunter</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0e0f13; color:#e7e7df;
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 28px 18px 80px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color:#8d8f98; font-size:13px; margin:0 0 6px; }
  .counts { color:#b9bbc6; font-size:12.5px; margin:0 0 22px;
    border-bottom:1px solid #23252e; padding-bottom:14px; }
  h2 { font-size:15px; margin:30px 0 12px; color:#f2c14e; display:flex;
    align-items:baseline; gap:8px; }
  h2 .n { font-size:11px; color:#75777f; font-weight:400; }
  .card { background:#15171d; border:1px solid #23252e; border-radius:10px;
    padding:12px 14px; margin:0 0 12px; }
  .meta { display:flex; flex-wrap:wrap; align-items:center; gap:7px;
    margin-bottom:8px; font-size:11.5px; }
  .kind { color:#7fd1e3; font-weight:600; text-transform:uppercase;
    letter-spacing:.04em; }
  .tag { background:#262a36; color:#c6b2f0; border-radius:999px;
    padding:1px 8px; font-size:11px; }
  .score { margin-left:auto; color:#6a6d78; font-variant-numeric:tabular-nums; }
  pre { margin:0; white-space:pre-wrap; word-break:break-word;
    font:13px/1.45 ui-monospace,"SF Mono",Consolas,monospace; color:#e7e7df; }
  .src { margin-top:8px; font-size:12px; }
  .src a { color:#6ea8fe; text-decoration:none; }
  .src a:hover { text-decoration:underline; }
  .empty { color:#75777f; }
</style></head>
<body><div class="wrap">
  <h1>Comment &amp; Easter-Egg Hunter</h1>
  <p class="sub">Human things found hiding in the source while crawling the open web.</p>
  <p class="counts"><b>__TOTAL__</b> finds across <b>__DOMAINS__</b> domains &middot; __COUNTS__ &middot; updated __GENERATED__</p>
  __BODY__
</div></body></html>
"""


# --------------------------------------------------------------------------
# Crawl loop (mirrors text_harvester.harvest)
# --------------------------------------------------------------------------

def fetch_page(session, url, headers, timeout, max_fetch_seconds):
    """Like email_audit.fetch_page but also returns the response headers
    (we want them for the jokey-header check)."""
    started = time.monotonic()
    with session.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as resp:
        final_url = resp.url
        status = resp.status_code
        resp_headers = resp.headers
        content_type = resp_headers.get("Content-Type", "")
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=8192):
            body.extend(chunk)
            if time.monotonic() - started > max_fetch_seconds:
                raise TimeoutError(f"exceeded {max_fetch_seconds:.0f}s wall-clock fetch limit")
        text = body.decode(resp.encoding or "utf-8", errors="replace")
    return final_url, status, content_type, resp_headers, text


def harvest_page(store, url, final_url, html, headers, session, timeout,
                 ignore_robots, max_finds_per_domain, no_humans):
    domain = urlparse(final_url).netloc
    if max_finds_per_domain and store.domain_count(domain) >= max_finds_per_domain:
        return

    def allowed():
        return not max_finds_per_domain or store.domain_count(domain) < max_finds_per_domain

    # Jokey response headers
    for egg in extract_header_eggs(headers):
        if allowed():
            store.add("header", egg, final_url, domain, 5, ["header"])

    # HTML comments + ASCII art (regex over raw source catches everything)
    for raw in re.findall(r"<!--(.*?)-->", html, re.DOTALL):
        if not allowed():
            break
        verdict = classify_comment(raw)
        if verdict is None:
            continue
        kind, score, tags = verdict
        text = raw.strip("\n") if kind == "ascii_art" else _collapse(raw)
        if not store.already_have(kind, text):
            store.add(kind, text, final_url, domain, score, tags)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    if soup is not None:
        for msg in extract_console_eggs(soup):
            if allowed():
                store.add("console", msg, final_url, domain, 5, ["console"])

    # humans.txt - once per host (mark the URL visited so we don't re-probe)
    if not no_humans:
        parsed = urlparse(final_url)
        humans_url = f"{parsed.scheme}://{parsed.netloc}/humans.txt"
        if humans_url not in store_probed:
            store_probed.add(humans_url)
            if ignore_robots or robots_allows(humans_url, timeout):
                body = extract_humans_txt(session, parsed.scheme, parsed.netloc, timeout)
                if body and allowed():
                    store.add("humans_txt", body, humans_url, domain, 4, ["credits"])

    return soup


# Hosts whose humans.txt we've already probed this run (kept simple/global).
store_probed = set()


def harvest(seeds, args, store):
    session = requests.Session()
    visited = load_visited(args.output_dir)
    per_domain_pages = {}
    queue = deque()
    for leftover in load_frontier(frontier_path(args.output_dir)):
        n = normalize_for_frontier(leftover)
        if n not in visited:
            queue.append(n)
    for seed in seeds:
        n = normalize_for_frontier(seed)
        if n not in visited:
            queue.append(n)

    visited_f = (Path(args.output_dir) / VISITED_NAME).open("a", encoding="utf-8")
    pages = 0
    last_gallery = 0
    try:
        while queue:
            if args.max_pages and pages >= args.max_pages:
                print(f"Reached --max-pages limit ({args.max_pages}), stopping.")
                break
            url = queue.popleft()
            if url in visited:
                continue

            netloc = urlparse(url).netloc
            if args.max_pages_per_domain and per_domain_pages.get(netloc, 0) >= args.max_pages_per_domain:
                visited.add(url)
                continue
            if not args.ignore_robots and not robots_allows(url, args.timeout):
                visited.add(url)
                continue

            visited.add(url)
            visited_f.write(url + "\n")
            visited_f.flush()
            pages += 1
            per_domain_pages[netloc] = per_domain_pages.get(netloc, 0) + 1
            print(f"  [{pages}] {url}")

            headers = {"User-Agent": random.choice(USER_AGENTS)}
            try:
                final_url, status, ctype, resp_headers, text = fetch_page(
                    session, url, headers, args.timeout, args.max_fetch_seconds)
            except Exception as exc:
                print(f"  [ERROR] {url}: {exc}")
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue
            time.sleep(random.uniform(args.delay_min, args.delay_max))

            if status >= 400 or "html" not in ctype:
                continue

            soup = harvest_page(store, url, final_url, text, resp_headers, session,
                                args.timeout, args.ignore_robots,
                                args.max_finds_per_domain, args.no_humans_txt)

            if soup is not None:
                page_links, _resources = extract_links(final_url, soup)
                for link in page_links:
                    link = normalize_for_frontier(link)
                    if link not in visited:
                        queue.append(link)

            if pages - last_gallery >= args.gallery_every:
                store.write_gallery()
                last_gallery = pages
    finally:
        visited_f.close()
        save_frontier(frontier_path(args.output_dir), list(queue))
        store.write_gallery()

    return pages


# --------------------------------------------------------------------------
# Resume bookkeeping (plain files, same style as text_harvester)
# --------------------------------------------------------------------------

VISITED_NAME = ".chunt_visited.txt"
FRONTIER_NAME = ".chunt_frontier.txt"


def frontier_path(outdir):
    return Path(outdir) / FRONTIER_NAME


def load_visited(outdir):
    path = Path(outdir) / VISITED_NAME
    seen = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                seen.add(line)
    return seen


def main():
    parser = argparse.ArgumentParser(
        description="Wander the open web collecting interesting HTML comments and Easter eggs.")
    parser.add_argument("inputs", nargs="+", help="Seed URLs, or a single file with one URL per line")
    parser.add_argument("--max-pages", type=int, default=500,
                         help="Total page fetch budget for this run (default 500)")
    parser.add_argument("--max-pages-per-domain", type=int, default=25,
                         help="Cap pages fetched per host per run (0 = unlimited, default 25) - keeps it roaming")
    parser.add_argument("--max-finds-per-domain", type=int, default=25,
                         help="Cap finds recorded per host (0 = unlimited, default 25) - one quirky site can't flood the gallery")
    parser.add_argument("--delay-min", type=float, default=0.4, help="Min seconds between requests (default 0.4)")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max seconds between requests (default 1.2)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request connect/read timeout (default 10)")
    parser.add_argument("--max-fetch-seconds", type=float, default=30,
                         help="Hard wall-clock cap per request (default 30)")
    parser.add_argument("--gallery-every", type=int, default=25,
                         help="Rebuild gallery.html every N pages (default 25)")
    parser.add_argument("--no-humans-txt", action="store_true", help="Don't probe /humans.txt per host")
    parser.add_argument("--output-dir", default=".", help="Directory for outputs (default: current dir)")
    parser.add_argument("--ignore-robots", action="store_true",
                         help="Skip robots.txt checks (off by default - this tool roams sites you don't own)")
    parser.add_argument("--fresh", action="store_true", help="Discard all finds and crawl history, start clean")
    args = parser.parse_args()

    # This tool deliberately collects kaomoji, emoji and box-drawing art,
    # none of which the default Windows console (cp1252) can encode - so a
    # single such find would otherwise crash the live log with a
    # UnicodeEncodeError mid-crawl. Force stdout to utf-8, replacing
    # anything the terminal still can't show.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    seeds = load_seeds(args.inputs)
    if args.fresh:
        for name in (VISITED_NAME, FRONTIER_NAME):
            p = Path(args.output_dir) / name
            if p.is_file():
                p.unlink()
    store = FindStore(args.output_dir, args.fresh)
    left = len(load_frontier(frontier_path(args.output_dir)))
    print(f"Starting with {len(store.finds)} find(s) already logged; "
          f"{len(load_visited(args.output_dir))} URL(s) already visited; "
          f"{left} URL(s) left over in the frontier.")

    started = time.time()
    try:
        pages = harvest(seeds, args, store)
    finally:
        store.close()

    elapsed = time.time() - started
    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Pages fetched this run : {pages}")
    print(f"New finds this run     : {store.added_this_run}")
    print(f"Total finds logged     : {len(store.finds)}")
    print(f"Gallery                : {store.gallery_path}")
    print(f"Plain-text log         : {store.log_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
