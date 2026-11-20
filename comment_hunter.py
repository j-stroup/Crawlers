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

Each domain's homepage is always crawled first (eggs cluster there), and
--max-pages-per-domain (default 5) keeps the crawl from sinking into any
one site so it keeps hopping to new domains.

Usage:
    python comment_hunter.py https://example.com --max-pages 500
    python comment_hunter.py seeds.txt --max-pages 5000 --max-pages-per-domain 3
    python comment_hunter.py seeds.txt --max-pages-per-domain 1   # homepages only
    python comment_hunter.py seeds.txt --fresh
"""

import argparse
import hashlib
import html as html_module
import json
import random
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

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

# Dev-voice, split by confidence. STRONG words carry real personality/humor
# and essentially never appear in routine code documentation, so one is
# enough to keep. WEAK words are mildly informal but also show up in boring
# dev-doc, so they only count a little and need company.
STRONG_DEV = (
    "hack", "kludge", "voodoo", "black magic", "dark magic", "wtf", "ugh",
    "sigh", "yikes", "oops", "whoops", "spaghetti", "duct tape", "band-aid",
    "bandaid", "fingers crossed", "god help", "i hate", "nobody knows",
    "no one knows", "no idea", "don't ask", "dont ask", "good luck",
    "future dev", "future developer", "poor soul", "next person", "whoever",
    " lol", "haha", "hehe", " omg", "yolo", "ship it", "cursed", "haunted",
    "scared", "afraid", "temporary", "temp fix", "quick fix", "nightmare",
    "don't touch", "dont touch", "do not touch", "don't remove", "dont remove",
    "please work", "it works", "why does", "how is this", "how does this",
    "somehow", "mystery", "magic number", "i'm sorry", "im sorry", "sorry",
    "apolog", "forgive", "here be dragons", "beer", "coffee", " 3am", "3 am",
    "midnight", "deadline", "i swear", "i promise", "trust me", "don't judge",
    "dont judge", "rage", "screaming", "crying", "in tears", "hair out",
)
WEAK_DEV = (
    "todo", "fixme", "for now", "workaround", "hopefully", "probably",
    "i think", "i guess", "temporarily", "sadly", "unfortunately",
)
# A subset of STRONG_DEV that is really just code jargon: in a CSS/JS source
# comment ("/* temporary hack */", "magic number below", "fixed somehow")
# these are routine, not personality. In strict mode (foreign comments) they
# no longer count as a "real signal", so a comment needs an actual confession/
# greeting/emoji/emoticon to survive. In HTML comments they still score.
CODE_JARGON = (
    "hack", "kludge", "temporary", "temp fix", "quick fix", "somehow",
    "magic number",
)
# Bare structural labels: an emoji stuck on one of these ("<!-- footer 🔚 -->")
# is decoration, not a human note. If the ONLY signal is the emoji/emoticon
# and every real word is structural, drop it.
STRUCTURAL_WORDS = frozenset((
    "footer", "header", "head", "section", "nav", "navigation", "intro",
    "body", "colophon", "content", "wrapper", "container", "main", "sidebar",
    "banner", "menu", "start", "end", "begin", "top", "bottom", "left",
    "right", "row", "col", "column", "grid", "block", "box", "page",
))

# Emoticons - matched only when they stand alone (surrounded by space/edge/
# punctuation), never as substrings. The old substring list fired on things
# like "modified:" (-> "d:") and "<300" (-> "<3"); this does not.
EMOTICON_RE = re.compile(
    r"(?:^|\s)(?::-?\)|:-?\(|:-?D|;-?\)|<3|\^_\^|>_<|¯\\_\(ツ\)_/¯|\(╯°|ಠ_ಠ|owo|uwu)"
    r"(?=$|\s|[.,!?])"
)
# Real emoji - an intentional, unambiguous "a human put this here" signal.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF⭐❤♥⚠⬆✔]"
)


def has_emoji(text):
    return bool(_EMOJI_RE.search(text))


# Symbols devs use as bullets / status markers in comments (✅ done, ❌ no,
# ★ note, → see). _EMOJI_RE matches several of these, but a checkmark on a
# routine comment ("✅ Fix resize listener", "✅ overflow 방지") is NOT the
# "a human left this on purpose" signal a real pictographic emoji is.
_WEAK_MARKERS = set("✅❌☑☒✔✘✓✗✖✕★☆✰✦✧✱✲✳✴❖▶◀◆◇○●→←↑↓⇒⇐➜➤➔➡❱❰⚓")


def has_meaningful_emoji(text):
    """True only for a real pictographic emoji - not a bare bullet/status marker
    (✅/★/→/❰❱). Strip the markers first, then look for an actual emoji."""
    return has_emoji("".join(c for c in text if c not in _WEAK_MARKERS))


# Keyword matching must respect word boundaries, or short words match inside
# longer ones: "rage" in "AVERAGE", "ugh" in "through", "sigh" in "Insights",
# "oops" in "loops", "secret" in "secretary", "pray" in "spray". A plain
# substring check is exactly how the first version produced ~90% noise.
def _kw_pattern(needle):
    n = needle.strip()
    left = r"(?<![a-z0-9])" if n[:1].isalnum() else ""
    right = r"(?![a-z0-9])" if n[-1:].isalnum() else ""
    return left + re.escape(n) + right


def _kw_re(words):
    return re.compile("|".join(_kw_pattern(w) for w in words), re.IGNORECASE)


_STRONG_SIGNAL_RES = {tag: _kw_re(words) for tag, words in STRONG_SIGNALS.items()}
_STRONG_DEV_RE = _kw_re(STRONG_DEV)
# Strict variant with code jargon removed - used for CSS/JS source comments.
_STRONG_DEV_STRICT_RE = _kw_re([w for w in STRONG_DEV if w not in CODE_JARGON])
_WEAK_DEV_RE = _kw_re(WEAK_DEV)


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
    if any(k in low for k in ("¯\\_(ツ)", "(╯°", "ಠ_ಠ")):   # distinctive kaomoji only
        return True
    if re.search(r"</?\w+[\s/>]", text):          # commented-out markup, not art
        return False
    if re.search(r"\{\{|\}\}|\{\||\|\}", text):   # wiki/template markup, not art
        return False
    if re.search(r"!\*{2,}|\*{2,}!", text):       # webpack module banner (!*** ./path ***!)
        return False
    lines = [ln for ln in text.strip("\n").splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    # A flat bar / section divider / ruler / cookie-consent banner is drawn with
    # ONE repeated char packed solid, maybe wrapped around a short label
    # ("**** General ****", "---- 16.1 >= 518px ----", "████", "......"), so one
    # char dominates the drawing glyphs AND the block is dense. Block-letter art
    # is also single-char but SPARSE (gaps form the letters), and real art with a
    # greeting/question in it ("Hey! I'm Ralph, what's up? ████") is a human
    # flourish, not a divider - both are spared.
    arts = [c for c in text if c in ART_CHARS]
    if len(arts) >= 10:
        inked = "".join(ln.strip() for ln in lines)
        dens = sum(1 for c in inked if c in ART_CHARS) / max(1, len(inked))
        dominant = max(arts.count(c) for c in set(arts)) / len(arts)
        human = "?" in text or re.search(r"\b(hey|hi|hello|yo|sup|howdy|welcome|greetings)\b", low)
        if dominant >= 0.9 and dens >= 0.6 and not human:
            return False
    artish = 0
    for ln in lines:
        core = ln.strip()
        if len(core) < 4:
            continue
        art = sum(1 for ch in core if ch in ART_CHARS)
        # Digits (and a few letters) are common "shading" pixels in real ASCII
        # art - d888P, o8OOo, Y8P. Count a line as art if it's dense in drawing
        # glyphs, OR dense in drawing-glyphs-plus-digits, OR carries a run of
        # >=4 identical non-space chars (8888, ####, ----) - a strong art tell
        # that prose almost never has.
        ink = art + sum(1 for ch in core if ch.isdigit())
        has_run = re.search(r"(\S)\1{3,}", core) is not None
        if (art / len(core) >= 0.4
                or ink / len(core) >= 0.55
                or (has_run and ink / len(core) >= 0.35)):
            artish += 1
    return artish >= 3 and artish / len(lines) >= 0.45


def normalize_ascii(text):
    """Make ASCII art render predictably: expand tabs to spaces (tab stops
    otherwise shift lines by different amounts), drop blank top/bottom
    lines, and strip the common left indentation so the art hugs the left
    edge instead of being pushed off-screen. The shape (relative alignment)
    is preserved. Idempotent."""
    lines = text.expandtabs(8).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return text
    indent = min(len(ln) - len(ln.lstrip(" ")) for ln in lines if ln.strip())
    if indent:
        lines = [ln[indent:] if len(ln) >= indent else ln.lstrip(" ") for ln in lines]
    return "\n".join(lines)


def classify_comment(raw, strict=False):
    """Decide what, if anything, an HTML comment is worth. Returns
    (kind, score, tags) or None to drop it. kind is 'ascii_art' or
    'html_comment'.

    strict=True is used for comments lifted from CSS/JS source files, where
    code jargon ("hack", "temporary", "magic number", "somehow") is routine
    rather than personality: in strict mode those words no longer count as a
    real signal, so the comment must carry an actual confession/greeting word,
    emoji, or emoticon to survive."""
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

    # Section / template markers that only score because a keyword happens to
    # sit inside them ("How it works", "Deadline Funnel", "Begin/End <thing>",
    # "<name> Section", "KS-...:END") - structural labels, not human notes. A
    # real face (emoji/emoticon) exempts them ("end with global configs :-(").
    if not (EMOTICON_RE.search(" " + collapsed + " ") or has_meaningful_emoji(text)):
        if "how it works" in low or "deadline funnel" in low:
            return None
        if len(low) <= 45 and (low.endswith("section")
                               or re.search(r":(start|end)\b", low)
                               or re.match(r"^(end|begin|start)\b", low)):
            return None

    tags = []
    score = 0
    real_signal = False       # did a genuine human marker fire? (strict gate)

    # Strong signals (hiring/greeting/easter-egg/credit/confession) auto-keep.
    for tag, rex in _STRONG_SIGNAL_RES.items():
        if rex.search(low):
            tags.append(tag)
    if tags:
        score += 5
        real_signal = True

    # Personality / humor vocabulary. One STRONG word is enough on its own;
    # WEAK words only nudge. This is the whole fight against false positives:
    # a routine developer doc-comment ("Container that holds slides",
    # "Preload the fonts") has *none* of these and therefore scores 0 and
    # drops, even though a human wrote it - it just isn't interesting.
    strong_hits = len(_STRONG_DEV_RE.findall(low))
    if strong_hits:
        tags.append("dev-voice")
        score += min(3 * strong_hits, 6)
        # In strict mode, only non-jargon dev words count as a real signal.
        if _STRONG_DEV_STRICT_RE.search(low):
            real_signal = True
    score += min(len(_WEAK_DEV_RE.findall(low)), 2)

    has_decoration = False
    if EMOTICON_RE.search(" " + collapsed + " ") or has_meaningful_emoji(text):
        tags.append("emoticon")
        score += 3
        real_signal = True
        has_decoration = True

    # Excitement/frustration punctuation and first-person voice - weak on
    # their own (so they can't carry a boring sentence past the bar), but
    # they tip a borderline-human comment over.
    if re.search(r"\?!|!\?|\?\?|!!", text):
        score += 2
    elif "!" in text:
        score += 1
    if re.search(r"\bi\b|\bi'm\b|\bim\b|\bmy\b|\bme\b|\bmyself\b", low):
        score += 1

    # Emoji/emoticon on a bare structural label ("<!-- footer 🔚 -->") is
    # decoration, not a note: if that is the ONLY thing carrying it and every
    # real word is structural boilerplate, drop it.
    if has_decoration and "dev-voice" not in tags and not any(
            t in tags for t in _STRONG_SIGNAL_RES):
        words = [w for w in re.findall(r"[a-z']{2,}", low) if w not in ("the", "a", "an", "of", "and")]
        if words and all(w in STRUCTURAL_WORDS for w in words):
            return None

    if strict and not real_signal:
        return None
    if score < KEEP_THRESHOLD:
        return None
    return ("html_comment", score, tags or ["note"])


# Inline console.* greetings (console.log("👋 we're hiring!"), the %c
# branded banners, self-XSS "Stop!" warnings...).
_CONSOLE_RE = re.compile(
    r"""console\.(?:log|info|warn|error)\s*\(\s*(['"`])(.*?)\1""",
    re.DOTALL,
)


# Console output is overwhelmingly operational logging, not Easter eggs.
# These substrings mark a message as routine log/debug/error noise - drop it.
_CONSOLE_NOISE = (
    "error", "failed", "fail ", "unable", "could not", "couldn't", "cannot",
    "can't", "not found", "not supported", "not defined", "not available",
    "undefined", "null", "invalid", "missing", "deprecated", "warning:",
    "exception", "retry", "timeout", "timed out", "loading", "loaded",
    "init", "debug", "trace", "stack", "fetch", "parse", "parsing",
    "hydrat", "assigning", "connect", "disconnect", "websocket",
    "service worker", "script load", "request", "response", "unexpected",
    # Emoji-prefixed debug logging is rampant in modern JS - an emoji is not an
    # egg when the line is clearly operational.
    "enabled", "disabled", "destroy", "fired", "logged in", "logged out",
    "listener", "registered", "mounted", "rendered", "consent",
)
# ...and these mark one as a deliberate greeting/egg - keep it.
_CONSOLE_EGG_WORDS = (
    "hiring", "we're hiring", "we are hiring", "join us", "join the",
    "careers", "jobs@", "come work", "work with us", "self-xss", "self xss",
    "don't paste", "do not paste", "stop!", "scam", "attackers",
    "welcome to", "hello there", "psst", "easter egg", "konami",
    "nothing to see here", "curious", "snoop", "we love", "built with",
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
            clean = re.sub(r"%[a-z]", "", msg).strip()
            low = clean.lower()
            if len(clean) < 8:
                continue
            if "${" in msg or "' +" in msg or '" +' in msg or "`+" in msg:
                continue                                   # logging a variable
            if any(n in low for n in _CONSOLE_NOISE):
                continue                                   # routine log line
            if clean.endswith(":") and not any(k in low for k in _CONSOLE_EGG_WORDS):
                continue             # "🎨 Updating CSS variable:" - logs a value
            styled = "%c" in msg
            keep = (
                any(k in low for k in _CONSOLE_EGG_WORDS)
                or has_meaningful_emoji(clean)
                or (styled and re.search(r"[a-z]{3,}\s+[a-z]{3,}", low)
                    and any(k in low for k in ("welcome", "hello", "hey ", "we ",
                                               "you ", "job", "hire", "made", "love")))
            )
            if keep:
                out.append(clean)
    return out


# Response headers worth keeping: the known memorials/jokes, plus any odd
# X- header whose value reads like a sentence (and isn't boring plumbing).
BORING_HEADER_VALUE_HINTS = ("gzip", "max-age", "charset", "nosniff", "deny",
                             "sameorigin", "no-cache", "bytes", "keep-alive",
                             "miss", "hit", "stale", "bypass", "tcp_", "akamai",
                             "deploy", "varnish", "cloudfront", "fastly", "pop",
                             "edge", "origin", "upstream")
BORING_HEADERS = {
    "x-powered-by", "x-frame-options", "x-content-type-options",
    "x-xss-protection", "x-cache", "x-served-by", "x-request-id",
    "x-runtime", "x-ua-compatible", "x-aspnet-version", "x-aspnetmvc-version",
    "x-varnish", "x-timer", "x-cache-hits", "x-fastly-request-id",
    "x-amz-cf-id", "x-amz-cf-pop", "x-amz-request-id", "x-github-request-id",
    "x-cdn", "x-edge-location", "x-download-options", "x-permitted-cross-domain-policies",
    "x-dns-prefetch-control", "x-redirect-by", "x-pingback", "x-robots-tag",
    "x-cache-remote", "x-ac", "x-nc", "x-served", "x-server", "x-backend",
    "x-cache-status", "x-cacheable", "x-proxy-cache", "x-rq", "x-litespeed-cache",
    "x-repository-download", "x-drupal-cache", "x-drupal-dynamic-cache",
}
# Whole families of plumbing headers (AWS, Drupal, Akamai) - reject by prefix
# so we don't have to enumerate every variant (x-amz-id-2, x-amz-version-id...).
BORING_HEADER_PREFIXES = ("x-amz-", "x-drupal-", "x-akamai-")
# Header NAME fragments that mark a "what built/serves this" plumbing header
# (x-generator, x-built-with, x-flow-powered, x-device-type, x-region...). These
# often carry a product/CMS/server NAME that reads like a couple of words, so
# they sneak past the value check - but only when the value is short and
# technical. A genuinely human value (the famous TextDrive x-powered-by: "The
# blood, sweat and tears of the fine, fine staff") is longer, so gate on length.
BORING_HEADER_NAME_HINTS = ("generator", "built-with", "device-type", "device",
                            "region", "powered", "flying-press", "fragment",
                            "flow-powered", "framework", "product", "proxy-by",
                            "ssr-error", "-nid", "upstream", "instance")


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
        if any(low_name.startswith(p) for p in BORING_HEADER_PREFIXES):
            continue
        v = value.strip()
        vl = v.lower()
        if any(b in vl for b in BORING_HEADER_VALUE_HINTS):
            continue
        if any(h in low_name for h in BORING_HEADER_NAME_HINTS) and len(v.split()) <= 5:
            continue        # generator/built-with/device/powered-by-product plumbing
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
    # Keep it if it's a real credit OR if it's ASCII art (some sites put a big
    # figlet banner in /humans.txt - that's a find even with no prose credit).
    if not humans_has_content(text) and not detect_ascii_art(text):
        return None
    return text[:4000]


# Matches the standard humanstxt.org skeleton lines so we can tell whether a
# dev actually filled it in or just uploaded the blank template.
_HUMANS_BOILER = frozenset((
    "team", "thanks", "site", "standards", "technology", "components",
    "software", "language", "doctype", "ide", "the humans responsible & technology colophon",
    "the humans responsible & colophon", "humanstxt.org",
))


def humans_has_content(text):
    """A humans.txt is a real credit (not a blank/placeholder stub) when, after
    stripping the humanstxt.org boilerplate and comment markers, some genuine
    text remains and no unfilled <placeholder> tokens are left in it."""
    low_all = text.lower()
    # Binary/junk served as humans.txt (e.g. a GIF canary token).
    if text[:6] in ("GIF89a", "GIF87a") or text[:4] == "%PDF" or text.startswith("\x89PNG"):
        return False
    # Unfilled template placeholders (<name>, <role>, <your name>, <link>...).
    if re.search(r"<\s*(name|role|link|date|email|site|url|your|twitter|title)\b",
                 text, re.IGNORECASE):
        return False
    meaningful = []
    for ln in text.splitlines():
        s = re.sub(r"^[#/*\s]+", "", ln)
        s = re.sub(r"[*/\s]+$", "", s).strip()
        if not s:
            continue
        low = s.lower()
        if "humanstxt.org" in low or low in _HUMANS_BOILER:
            continue
        meaningful.append(s)
    joined = " ".join(meaningful)
    return len(joined) >= 12 and len(re.findall(r"[A-Za-z]{2,}", joined)) >= 2


# --------------------------------------------------------------------------
# Extra senses (Batch 3): CSS/JS source comments, custom 404 pages,
# security.txt, and (opt-in) DNS TXT records. They reuse the HTML-comment
# classifier so all the anti-false-positive work carries straight over.
# --------------------------------------------------------------------------

# Optional: people occasionally tuck a joke into a DNS TXT record. Needs
# dnspython; if it isn't installed the feature just no-ops (see --dns-txt).
try:
    import dns.resolver as _dns_resolver
except Exception:
    _dns_resolver = None


def _fetch_text(session, url, timeout, max_bytes=512 * 1024):
    """Fetch a URL as decoded text with a hard size cap (for CSS/JS assets
    and probe pages). Returns text or None. Streams so a giant minified
    bundle can't blow up memory."""
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        with session.get(url, headers=headers, timeout=timeout,
                          allow_redirects=True, stream=True) as resp:
            if resp.status_code >= 400:
                return None
            body = bytearray()
            for chunk in resp.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > max_bytes:
                    break
            return _decode_body(body, resp.headers.get("Content-Type", ""))
    except Exception:
        return None


def classify_foreign_comment(body, kind_label):
    """Score a comment lifted from a CSS or JS file by reusing the HTML
    comment classifier (same boilerplate/library-banner/personality rules),
    then relabel. ASCII art stays ascii_art whatever file it came from."""
    verdict = classify_comment(body, strict=True)
    if verdict is None:
        return None
    kind, score, tags = verdict
    return ("ascii_art", score, tags) if kind == "ascii_art" else (kind_label, score, tags)


_CSS_JS_BLOCK_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)
# Only whole-line // comments for JS - avoids matching URLs (http://) and
# inline-after-code noise. 3-300 chars of content.
_JS_LINE_RE = re.compile(r"(?m)^[ \t]*//[ \t]?(.{3,300})$")


def extract_asset_comments(text, kind_label):
    """Yield (kind, out_text, score, tags) for interesting comments in a CSS
    or JS file. Block comments for both; whole-line // comments for JS too."""
    seen_local = set()
    bodies = [m.group(1) for m in _CSS_JS_BLOCK_RE.finditer(text)]
    if kind_label == "js_comment":
        bodies += [m.group(1) for m in _JS_LINE_RE.finditer(text)]
    for body in bodies:
        v = classify_foreign_comment(body, kind_label)
        if v is None:
            continue
        kind, score, tags = v
        out_text = normalize_ascii(body) if kind == "ascii_art" else _collapse(body)
        dedup = kind + "\x00" + out_text.lower()[:160]
        if dedup in seen_local:
            continue
        seen_local.add(dedup)
        yield kind, out_text, score, tags


_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
# A 404 page is only worth keeping if it has genuine CHARACTER - every 404 says
# "sorry / page not found / oops / we can't find it", so those generic phrases
# are NOT a signal (they were the #1 false-positive source). Keep only the
# distinctive, written-by-a-person turns of phrase.
_DISTINCTIVE_404 = (
    "here be dragons", "tumbleweed", "ghost town", "the void", "the abyss",
    "not all who wander", "lost in space", "wandered off", "you broke",
    "took a wrong", "wrong turn", "404 but make it", "off the map", "dead end",
    "uh oh", "uh-oh", "vanished", "misplaced", "you're lost", "youre lost",
    "nothing to see here", "tragically", "get here", "or is there",
    "black hole", "gone fishing", "under the couch", "into the ether",
)
_404_KEEP_RE = _kw_re(_DISTINCTIVE_404)
_PROBE_PATH_RE = re.compile(r"/vort-not-here-[0-9a-f]+")


def probe_custom_404(session, scheme, host, timeout):
    """Request a guaranteed-missing URL; keep the 404 page only if it has
    personality - ASCII art in a <pre>, or a human/jokey message. Returns a
    list of (kind, text, score, tags)."""
    token = "%08x" % random.getrandbits(32)
    url = f"{scheme}://{host}/vort-not-here-{token}"
    text = _fetch_text(session, url, timeout, max_bytes=256 * 1024)
    if not text or "<html" not in text.lower():
        return []
    out = []
    for pre in _PRE_RE.findall(text):
        inner = html_module.unescape(re.sub(r"<[^>]+>", "", pre))
        if detect_ascii_art(inner):
            out.append(("ascii_art", normalize_ascii(inner), 6, ["ascii-art", "404"]))
            break
    try:
        vis = _collapse(BeautifulSoup(text, "html.parser").get_text(" "))
    except Exception:
        vis = ""
    # Don't let our own probe path / a reflected-request artifact leak into the
    # stored find (it also made otherwise-identical 404s look unique).
    vis = _PROBE_PATH_RE.sub("/…", vis).replace('<!-- "> \'> -->', "").strip()
    if 15 <= len(vis) <= 600 and _404_KEEP_RE.search(vis.lower()):
        out.append(("custom_404", vis[:400], 5, ["404"]))
    return out


def probe_security_txt(session, scheme, host, timeout):
    """Fetch security.txt; keep it only if it carries something human - ASCII
    art, or comment (#) lines that read like a person wrote them - not the
    bare Contact/Expires boilerplate."""
    for path in ("/.well-known/security.txt", "/security.txt"):
        text = _fetch_text(session, f"{scheme}://{host}{path}", timeout, max_bytes=64 * 1024)
        if not text:
            continue
        low = text.lower()
        if "<html" in low or "contact:" not in low:
            continue
        if detect_ascii_art(text):
            return ("security_txt", normalize_ascii(text)[:2000], 5, ["security.txt", "ascii-art"])
        comments = [ln.lstrip("#").strip() for ln in text.splitlines()
                    if ln.lstrip().startswith("#") and len(ln.strip()) > 4]
        if comments:
            v = classify_comment(" ".join(comments))
            if v is not None:
                return ("security_txt", text.strip()[:2000], max(4, v[1]), ["security.txt"] + v[2])
        return None
    return None


# TXT records that are machine config, not a message - never interesting.
_DNS_NOISE_PREFIXES = (
    "v=spf1", "v=dkim1", "v=dmarc1", "google-site-verification", "ms=",
    "facebook-domain-verification", "apple-domain-verification", "docusign=",
    "stripe-verification", "adobe-idp-site-verification", "atlassian-domain",
    "have-i-been-pwned-verification", "amazonses:", "ca3-", "mongodb-site",
    "zoom-domain-verification", "p=mig", "onetrust-domain", "logmein-verification",
)


def probe_dns_txt(host):
    """Query the host's TXT records and keep any that read like a human
    message (greeting/personality/emoji) rather than machine config. No-op
    without dnspython. Returns a list of (kind, text, score, tags)."""
    if _dns_resolver is None:
        return []
    name = host.split(":")[0]
    try:
        answers = _dns_resolver.resolve(name, "TXT", lifetime=6)
    except Exception:
        return []
    out = []
    for rdata in answers:
        try:
            val = b"".join(getattr(rdata, "strings", [])).decode("utf-8", "replace")
        except Exception:
            val = str(rdata).strip('"')
        val = val.strip()
        low = val.lower()
        if not val or any(low.startswith(p) for p in _DNS_NOISE_PREFIXES) or "verification" in low:
            continue
        if (has_emoji(val) or _STRONG_DEV_RE.search(low)
                or any(r.search(low) for r in _STRONG_SIGNAL_RES.values())):
            out.append(("dns_txt", val[:400], 5, ["dns-txt"]))
    return out


# --------------------------------------------------------------------------
# Output: durable JSONL + plain-text log + the HTML gallery
# --------------------------------------------------------------------------

KIND_LABELS = {
    "html_comment": "HTML comment",
    "ascii_art": "ASCII art",
    "console": "console message",
    "header": "jokey header",
    "humans_txt": "humans.txt",
    "css_comment": "CSS comment",
    "js_comment": "JS comment",
    "custom_404": "404 page",
    "security_txt": "security.txt",
    "dns_txt": "DNS TXT",
}


def _find_key(kind, text, domain=""):
    # humans.txt files share the standard "# humanstxt.org/ #..." template
    # header, so a text-prefix key would collide across different sites and
    # throw away real credits. Dedup it per domain instead (one per site).
    if kind == "humans_txt":
        return "humans_txt\x00" + domain
    return _find_key_text(kind, text)


def _find_key_text(kind, text):
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
        self.by_key = {}          # find-key -> its rec (first sighting)
        self.seen_domains = {}    # find-key -> set of distinct domains seen on
        self.seen_path = outdir / ".chunt_seen_domains.json"
        if fresh and self.seen_path.is_file():
            self.seen_path.unlink()

        if self.jsonl_path.is_file():
            for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = _find_key(rec["kind"], rec["text"], rec.get("domain", ""))
                self.seen.add(key)
                self.finds.append(rec)
                self.by_key[key] = rec
                self.seen_domains.setdefault(key, set()).add(rec.get("domain", ""))
                self.per_domain[rec["domain"]] = self.per_domain.get(rec["domain"], 0) + 1

        # "Seen on N sites": the sidecar accumulates every distinct domain a
        # find turned up on across runs - cross-domain repeats (an X-Clacks
        # header on dozens of sites) would otherwise just dedup away to one.
        if self.seen_path.is_file():
            try:
                saved = json.loads(self.seen_path.read_text(encoding="utf-8"))
                for key, doms in saved.items():
                    self.seen_domains.setdefault(key, set()).update(doms)
            except Exception:
                pass
        for key, rec in self.by_key.items():
            rec["seen_on"] = len(self.seen_domains.get(key) or {rec.get("domain", "")})

        self.jsonl_f = self.jsonl_path.open("a", encoding="utf-8")
        self.log_f = self.log_path.open("a", encoding="utf-8")
        self.added_this_run = 0

    def already_have(self, kind, text, domain=""):
        return _find_key(kind, text, domain) in self.seen

    def domain_count(self, domain):
        return self.per_domain.get(domain, 0)

    def add(self, kind, text, url, domain, score, tags):
        key = _find_key(kind, text, domain)
        if key in self.seen:
            # Already recorded - just note we've now seen it on another site.
            doms = self.seen_domains.setdefault(key, set())
            n0 = len(doms)
            doms.add(domain)
            if len(doms) != n0 and key in self.by_key:
                self.by_key[key]["seen_on"] = len(doms)
            return False
        self.seen.add(key)
        self.seen_domains.setdefault(key, set()).add(domain)
        rec = {
            "kind": kind, "text": text, "url": url, "domain": domain,
            "score": score, "tags": tags,
            "seen_on": len(self.seen_domains[key]),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self.finds.append(rec)
        self.by_key[key] = rec
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
        try:
            self.seen_path.write_text(
                json.dumps({k: sorted(v) for k, v in self.seen_domains.items()},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception as exc:
            print(f"  [SEEN-SIDECAR ERROR] {exc}")
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
        kind = rec["kind"]
        label = _esc(KIND_LABELS.get(kind, kind)).upper()
        ts = str(rec.get("ts", ""))
        when = _esc(ts.replace("T", " ")[:16]) or "?"   # 2026-10-28 20:15
        body = fix_mojibake(rec["text"])
        if kind == "ascii_art":
            body = normalize_ascii(body)
        seen_on = int(rec.get("seen_on", 1) or 1)
        seen_html = f'<span class="seen">seen on {seen_on} sites</span>' if seen_on > 1 else ""
        return (
            f'<div class="card k-{_esc(kind)}" data-kind="{_esc(kind)}" '
            f'data-score="{_esc(rec.get("score", 0))}" data-ts="{_esc(ts)}">'
            f'<div class="meta"><span class="kind">[{label}]</span>{tags}'
            f'<span class="score">::{_esc(rec.get("score", ""))}</span></div>'
            f'<pre>{_esc(body)}</pre>'
            f'<div class="src"><span class="arr">&gt;&gt;</span> '
            f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{host}</a>'
            f'{seen_html}'
            f'<span class="when">found {when}</span></div>'
            '</div>'
        )

    # One list, sorted best-first by default; the page re-sorts/filters it live.
    ordered = sorted(finds, key=lambda r: (-r.get("score", 0), r.get("ts", "")))
    cards = ("".join(card(r) for r in ordered)
             or '<p class="empty">no loot yet &mdash; let it crawl_</p>')

    chips = [f'<button class="chip active" type="button" data-filter="all">all'
             f' <span class="cn">{total}</span></button>']
    for kind, label in KIND_LABELS.items():
        n = len(by_kind.get(kind, []))
        if n:
            chips.append(f'<button class="chip" type="button" data-filter="{_esc(kind)}">'
                         f'{_esc(label)} <span class="cn">{n}</span></button>')
    chips_html = "".join(chips)

    counts = " // ".join(
        f"{len(v)} {KIND_LABELS.get(k, k)}" for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
    )
    domains = len({r["domain"] for r in finds})
    generated = datetime.now().isoformat(timespec="seconds")

    return _GALLERY_TEMPLATE.replace("__TOTAL__", str(total)) \
        .replace("__DOMAINS__", str(domains)) \
        .replace("__COUNTS__", _esc(counts)) \
        .replace("__GENERATED__", _esc(generated)) \
        .replace("__CHIPS__", chips_html) \
        .replace("__CARDS__", cards)


_GALLERY_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>v0rt // loot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%23040804'/%3E%3Ctext x='1' y='12.5' font-family='monospace' font-size='12' font-weight='bold' fill='%2300ff66'%3Ev0%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg:#040804; --green:#00ff66; --green2:#19ff7a; --dim:#0b7a3a;
    --ink:#c6ffd9; --amber:#ffb62e; --cyan:#3bf7e4; --magenta:#ff5fd2;
    --mono:"Share Tech Mono",ui-monospace,"Consolas","Courier New",monospace;
    --glow:0 0 4px rgba(0,255,102,.45);
  }
  * { box-sizing:border-box; }
  html,body { margin:0; background:var(--bg); }
  body { color:var(--ink); font:16px/1.5 var(--mono);
    text-shadow:var(--glow); min-height:100vh; }
  a { color:var(--cyan); }

  /* CRT scanlines (static) + a slow bright scan sweep + a faint flicker -
     all fixed, non-interactive overlays. This replaced the matrix rain. */
  body::after { content:""; position:fixed; inset:0; z-index:60; pointer-events:none;
    background:repeating-linear-gradient(to bottom,
      rgba(0,0,0,0) 0, rgba(0,0,0,0) 2px, rgba(0,0,0,.24) 3px, rgba(0,0,0,0) 4px);
    mix-blend-mode:multiply; }
  body::before { content:""; position:fixed; inset:0; z-index:59; pointer-events:none;
    background:rgba(0,255,100,.015); animation:flick 7s infinite steps(1); }
  @keyframes flick { 0%,92%{opacity:.5} 93%{opacity:1} 94%{opacity:.25} 96%{opacity:.8} 100%{opacity:.5} }
  .sweep { position:fixed; left:0; right:0; top:0; height:150px; z-index:58; pointer-events:none;
    background:linear-gradient(to bottom, rgba(130,255,180,0), rgba(130,255,180,.055), rgba(130,255,180,0));
    transform:translateY(-150px); animation:sweep 8s linear infinite; }
  @keyframes sweep { 0%{transform:translateY(-150px)} 100%{transform:translateY(100vh)} }

  .wrap { position:relative; z-index:1; max-width:1000px; margin:0 auto; padding:22px 16px 90px; }

  /* terminal header */
  .term { border:1px solid var(--dim); border-radius:6px; overflow:hidden;
    background:rgba(2,14,6,.78); margin-bottom:26px; box-shadow:0 0 24px rgba(0,255,102,.08); }
  .titlebar { display:flex; align-items:center; gap:7px; padding:7px 11px;
    background:rgba(0,255,102,.08); border-bottom:1px solid var(--dim); font-size:13px; color:var(--green2); }
  .dot { width:11px; height:11px; border-radius:50%; display:inline-block; }
  .dot.r{background:#ff5f56} .dot.y{background:#ffbd2e} .dot.g{background:#27c93f}
  .titlebar .tt { margin-left:8px; color:var(--dim); }
  .termbody { padding:16px 16px 18px; }
  .line { color:var(--green2); font-size:14px; }
  .line .typed { color:var(--ink); }
  .cursor { display:inline-block; width:.6ch; background:var(--green); color:var(--green);
    animation:blink 1.05s steps(1) infinite; }
  @keyframes blink { 50%{opacity:0} }
  h1.glitch { position:relative; margin:10px 0 6px; letter-spacing:2px; line-height:1;
    font-size:clamp(30px,7.5vw,54px); color:var(--green);
    text-shadow:0 0 8px rgba(0,255,102,.6); }
  h1.glitch::before, h1.glitch::after { content:attr(data-text); position:absolute; left:0; top:0;
    width:100%; overflow:hidden; }
  h1.glitch::before { color:var(--cyan); clip-path:inset(0 0 58% 0); animation:gl1 3.4s infinite linear alternate; }
  h1.glitch::after { color:var(--magenta); clip-path:inset(58% 0 0 0); animation:gl2 2.7s infinite linear alternate; }
  @keyframes gl1 { 0%,88%{transform:translateX(0)} 90%{transform:translateX(-2px)} 93%{transform:translateX(2px)} 100%{transform:translateX(0)} }
  @keyframes gl2 { 0%,85%{transform:translateX(0)} 88%{transform:translateX(2px)} 92%{transform:translateX(-1px)} 100%{transform:translateX(0)} }
  .sub { color:var(--dim); font-size:14px; margin:0 0 12px; }
  .stat { color:var(--green2); font-size:13px; margin:0; }
  .stat b { color:var(--amber); }

  /* command-style section label */
  h2 { font-size:15px; margin:28px 0 12px; font-weight:400; color:var(--ink); }
  h2 .pr { color:var(--green); } h2 .pa { color:var(--cyan); }
  h2 .cmd { color:var(--amber); }

  /* filter + sort toolbar */
  .toolbar { display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin:0 0 10px; }
  .tlbl { color:var(--dim); font-size:13px; }
  .chip, .sortbtn { cursor:pointer; font:13px var(--mono); color:var(--green2);
    background:rgba(2,16,7,.7); border:1px solid var(--dim); border-radius:4px;
    padding:4px 10px; text-shadow:var(--glow); }
  .chip:hover, .sortbtn:hover { border-color:var(--green); }
  .chip.active { background:var(--green); color:#03230f; border-color:var(--green); text-shadow:none; }
  .chip .cn { color:var(--dim); } .chip.active .cn { color:#03230f; }
  .sortbtn.active { color:var(--amber); border-color:var(--amber); }
  .showing { margin-left:auto; color:var(--dim); font-size:12.5px; }
  .morewrap { text-align:center; margin:22px 0 0; }
  .morebtn { cursor:pointer; font:14px var(--mono); color:var(--green2);
    background:rgba(2,16,7,.7); border:1px solid var(--dim); border-radius:4px;
    padding:9px 24px; text-shadow:var(--glow); }
  .morebtn:hover { border-color:var(--green); color:var(--green); }
  .morebtn[hidden] { display:none; }

  .grid { display:grid; grid-template-columns:minmax(0,1fr); gap:12px; }
  .card { min-width:0; border:1px solid var(--dim); border-left:3px solid var(--green);
    background:rgba(2,16,7,.72); border-radius:4px; padding:11px 13px; }
  .meta { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:7px; font-size:12px; }
  .kind { color:var(--green); letter-spacing:.04em; }
  .tag { border:1px solid var(--dim); color:var(--cyan); border-radius:3px; padding:0 7px; font-size:11px; }
  .score { margin-left:auto; color:var(--dim); }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font:15px/1.5 var(--mono); color:var(--ink); }
  .k-ascii_art pre { white-space:pre; word-break:normal; overflow-x:auto; tab-size:8;
    font-size:13px; line-height:1.2; padding-bottom:4px; }
  .k-ascii_art pre::-webkit-scrollbar { height:8px; }
  .k-ascii_art pre::-webkit-scrollbar-thumb { background:var(--dim); border-radius:4px; }
  .src { margin-top:8px; font-size:12.5px; display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
  .src .arr { color:var(--green); }
  .src a { text-decoration:none; } .src a:hover { text-decoration:underline; }
  .src .when { margin-left:auto; color:var(--dim); }
  .src .seen { color:var(--amber); font-size:11px; border:1px solid var(--dim); border-radius:3px; padding:0 6px; }
  .empty { color:var(--dim); }
  /* kind accents */
  .k-header { border-left-color:var(--amber); } .k-header .kind { color:var(--amber); }
  .k-humans_txt { border-left-color:var(--cyan); } .k-humans_txt .kind { color:var(--cyan); }
  .k-ascii_art { border-left-color:var(--magenta); } .k-ascii_art .kind { color:var(--magenta); }
  .k-console { border-left-color:var(--cyan); }
  .k-css_comment { border-left-color:var(--cyan); } .k-css_comment .kind { color:var(--cyan); }
  .k-js_comment { border-left-color:var(--amber); } .k-js_comment .kind { color:var(--amber); }
  .k-custom_404 { border-left-color:var(--magenta); } .k-custom_404 .kind { color:var(--magenta); }
  .k-security_txt { border-left-color:var(--amber); } .k-security_txt .kind { color:var(--amber); }
  .k-dns_txt { border-left-color:var(--cyan); } .k-dns_txt .kind { color:var(--cyan); }

  /* boot overlay (shown by JS only; invisible without JS) */
  #boot { display:none; position:fixed; inset:0; z-index:90; background:#020402;
    align-items:center; justify-content:center; cursor:pointer; }
  #boot.gone { opacity:0; transition:opacity .5s; }
  #bootlog { color:var(--green); font:15px/1.7 var(--mono); text-shadow:var(--glow);
    white-space:pre; padding:20px; }

  @media (prefers-reduced-motion: reduce) {
    body::before, .cursor, h1.glitch::before, h1.glitch::after, .sweep { animation:none; }
    h1.glitch::before, h1.glitch::after, .sweep { display:none; }
  }
</style></head>
<body>
<div class="sweep"></div>
<div id="boot"><pre id="bootlog"></pre></div>

<div class="wrap">
  <div class="term">
    <div class="titlebar"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
      <span class="tt">root@v0rt: ~/finds &mdash; 80x24</span></div>
    <div class="termbody">
      <div class="line"><span class="pr" style="color:var(--green)">visitor@v0rt</span>:<span style="color:var(--cyan)">~</span>$ <span class="typed">./v0rt --show-loot</span></div>
      <h1 class="glitch" data-text="v0rt.com">v0rt.com</h1>
      <p class="sub">// human things hiding in the source of the open web</p>
      <p class="stat">[ <b>__TOTAL__</b> finds ] [ <b>__DOMAINS__</b> domains ] [ __COUNTS__ ] [ last sync __GENERATED__ ]<span class="cursor">&nbsp;</span></p>
    </div>
  </div>

  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">./loot --browse</span></h2>
  <div class="toolbar"><span class="tlbl">filter:</span>__CHIPS__</div>
  <div class="toolbar"><span class="tlbl">sort:</span>
    <button class="sortbtn active" type="button" data-sort="score">score</button>
    <button class="sortbtn" type="button" data-sort="newest">newest</button>
    <button class="sortbtn" type="button" data-sort="oldest">oldest</button>
    <span class="showing" id="showing">showing __TOTAL__</span>
  </div>
  <div class="grid" id="grid">__CARDS__</div>
  <div class="morewrap"><button class="morebtn" id="more" type="button" hidden>load more</button></div>
</div>

<script>
(function(){
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // --- boot sequence (plays once per session, skippable) ---
  var boot = document.getElementById('boot'), log = document.getElementById('bootlog');
  function endBoot(){ if(!boot) return; boot.classList.add('gone');
    setTimeout(function(){ if(boot && boot.parentNode) boot.parentNode.removeChild(boot); }, 550); }
  if (reduce || sessionStorage.getItem('chunt_booted')) {
    /* leave boot hidden */
  } else {
    sessionStorage.setItem('chunt_booted','1');
    boot.style.display = 'flex';
    var lines = ["> INITIALIZING UPLINK ...",
                 "> BYPASSING ICE .............. [ OK ]",
                 "> ACCESSING MAINFRAME ........ [ OK ]",
                 "> DECRYPTING finds.log ....... [ OK ]",
                 "> LOOT RECOVERED: __TOTAL__ ENTRIES",
                 "> CONNECTION ESTABLISHED_"];
    var i=0, buf="";
    (function type(){
      if(i>=lines.length){ setTimeout(endBoot, 550); return; }
      buf += lines[i] + "\\n"; log.textContent = buf; i++; setTimeout(type, 250);
    })();
    boot.addEventListener('click', endBoot);
    window.addEventListener('keydown', endBoot, {once:true});
  }

  // --- live filter (by type) + sort (score / newest / oldest) ---
  var grid = document.getElementById('grid');
  var cards = [].slice.call(grid.querySelectorAll('.card'));
  var chips = [].slice.call(document.querySelectorAll('.chip'));
  var sorts = [].slice.call(document.querySelectorAll('.sortbtn'));
  var showing = document.getElementById('showing');
  var curFilter = 'all', curSort = 'score';
  function cmp(a, b){
    var ta = a.dataset.ts, tb = b.dataset.ts;
    if (curSort === 'score') { return (+b.dataset.score - +a.dataset.score) || (tb < ta ? -1 : tb > ta ? 1 : 0); }
    if (curSort === 'newest') { return tb < ta ? -1 : tb > ta ? 1 : 0; }
    return ta < tb ? -1 : ta > tb ? 1 : 0;   // oldest
  }
  var moreBtn = document.getElementById('more');
  var PAGE = 48, shown = PAGE;
  function apply(){
    var arr = cards.slice().sort(cmp);
    var matched = 0, visible = 0, frag = document.createDocumentFragment();
    arr.forEach(function(c){
      var inFilter = (curFilter === 'all' || c.dataset.kind === curFilter);
      if (inFilter && matched < shown) { c.style.display = ''; visible++; }
      else { c.style.display = 'none'; }
      if (inFilter) matched++;
      frag.appendChild(c);
    });
    grid.appendChild(frag);
    if (showing) showing.textContent = 'showing ' + visible + (matched > visible ? ' of ' + matched : '');
    if (moreBtn) {
      var rest = matched - visible;
      moreBtn.hidden = rest <= 0;
      if (rest > 0) moreBtn.textContent = 'load more (' + rest + ')';
    }
  }
  chips.forEach(function(ch){ ch.addEventListener('click', function(){
    chips.forEach(function(x){ x.classList.remove('active'); }); ch.classList.add('active');
    curFilter = ch.dataset.filter; shown = PAGE; apply();
  }); });
  sorts.forEach(function(b){ b.addEventListener('click', function(){
    sorts.forEach(function(x){ x.classList.remove('active'); }); b.classList.add('active');
    curSort = b.dataset.sort; shown = PAGE; apply();
  }); });
  if (moreBtn) moreBtn.addEventListener('click', function(){ shown += PAGE; apply(); });
  apply();
})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# Public export - redact before anything is shared, and never publish a find
# that contained a real secret.
# --------------------------------------------------------------------------

# High-confidence credential shapes. A find containing any of these is
# DROPPED from the public export entirely - we don't spotlight someone's
# leak, even masked.
_SECRET_RES = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"sk_live_[0-9a-zA-Z]{16,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,48}"),
    re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PGP)? ?PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
               r"password|passwd)\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{12,}['\"]"),
]
# Masked-in-place (the find is still published, just scrubbed).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def redact_text(text):
    """Returns (redacted_text, had_secret). Emails keep only their domain,
    IPv4s become [ip]; had_secret flags a find that should not be published
    at all."""
    had_secret = any(r.search(text) for r in _SECRET_RES)
    red = _EMAIL_RE.sub(lambda m: "[redacted]@" + m.group(1), text)
    red = _IPV4_RE.sub("[ip]", red)
    return red, had_secret


# A UTF-8 lead byte (0xC2-0xF4 -> chars À-ô) immediately followed by a
# continuation byte (0x80-0xBF) is the unmistakable signature of UTF-8 text
# that was decoded as Latin-1 - i.e. mojibake. Real text essentially never
# has an accented capital/letter followed by a C1 control char.
_MOJIBAKE_RE = re.compile("[Â-ô][\u0080-¿]")


def fix_mojibake(text):
    """Repair text already stored with the ISO-8859-1 decode bug (e.g. a
    find showing 'â\\x80\\x94psst' instead of '-psst'). No-op on clean text."""
    if not _MOJIBAKE_RE.search(text):
        return text
    for enc in ("latin-1", "cp1252"):
        try:
            fixed = text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if "�" not in fixed:
            return fixed
    return text


def public_id(rec):
    """Stable per-find id (survives re-exports) so likes persist on the
    public site when the dataset is refreshed."""
    key = _find_key(rec["kind"], rec["text"], rec.get("domain", ""))
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def count_domains_crawled(public_path):
    """Count the unique sites (host, www-folded) the crawler has actually
    visited, read from the visited log(s) sitting next to the published file
    (the main roaming crawl + an optional mysites/ scoped workspace). This is
    far larger than the number of domains that produced a find. Returns 0 if no
    visited log is found, so callers in other contexts degrade gracefully."""
    base = Path(public_path).resolve().parent
    doms = set()
    for p in (base / VISITED_NAME, base / "mysites" / VISITED_NAME):
        if not p.is_file():
            continue
        try:
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        host = urlparse(line).netloc.lower()
                    except Exception:
                        continue
                    if host.startswith("www."):
                        host = host[4:]
                    if host:
                        doms.add(host)
        except Exception:
            continue
    return len(doms)


def export_public(finds, path, cap=0):
    """Write a redacted, publish-safe JSON of the finds for the PHP site.

    Publishes ALL non-sensitive finds by default (cap=0) as a JSON object
    {total_collected, generated, finds}. The site shows the top 500 on the
    homepage and paginates the full set in /archive, so nothing is lost but
    no single page is heavy. `total_collected` is the true tally (every find
    v0rt logged, including ones dropped here as sensitive). A positive `cap`
    hard-limits the published `finds` list (by score) for file-size safety.
    Returns (kept, dropped, capped)."""
    out, dropped = [], 0
    for rec in finds:
        red, had_secret = redact_text(fix_mojibake(rec["text"]))
        if had_secret:
            dropped += 1
            continue
        if rec["kind"] == "ascii_art":
            red = normalize_ascii(red)
        out.append({
            "id": public_id(rec),
            "kind": rec["kind"],
            "text": red,
            "url": rec["url"],
            "domain": rec["domain"],
            "score": rec.get("score", 0),
            "tags": rec.get("tags", []),
            "ts": rec.get("ts", ""),
            "seen_on": int(rec.get("seen_on", 1) or 1),
        })
    out.sort(key=lambda r: (r["score"], r["ts"]), reverse=True)
    capped = max(0, len(out) - cap) if cap else 0
    if cap:
        out = out[:cap]
    payload = {
        "total_collected": len(finds),
        "domains_crawled": count_domains_crawled(path),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "finds": out,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return len(out), dropped, capped


# --------------------------------------------------------------------------
# Crawl loop (mirrors text_harvester.harvest)
# --------------------------------------------------------------------------

def _decode_body(body, content_type):
    """Decode page bytes without `requests`' bad habit of defaulting
    charset-less text/html to ISO-8859-1 - which silently turns a UTF-8
    em-dash (bytes E2 80 94) into mojibake like 'â\\x80\\x94'. Trust an
    explicit charset (header, then <meta>), otherwise assume UTF-8 and
    only fall back to cp1252 if UTF-8 genuinely doesn't decode."""
    m = re.search(r"charset=[\"']?\s*([\w\-]+)", content_type, re.I)
    charset = m.group(1).strip() if m else None
    if not charset:
        head = bytes(body[:4096]).decode("ascii", "ignore").lower()
        mm = re.search(r"<meta[^>]+charset=[\"']?\s*([\w\-]+)", head)
        if mm:
            charset = mm.group(1)
    if charset:
        try:
            return bytes(body).decode(charset, errors="replace")
        except (LookupError, TypeError):
            pass
    try:
        return bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(body).decode("cp1252", errors="replace")


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
        text = _decode_body(body, content_type)
    return final_url, status, content_type, resp_headers, text


def harvest_page(store, url, final_url, html, headers, session, args):
    timeout = args.timeout
    ignore_robots = args.ignore_robots
    max_finds_per_domain = args.max_finds_per_domain

    domain = urlparse(final_url).netloc
    if max_finds_per_domain and store.domain_count(domain) >= max_finds_per_domain:
        return None

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
        text = normalize_ascii(raw) if kind == "ascii_art" else _collapse(raw)
        store.add(kind, text, final_url, domain, score, tags)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    if soup is not None:
        for msg in extract_console_eggs(soup):
            if allowed():
                store.add("console", msg, final_url, domain, 5, ["console"])

    parsed = urlparse(final_url)
    host = parsed.netloc
    hbase = f"{parsed.scheme}://{host}"

    # humans.txt - once per host
    if not args.no_humans_txt:
        humans_url = f"{hbase}/humans.txt"
        if humans_url not in store_probed:
            store_probed.add(humans_url)
            if ignore_robots or robots_allows(humans_url, timeout):
                body = extract_humans_txt(session, parsed.scheme, host, timeout)
                if body and allowed():
                    store.add("humans_txt", body, humans_url, domain, 4, ["credits"])

    # Custom 404 page - probe one missing URL per host.
    if not args.no_404 and host not in probed_404:
        probed_404.add(host)
        if ignore_robots or robots_allows(f"{hbase}/vort-not-here", timeout):
            for kind, text, score, tags in probe_custom_404(session, parsed.scheme, host, timeout):
                if allowed():
                    store.add(kind, text, f"{hbase}/this-page-does-not-exist", host, score, tags)

    # security.txt - once per host.
    if not args.no_security_txt and host not in sec_probed:
        sec_probed.add(host)
        sec_url = f"{hbase}/.well-known/security.txt"
        if ignore_robots or robots_allows(sec_url, timeout):
            v = probe_security_txt(session, parsed.scheme, host, timeout)
            if v and allowed():
                store.add(v[0], v[1], sec_url, host, v[2], v[3])

    # First-party CSS/JS source comments - capped per host.
    if (not args.no_asset_comments and soup is not None
            and assets_probed.get(host, 0) < args.max_asset_fetches):
        wanted = []
        for tag in soup.find_all("link", href=True):
            rel = tag.get("rel") or []
            rel = " ".join(rel) if isinstance(rel, list) else str(rel)
            if "stylesheet" in rel.lower():
                wanted.append(("css_comment", tag["href"]))
        for tag in soup.find_all("script", src=True):
            wanted.append(("js_comment", tag["src"]))
        for kind_label, src in wanted:
            if assets_probed.get(host, 0) >= args.max_asset_fetches or not allowed():
                break
            try:
                asset_url = urljoin(final_url, src)
                if urlparse(asset_url).netloc != host:   # first-party only
                    continue
            except Exception:
                continue
            if asset_url in assets_fetched:
                continue                                 # no refetch per page
            if not (ignore_robots or robots_allows(asset_url, timeout)):
                continue
            assets_fetched.add(asset_url)
            assets_probed[host] = assets_probed.get(host, 0) + 1
            cap = 768 * 1024 if kind_label == "js_comment" else 256 * 1024
            body = _fetch_text(session, asset_url, timeout, max_bytes=cap)
            if not body:
                continue
            for kind, text, score, tags in extract_asset_comments(body, kind_label):
                if not allowed():
                    break
                store.add(kind, text, asset_url, host, score, tags)

    # DNS TXT records - opt-in, once per host.
    if args.dns_txt and host not in dns_probed:
        dns_probed.add(host)
        for kind, text, score, tags in probe_dns_txt(host):
            if allowed():
                store.add(kind, text, f"{hbase}/", host, score, tags)

    return soup


# Per-run, per-host probe bookkeeping (kept simple/global).
store_probed = set()      # humans.txt URLs probed
sec_probed = set()        # hosts whose security.txt we've probed
probed_404 = set()        # hosts whose custom 404 we've probed
dns_probed = set()        # hosts whose DNS TXT we've probed
assets_probed = {}        # host -> count of CSS/JS files fetched
assets_fetched = set()    # asset URLs already fetched (don't refetch per page)


def host_allowed(netloc, allow):
    """True if netloc belongs to the allowlist - matching an apex domain or any
    of its subdomains (manableed.com allows www.manableed.com, blog.manableed.com).
    An empty allowlist allows everything (the default roaming behaviour)."""
    if not allow:
        return True
    nl = (netloc or "").lower()
    if nl.startswith("www."):
        nl = nl[4:]
    return any(nl == d or nl.endswith("." + d) for d in allow)


def harvest(seeds, args, store):
    allow = getattr(args, "only_domains", None) or set()
    session = requests.Session()
    visited = load_visited(args.output_dir)
    per_domain_pages = {}
    rooted = set()          # domains whose homepage we've already queued
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

            try:
                netloc = urlparse(url).netloc
            except Exception:
                visited.add(url)          # malformed URL in the frontier - skip it
                continue
            if not host_allowed(netloc, allow):   # --only-domains: stay on these sites
                visited.add(url)
                continue
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

            # Easter eggs cluster on a site's main page, but we often arrive
            # at a domain via a deep link. The first time we touch any
            # domain, jump its homepage to the FRONT of the queue so it's
            # crawled next - otherwise, with a low per-domain cap, we might
            # spend the whole budget on article pages and never see it.
            if netloc not in rooted:
                rooted.add(netloc)
                root = normalize_for_frontier(f"{urlparse(url).scheme}://{netloc}/")
                if root != url and root not in visited:
                    queue.appendleft(root)

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

            try:
                soup = harvest_page(store, url, final_url, text, resp_headers, session, args)
            except Exception as exc:
                print(f"  [HARVEST ERROR] {final_url}: {exc}")
                soup = None

            if soup is not None:
                try:
                    page_links, _resources = extract_links(final_url, soup)
                except Exception as exc:
                    print(f"  [LINK-PARSE ERROR] {final_url}: {exc}")
                    page_links = []
                for link in page_links:
                    try:
                        link = normalize_for_frontier(link)
                    except Exception:
                        continue
                    if link in visited:
                        continue
                    try:
                        if not host_allowed(urlparse(link).netloc, allow):
                            continue          # --only-domains: don't wander off-site
                    except Exception:
                        continue
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
    parser.add_argument("--max-pages-per-domain", type=int, default=5,
                         help="Cap pages fetched per host per run (0 = unlimited, default 5). Low on purpose: "
                              "eggs cluster on the homepage (which is always crawled first per domain), so a "
                              "few pages per site is plenty, and it keeps the crawl hopping to new domains. "
                              "Use 1 for a homepage-only blitz.")
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
    parser.add_argument("--no-404", dest="no_404", action="store_true",
                         help="Don't probe each host's custom 404 page for art/jokes")
    parser.add_argument("--no-security-txt", action="store_true",
                         help="Don't probe each host's security.txt")
    parser.add_argument("--no-asset-comments", action="store_true",
                         help="Don't fetch first-party CSS/JS to mine their source comments")
    parser.add_argument("--max-asset-fetches", type=int, default=4,
                         help="Max first-party CSS/JS files fetched per host for comments (default 4)")
    parser.add_argument("--dns-txt", action="store_true",
                         help="Also check each host's DNS TXT records for messages (needs dnspython)")
    parser.add_argument("--only-domains", default="",
                         help="Comma-separated domain allowlist - the crawl stays on these sites and their "
                              "subdomains and never wanders off (e.g. manableed.com,v0rt.com). Apex domains "
                              "match www and all subdomains. Empty = roam the whole web (default). Pair with "
                              "a separate --output-dir to crawl just your own sites without touching an "
                              "existing roaming crawl's frontier.")
    parser.add_argument("--output-dir", default=".", help="Directory for outputs (default: current dir)")
    parser.add_argument("--ignore-robots", action="store_true",
                         help="Skip robots.txt checks (off by default - this tool roams sites you don't own)")
    parser.add_argument("--fresh", action="store_true", help="Discard all finds and crawl history, start clean")
    parser.add_argument("--public", action="store_true",
                         help="Also write public_finds.json - a redacted, publish-safe export for the PHP site "
                              "(emails/IPs masked; any find containing a real secret is dropped). Re-running with "
                              "--public after a crawl has finished exports the existing finds without re-fetching.")
    parser.add_argument("--public-cap", type=int, default=0,
                         help="With --public, hard-limit the published finds to the top N by score "
                              "(default 0 = publish everything; the site shows the top 500 on the "
                              "homepage and paginates the rest in /archive). Set a positive N only "
                              "to cap file size on very large datasets.")
    args = parser.parse_args()
    args.only_domains = {
        d.strip().lower()[4:] if d.strip().lower().startswith("www.") else d.strip().lower()
        for d in args.only_domains.split(",") if d.strip()
    }

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

    if args.dns_txt and _dns_resolver is None:
        print("[note] --dns-txt needs dnspython (pip install dnspython); "
              "skipping DNS TXT checks this run.")

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

    if args.public:
        public_path = Path(args.output_dir) / "public_finds.json"
        kept, dropped, capped = export_public(store.finds, public_path, args.public_cap)
        extra = f", {capped} held back below the top-{args.public_cap} cap" if capped else ""
        print(f"Public export          : {public_path} "
              f"({kept} finds published, {dropped} dropped as sensitive{extra})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
