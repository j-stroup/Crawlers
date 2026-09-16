# Crawlers

Two link/file auditors for websites you own or manage. Neither one is a
vulnerability scanner - the goal is housekeeping: find broken links, find
PDFs/docs that shouldn't be on a modern site, and flag anything that
deserves a closer look by hand.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## quick_scan.py - monthly checkup

Single domain only (never follows links off-site), single-threaded with a
small randomized delay so it doesn't look like an attack, smallest
possible footprint. Flags any response >= 400 and any link ending in
`.pdf`, `.txt`, `.doc`, or `.docx`.

Pages within the domain are fully crawled. Everything else found on a
page - external `<a href>` links, `<img>`, `<link rel=stylesheet|icon>`,
`<iframe>`, and `<script>` - gets a status check (broken image, dead
external link, missing stylesheet, etc.) but is never crawled further.

```
python quick_scan.py https://example.com
python quick_scan.py https://example.com --delay-min 0.5 --delay-max 1.5
python quick_scan.py https://example.com --max-pages 500 --output-dir reports
```

Writes `{domain}.txt` in the current directory (or `--output-dir`).

## deep_audit.py - twice-a-year deep dive

Threaded, throttle controlled by you, robots.txt aware (with an opt-out),
proxy support, and can crawl one site, several sites in one session, or
follow links anywhere once told to. Meant to run for a long time.

Pages within scope (same domain, or anywhere once `--allow-external` is
set) are fully crawled. External `<a href>` links (when not
`--allow-external`) and any `<img>`, `<link rel=stylesheet|icon>`,
`<iframe>`, or external `<script>` found on a page get a status check but
are never crawled further - so a broken image or a dead link to someone
else's site gets caught without spidering their whole domain.

Finds:
- 404/410s as broken links; 401/403/407/5xx as a separate "security-watch"
  bucket (auth issues, server errors that can leak internals)
- 429/503 rate-limiting handled with per-host backoff + retry (see below),
  reported as "could not verify" rather than as a broken link
- Broken resources: dead images, stylesheets, iframes, scripts, and
  external links
- Exposed directory listings (`Index of /...` pages - Apache/nginx/IIS
  autoindex left on)
- Missing baseline security response headers (`Content-Security-Policy`,
  `X-Frame-Options`, `X-Content-Type-Options`, `Strict-Transport-Security`,
  `Referrer-Policy`) - checked once per site against the **seed/homepage**
  (header config is normally site-wide)
- Legacy document types (pdf/doc/docx/ppt/pptx/xls/xlsx)
- Sensitive/data-exposure file types, tiered: always-bad types (`.sql`,
  `.env`, `.db`, `.bak`, `.key`, `.pem`, `.zip`, ...) always flag;
  normally-benign ones (`.json`, `.xml`, `.csv`, `.txt`) flag **only** when
  the filename itself looks like a dump (`users_export.csv`,
  `db_backup.json`) - so `sitemap.xml` and `manifest.json` don't
- URLs with a **dangerous parameter value** - a redirect/callback param
  pointing at a URL (open-redirect/SSRF), a value with `../` traversal, or
  a `debug=true`/`admin=1` toggle. Value-based, so a plain `?id=5` or
  `?page=2` no longer trips anything
- Leaked API keys/credentials in HTML and `.js`, **tiered by confidence**:
  high (AWS, Slack, Stripe `sk_live`, private-key blocks), medium (raw
  JWTs, high-entropy generic `api_key=`/`password=` assignments), and a
  separate informational bucket for public-by-design keys (Google/Firebase
  browser keys, Stripe publishable keys) that are *meant* to ship in the
  page. Placeholder/example values (`your_api_key_here`, the AWS docs key)
  and low-entropy junk (`password = "password"`) are dropped. Values are
  masked (`AKIA****************TLPD`) so the report isn't a secrets dump.

Plus a handful of cheap extra signals gathered while it's already on the
page (no extra requests):
- **Mixed content** - `http://` sub-resources (scripts, images, CSS,
  iframes, form actions) loaded on an `https://` page
- **Cookies missing security flags** - `Set-Cookie` without
  `Secure`/`HttpOnly`/`SameSite`, reported once per cookie name
- **Version-disclosure headers** - `Server`, `X-Powered-By`,
  `X-AspNet-Version`, etc. that hand an attacker a version to look up
- **Third-party script inventory** - the external hosts you're loading
  JavaScript from (supply-chain surface), one line per host
- **`target="_blank"` without `rel="noopener"`** - cross-origin
  tabnabbing links
- **Exposed common paths** (opt-in, `--probe-common-paths`) - the only
  active check: it requests a small fixed list of things that shouldn't be
  public (`/.git/HEAD`, `/.env`, `/backup.zip`, ...) even if nothing links
  to them, and reports any that are reachable (or present-but-403). Off by
  default because it fetches URLs the site never advertised.

Broken-resource and external-link findings are attributed to the site
that referenced them, not the (often third-party) domain the link points
to - so a dead CDN image on example.com shows up in `example.com.txt`,
not in a report for the CDN.

```
# One site, defaults (8 threads, robots.txt respected, same-domain only)
python deep_audit.py https://example.com

# Several sites in one run - each gets its own report
python deep_audit.py https://example.com https://other-site.com --threads 15

# Crawl beyond the starting domain (follows every link it finds)
python deep_audit.py https://example.com --allow-external --max-pages 20000

# These are my own sites - skip robots.txt entirely
python deep_audit.py https://example.com --ignore-robots

# Slow it way down, route through a proxy
python deep_audit.py https://example.com --delay-min 1 --delay-max 3 --proxy http://127.0.0.1:8080

# Rotate through a list of proxies (one per line, file passed via --proxy-file)
python deep_audit.py https://example.com --proxy-file proxies.txt

# A site with years of near-identical archive pages (e.g. /bids/...) -
# only fully crawl the first 50 pages under each top-level path, and cap
# calendar/faceted-search traps at 20 query variants per path
python deep_audit.py https://example.com --max-per-path-prefix 50 --max-query-variants 20

# It crashed six hours in - just run the exact same command again to resume
python deep_audit.py https://example.com
# ...or force a clean start, ignoring saved state
python deep_audit.py https://example.com --fresh

# Also probe for unlinked sensitive paths (.git/.env/backups) - your own site
python deep_audit.py https://example.com --probe-common-paths
```

`--max-pages` defaults to 5000 as a safety valve across the whole run
(all sites combined); pass `--max-pages 0` for unlimited if you really
want it to run until the queue is empty.

Writes one `{domain}.txt` per site into `--output-dir` (default
`./reports/`).

**Resumable state + bounded memory (SQLite).** The crawl frontier, the
"already seen" set, and every finding live in a SQLite file
(`<output-dir>/.crawl_state/audit_<hash>.db` by default, or
`--state-db PATH`), not in RAM. Two payoffs for the multi-day crawls this
is built for:

- *Resume after a crash.* If a run dies - crash, killed terminal, dropped
  remote session, power blip - just rerun the exact same command. It
  resets the handful of URLs that were in flight and picks the frontier
  back up where it stopped, instead of re-crawling the whole site.
  `--fresh` forces a clean start; a re-run of an already-*completed* crawl
  also starts fresh automatically.
- *Bounded memory.* Because the frontier and seen-set are on disk, the
  process doesn't grow without limit on a million-URL site until the OS
  kills it - historically the reason big crawls didn't finish.

The `.txt`/`.html` reports are still checkpointed every
`--checkpoint-interval` seconds (default 120, `0` disables) with a
`*** PARTIAL / IN-PROGRESS ***` banner, so you always have a readable
deliverable mid-crawl; the database is the durable source of truth behind
them.

**Cutting redundant crawling (no extra requests).** Three defenses stop a
big or trap-laden site from running for days without fetching any faster:

- *URL canonicalization.* Before a URL is added to the frontier it's
  normalized - lowercased host, default port and tracking params
  (`utm_*`, `fbclid`, `gclid`, ...) stripped, query parameters sorted - so
  the same page linked a dozen different ways is crawled **once** instead
  of a dozen times. (Trailing slashes are left alone, since some servers
  really do treat `/foo` and `/foo/` as different pages.)
- *`--max-per-path-prefix N`* caps how many pages get fully crawled under
  the same leading path segment(s) (`--path-prefix-depth`, default 1 -
  e.g. everything under `/bids/` counts as one bucket regardless of year).
  For huge flat archives - years of `/bids/...` postings, thousands of
  `/blog/...` posts - that rarely turn up anything new.
- *`--max-query-variants N`* caps how many distinct query-string variants
  of the same path get crawled - the classic calendar
  (`/events?date=...` → next month forever) and faceted-search
  (`/search?color=&size=&sort=...` → combinatorial explosion) traps.
- *`--max-depth N`* stops following links past N hops from a seed (a seed
  is depth 0). A blunt but effective cap on how deep a templated or
  paginated tree is chased.

All four are off by default (the caps at `0`, canonicalization is always
on since it only removes provable duplicates). Each capped skip is
counted per site in the report, so a low page count reads as "we capped
it here," not "the site only had that many pages." Turn the caps on for
the specific large or trap-prone sites where you know they apply.

**Politeness / not DoS-ing the site.** Beyond the randomized per-request
delay, the crawler watches for `429 Too Many Requests` / `503` responses.
When a host returns one it backs that host off for a cooldown window that
*every* thread honours (respecting a `Retry-After` header if present, else
a doubling 2s → 60s backoff), and retries the URL up to `--max-retries`
times (default 2). Only if it's still throttled after the retries does it
record a finding - as "rate-limited / could not verify," never as a broken
link. Net effect: if a site starts pushing back, the whole crawl quietly
slows down instead of hammering it, and you don't get a report full of
phantom "broken" links that were really just rate-limiting.

## Notes

- Both tools rotate through a shared pool of real browser user-agent
  strings (`user_agents.py`) instead of sending a default
  `python-requests/x.x` header.
- The "risky params" and "security-watch status codes" lists are
  heuristics meant to shorten your manual review list, not a claim that
  something is actually vulnerable.
- Report files (and anything in `reports/`) are gitignored by default
  since they can contain masked-but-still-sensitive findings.
