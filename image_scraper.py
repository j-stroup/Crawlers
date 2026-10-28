#!/usr/bin/env python3
"""Image scraper - crawls a list of domains and downloads every <img>
(src and srcset) found on their pages.

Single-threaded with a small randomized delay, same courtesy approach as
quick_scan.py and email_audit.py. Only follows links within the domain
being scanned - external links are never fetched. Plain breadth-first
order (unlike email_audit.py's contact-page-first priority queue - there's
no equivalent "most likely" page for images, they're everywhere). Images
are deduped by content hash, not just URL, so the same logo/banner served
from ten different pages (or ten different cache-busted URLs) is only
saved once.

Usage:
    python image_scraper.py domains.txt
    python image_scraper.py https://example.com https://other-site.com
    python image_scraper.py domains.txt --max-pages 300 --output-dir reports
"""

import argparse
import hashlib
import json
import mimetypes
import random
import shutil
import struct
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from user_agents import USER_AGENTS
from quick_scan import same_domain, extract_links, site_name
from email_audit import canonicalize, fetch_page

IMAGE_CONTENT_TYPE_PREFIX = "image/"


def extract_srcset_urls(base_url, soup):
    """<img src> is already covered by quick_scan's extract_links (as a
    resource_links "image" entry) - this just adds the extra variants
    listed in srcset, which quick_scan never needed to look at."""
    urls = []
    for tag in soup.find_all("img", srcset=True):
        for part in tag["srcset"].split(","):
            part = part.strip()
            if not part:
                continue
            candidate = urljoin(base_url, part.split()[0])
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)
    return urls


# Content-types where the URL's own extension can't be trusted - CDNs
# commonly serve a different format than the path implies via a query
# param (Contentful/imgix/Cloudinary-style "?fm=webp" on a path that
# still ends in .jpg). The first entry is used when renaming.
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": (".jpg", ".jpeg", ".jfif"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
    "image/svg+xml": (".svg",),
    "image/avif": (".avif",),
    "image/bmp": (".bmp",),
    "image/x-icon": (".ico",),
    "image/vnd.microsoft.icon": (".ico",),
    "image/tiff": (".tiff", ".tif"),
}


def guess_filename(url, content_type):
    """Names the file after the URL's own filename, but corrects the
    extension when it doesn't match what the server actually sent - a
    path can keep its original ".jpg" even when a CDN query param (e.g.
    "?fm=webp") makes it actually serve webp bytes."""
    name = Path(urlparse(url).path).name or "image"
    stem = Path(name).stem or "image"
    current_ext = Path(name).suffix.lower()

    mime = (content_type or "").split(";")[0].strip().lower()
    acceptable = CONTENT_TYPE_EXTENSIONS.get(mime)

    if acceptable and current_ext not in acceptable:
        return stem + acceptable[0]
    if not current_ext:
        return stem + (acceptable[0] if acceptable else (mimetypes.guess_extension(mime) or ""))
    return name


def unique_path(dest_dir, filename):
    """Appends -2, -3, ... before the extension if filename is already
    taken by a *different* file (same name, different content - the same
    content never gets here since that's caught by the hash dedup first)."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def get_image_dimensions(body):
    """Reads width/height straight out of a raster image's own header
    bytes - no decode, no imaging library dependency, just the same
    "parse it ourselves" approach already used for robots.txt in
    text_harvester.py. Returns None for formats this doesn't recognize
    (WEBP, SVG, BMP, ICO, ...) - those are let through the size filter
    unmeasured rather than guessed at, since a wrong guess would be
    worse than no guess."""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        if len(body) < 24:
            return None
        width, height = struct.unpack(">II", body[16:24])
        return width, height

    if body[:3] == b"\xff\xd8\xff":
        i = 2
        while i < len(body) - 9:
            if body[i] != 0xFF:
                i += 1
                continue
            marker = body[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", body[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", body[i + 5:i + 9])
                return width, height
            i += 2 + seg_len
        return None

    if body[:6] in (b"GIF87a", b"GIF89a"):
        if len(body) < 10:
            return None
        width, height = struct.unpack("<HH", body[6:10])
        return width, height

    return None


def download_image(session, url, headers, timeout, max_fetch_seconds):
    """Same streamed GET + hard wall-clock deadline as fetch_page, but
    returns raw bytes instead of decoding as text - decoding binary
    image data as UTF-8 would corrupt it."""
    started = time.monotonic()
    with session.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as resp:
        status = resp.status_code
        content_type = resp.headers.get("Content-Type", "")
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=8192):
            body.extend(chunk)
            if time.monotonic() - started > max_fetch_seconds:
                raise TimeoutError(f"exceeded {max_fetch_seconds:.0f}s wall-clock fetch limit")
    return status, content_type, bytes(body)


def crawl_domain(start_url, delay_min, delay_max, max_pages, max_images, timeout, max_fetch_seconds,
                  dest_dir, seen_hashes, on_image, min_width=0, min_height=0, allow_external=False):
    netloc = urlparse(start_url).netloc
    session = requests.Session()

    seed = canonicalize(start_url)
    visited = set()
    found_on = {seed: "(seed)"}
    queue = deque([seed])
    seen_image_urls = set()
    downloaded = 0

    while queue:
        if max_pages and len(visited) >= max_pages:
            print(f"  reached --max-pages limit ({max_pages}), stopping {netloc}")
            break
        if max_images and downloaded >= max_images:
            print(f"  reached --max-images limit ({max_images}), stopping {netloc}")
            break

        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        print(f"  [{len(visited)}] {url}  ({downloaded} image(s) saved so far)")

        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            final_url, status, content_type, text = fetch_page(session, url, headers, timeout, max_fetch_seconds)
        except Exception as exc:
            print(f"      [ERROR] {exc}")
            time.sleep(random.uniform(delay_min, delay_max))
            continue
        time.sleep(random.uniform(delay_min, delay_max))

        if status >= 400:
            print(f"      skipped page, HTTP {status}")
            continue
        if "html" not in content_type:
            print(f"      skipped page, not HTML ({content_type or 'no content-type'})")
            continue

        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception as exc:
            print(f"      [ERROR] couldn't parse page HTML: {exc}")
            continue

        page_links, resource_links = extract_links(final_url, soup)
        image_urls = [u for u, kind in resource_links if kind == "image"]
        image_urls += extract_srcset_urls(final_url, soup)

        # Dedup against both this domain's history AND repeats within
        # this same page (e.g. the same image used in two different
        # layout spots) - without the second part, a same-page repeat
        # would get fetched twice before the first fetch's result could
        # mark it seen.
        new_image_urls = []
        for image_url in image_urls:
            if image_url not in seen_image_urls:
                seen_image_urls.add(image_url)
                new_image_urls.append(image_url)
        print(f"      {len(image_urls)} image reference(s) on this page "
              f"({len(new_image_urls)} not seen on this domain yet), {len(page_links)} link(s) to follow")

        for image_url in new_image_urls:
            if max_images and downloaded >= max_images:
                print(f"      reached --max-images limit ({max_images}), not fetching the rest of this page's images")
                break

            img_headers = {"User-Agent": random.choice(USER_AGENTS)}
            try:
                img_status, img_content_type, body = download_image(
                    session, image_url, img_headers, timeout, max_fetch_seconds
                )
            except Exception as exc:
                print(f"      [IMG ERROR] {image_url}: {exc}")
                time.sleep(random.uniform(delay_min, delay_max))
                continue
            time.sleep(random.uniform(delay_min, delay_max))

            if img_status >= 400:
                print(f"      -> skip, HTTP {img_status}: {image_url}")
                continue
            if not img_content_type.startswith(IMAGE_CONTENT_TYPE_PREFIX):
                print(f"      -> skip, not an image ({img_content_type or 'no content-type'}): {image_url}")
                continue

            dims = get_image_dimensions(body)
            if dims and ((min_width and dims[0] < min_width) or (min_height and dims[1] < min_height)):
                print(f"      -> skip, too small ({dims[0]}x{dims[1]}): {image_url}")
                continue

            digest = hashlib.sha256(body).hexdigest()
            if digest in seen_hashes:
                print(f"      -> duplicate content, not re-saved: {image_url}")
                on_image(image_url, url, None, duplicate=True)
                continue

            saved_path = unique_path(dest_dir, guess_filename(image_url, img_content_type))
            saved_path.write_bytes(body)
            seen_hashes.add(digest)
            downloaded += 1
            print(f"      -> saved {saved_path.name} ({len(body) / 1024:.1f} KB): {image_url}")
            on_image(image_url, url, saved_path, duplicate=False)

        for link in page_links:
            link = canonicalize(link)
            if (allow_external or same_domain(link, netloc)) and link not in found_on:
                found_on[link] = url
                queue.append(link)

    return visited, downloaded


def state_path_for(outdir):
    return Path(outdir) / ".image_scraper_state.json"


def load_completed_domains(outdir, fresh):
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


class Manifest:
    """Tracks what's been downloaded and writes a running log, mirroring
    email_audit.py's LiveWriter: flushes after every write so an
    interrupted run doesn't lose the record of what it already saved.

    Downloaded images and the manifest persist and accumulate across
    EVERY normal run, the same as text_harvester.py's corpus.txt - not
    just during a resumed (interrupted-run) one. Whether a previous run
    finished cleanly or got interrupted has no bearing on this; only an
    explicit --fresh wipes them. (Domain-level skip-if-already-done is
    a separate, unrelated decision driven by the completed-domains
    state file - that one DOES still reset after a clean full
    completion, same as email_audit.py, so a later re-run of the same
    domain list will re-crawl its pages again rather than skip them -
    but it was never this class's job to delete anything on that
    basis, which is the bug this replaced.)"""

    def __init__(self, outdir, fresh):
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.images_dir = outdir / "images"
        self.manifest_path = outdir / "images_manifest.txt"

        if fresh:
            if self.images_dir.is_dir():
                shutil.rmtree(self.images_dir)
            if self.manifest_path.is_file():
                self.manifest_path.unlink()
        self.images_dir.mkdir(parents=True, exist_ok=True)

        self.seen_hashes = set()
        for existing in self.images_dir.rglob("*"):
            if existing.is_file():
                self.seen_hashes.add(hashlib.sha256(existing.read_bytes()).hexdigest())

        appending = self.manifest_path.is_file()
        self.manifest_f = self.manifest_path.open("a" if appending else "w", encoding="utf-8")
        if appending:
            self.manifest_f.write(f"\n--- run started at {datetime.now().isoformat(timespec='seconds')} ---\n\n")
        else:
            self.manifest_f.write("Image Scrape Manifest\n")
            self.manifest_f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
            self.manifest_f.write("(written as it goes - safe to read mid-run or after an interruption)\n")
            self.manifest_f.write("=" * 70 + "\n\n")
        self.manifest_f.flush()

        self.total_downloaded = 0
        self.total_duplicates = 0

    def start_domain(self, start_url):
        self.manifest_f.write(f"{start_url}\n")
        self.manifest_f.flush()

    def image_event(self, image_url, found_on_url, saved_path, duplicate):
        self.manifest_f.write(f"    {image_url}\n        found on: {found_on_url}\n")
        if duplicate:
            self.manifest_f.write("        (duplicate content, not re-saved)\n")
            self.total_duplicates += 1
        else:
            self.manifest_f.write(f"        saved: {saved_path.relative_to(self.images_dir.parent)}\n")
            self.total_downloaded += 1
        self.manifest_f.flush()

    def finish_domain(self, visited_count, downloaded_count):
        self.manifest_f.write(f"  ({visited_count} page(s) crawled, {downloaded_count} image(s) downloaded)\n\n")
        self.manifest_f.flush()

    def close(self):
        self.manifest_f.close()


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
        description="Crawl a list of domains and download the images found on them."
    )
    parser.add_argument("inputs", nargs="+", help="Domains/URLs, or a single file with one domain per line")
    parser.add_argument("--delay-min", type=float, default=0.4, help="Min seconds between requests (default 0.4)")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max seconds between requests (default 1.2)")
    parser.add_argument("--max-pages", type=int, default=150, help="Per-site safety cap on pages crawled (0 = unlimited, default 150)")
    parser.add_argument("--max-images", type=int, default=300, help="Per-site safety cap on images downloaded (0 = unlimited, default 300)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request connect/read timeout in seconds (default 10)")
    parser.add_argument("--max-fetch-seconds", type=float, default=30, help="Hard wall-clock cap per request regardless of how slowly the server sends data (default 30)")
    parser.add_argument("--output-dir", default=".", help="Directory to save images and the manifest into (default: current dir)")
    parser.add_argument("--fresh", action="store_true", help="Delete all previously downloaded images and the manifest, ignore any saved domain-completion progress, and start the whole list over. Without this, images always accumulate across runs.")
    parser.add_argument("--min-width", type=int, default=100, help="Skip images narrower than this many pixels (0 = no minimum, default 100). Only enforced for formats whose dimensions can be read without a decode (PNG/JPEG/GIF) - WEBP/SVG/BMP/ICO pass through unmeasured.")
    parser.add_argument("--min-height", type=int, default=100, help="Skip images shorter than this many pixels (0 = no minimum, default 100). Same format caveat as --min-width.")
    parser.add_argument("--allow-external", action="store_true", help="Follow links off the starting domain too, same as deep_audit.py's flag of the same name - --max-pages/--max-images are a single shared budget across every site reached this way, images still land under the ORIGINAL seed's folder")
    args = parser.parse_args()

    domains = load_domains(args.inputs)
    completed = load_completed_domains(args.output_dir, args.fresh)
    resume = bool(completed) and not args.fresh
    if resume:
        print(f"Resuming - {len(completed)} domain(s) already completed will be skipped.")

    manifest = Manifest(args.output_dir, fresh=args.fresh)
    started = time.time()
    completed_fully = False

    def on_image(image_url, found_on_url, saved_path, duplicate):
        manifest.image_event(image_url, found_on_url, saved_path, duplicate)

    try:
        for raw in domains:
            start_url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
            canonical_start = canonicalize(start_url)
            if canonical_start in completed:
                print(f"Skipping {start_url} (already completed in a previous run)")
                continue

            print(f"Scanning {start_url} ...")
            manifest.start_domain(start_url)
            dest_dir = manifest.images_dir / site_name(start_url)
            dest_dir.mkdir(parents=True, exist_ok=True)

            visited, downloaded = crawl_domain(
                start_url, args.delay_min, args.delay_max, args.max_pages, args.max_images,
                args.timeout, args.max_fetch_seconds, dest_dir, manifest.seen_hashes, on_image,
                min_width=args.min_width, min_height=args.min_height, allow_external=args.allow_external,
            )
            manifest.finish_domain(len(visited), downloaded)
            print(f"  {len(visited)} page(s) crawled, {downloaded} image(s) downloaded")

            completed.add(canonical_start)
            save_completed_domains(args.output_dir, completed)
        completed_fully = True
    finally:
        manifest.close()

    if completed_fully:
        state_path_for(args.output_dir).unlink(missing_ok=True)

    elapsed = time.time() - started
    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Total images downloaded: {manifest.total_downloaded}")
    print(f"Total duplicates skipped: {manifest.total_duplicates}")
    print(f"Images saved under : {manifest.images_dir}")
    print(f"Manifest            : {manifest.manifest_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
