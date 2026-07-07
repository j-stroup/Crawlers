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
- Broken resources: dead images, stylesheets, iframes, scripts, and
  external links
- Exposed directory listings (`Index of /...` pages - Apache/nginx/IIS
  autoindex left on)
- Missing baseline security response headers (`Content-Security-Policy`,
  `X-Frame-Options`, `X-Content-Type-Options`, `Strict-Transport-Security`,
  `Referrer-Policy`) - checked once per site against the first
  successfully-loaded page, since header config is normally site-wide
- Legacy document types (pdf/doc/docx/ppt/pptx/xls/xlsx)
- Sensitive/data-exposure file types (`.db`, `.sql`, `.env`, `.log`, `.bak`,
  `.json`, `.xml`, `.yml`, `.zip`, etc. - see `SENSITIVE_EXTENSIONS` in
  the script to tune the list)
- URLs whose query parameters look risky (`id`, `debug`, `redirect`,
  `token`, `cmd`, `path`, etc. - see `RISKY_PARAM_NAMES` to tune)
- Likely leaked API keys/credentials in HTML and `.js` source (AWS,
  Google, Slack, Stripe, JWTs, private key blocks, generic
  `api_key=`/`password=` assignments). Values are masked in the report
  (`AKIA****************3F2A`) so the report file itself isn't a plaintext
  secrets dump.

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
# only fully crawl the first 50 pages under each top-level path
python deep_audit.py https://example.com --max-per-path-prefix 50
```

`--max-pages` defaults to 5000 as a safety valve across the whole run
(all sites combined); pass `--max-pages 0` for unlimited if you really
want it to run until the queue is empty.

Writes one `{domain}.txt` per site into `--output-dir` (default
`./reports/`).

**Crash safety.** For a run that's going to take hours, losing
everything to a crash, a killed terminal, or a dropped remote session
would be a waste of an afternoon. Every `--checkpoint-interval` seconds
(default 120, `0` disables) each site's report is rewritten to disk
mid-crawl with a `*** PARTIAL / IN-PROGRESS ***` banner at the top. The
final write at the end of a clean run replaces that banner with the
finished report. Worst case if something dies mid-run, you lose the last
checkpoint interval of findings, not the whole audit.

**Repeated/archive endpoints.** Some sites have huge flat archives of
structurally identical pages - years of `/bids/...` postings, thousands
of blog posts under `/blog/...` - that rarely turn up anything new and
just make a long crawl longer. `--max-per-path-prefix N` caps how many
pages get *fully crawled* under the same leading path segment(s)
(`--path-prefix-depth`, default 1, controls how many segments count as
the "same bucket" - e.g. depth 1 buckets everything under `/bids/`
together regardless of year). Pages beyond the cap are skipped outright
(not fetched at all); the report notes how many were skipped per site so
you know the number is being capped, not that the site only has that
many pages. Off by default (`0`) since it's easy to accidentally skip
something real on a smaller site - turn it on for the specific
large-archive sites where you know it applies.

## Notes

- Both tools rotate through a shared pool of real browser user-agent
  strings (`user_agents.py`) instead of sending a default
  `python-requests/x.x` header.
- The "risky params" and "security-watch status codes" lists are
  heuristics meant to shorten your manual review list, not a claim that
  something is actually vulnerable.
- Report files (and anything in `reports/`) are gitignored by default
  since they can contain masked-but-still-sensitive findings.
