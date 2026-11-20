"""Refresh the ASCII-art find(s) for a site from its current homepage.

A plain re-crawl can't *replace* art: the crawler keys finds by text, so edited
art becomes a brand-new find and the stale one lingers. This fetches the live
page, extracts its ASCII art exactly the way the crawler would, and swaps it in
for that domain's existing ascii_art find(s) in both finds.jsonl stores, then
re-exports public_finds.json.

    python refresh_site_art.py https://manableed.com/            # dry run
    python refresh_site_art.py https://manableed.com/ --apply    # do it
"""
import sys, re, json, shutil, argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import comment_hunter as ch  # noqa: E402
import requests  # noqa: E402

MAIN = HERE / "finds.jsonl"
SCOPED = HERE / "mysites" / "finds.jsonl"
PUB = HERE / "public_finds.json"


def load(p):
    out = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def extract_ascii(url):
    """Fetch url, return list of ASCII-art texts exactly as the crawler stores
    them (classify_comment -> ascii_art -> normalize_ascii)."""
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (v0rt art refresh)"}, timeout=20)
    r.raise_for_status()
    html = r.text
    arts = []
    for raw in re.findall(r"<!--(.*?)-->", html, re.DOTALL):
        v = ch.classify_comment(raw)
        if v and v[0] == "ascii_art":
            arts.append((ch.normalize_ascii(raw), v[1], v[2]))
    return arts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    domain = urlparse(args.url).netloc
    new_arts = extract_ascii(args.url)
    print(f"Live {domain}: found {len(new_arts)} ASCII-art block(s) on {args.url}\n")
    for t, sc, tg in new_arts:
        print("  --- new ---")
        print("  " + t.replace("\n", "\n  "))
        print()

    base = load(MAIN)
    old = [r for r in base if r.get("kind") == "ascii_art" and r.get("domain") == domain]
    print(f"Existing ascii_art find(s) for {domain} in finds.jsonl: {len(old)}")
    for r in old:
        print("  --- old (will be removed) ---")
        print("  " + r.get("text", "").replace("\n", "\n  "))
        print()

    new_texts = {t for t, _, _ in new_arts}
    old_texts = {r.get("text", "") for r in old}
    if new_texts == old_texts:
        print("No change: the live art already matches what's stored. Nothing to do.")
        return

    if not args.apply:
        print("(DRY RUN — re-run with --apply to swap old art for new in both stores "
              "and re-export public_finds.json.)")
        return

    now = datetime.now().isoformat(timespec="seconds")
    def swap(path):
        recs = load(path)
        if not recs and path is SCOPED:
            return 0, 0
        kept = [r for r in recs if not (r.get("kind") == "ascii_art" and r.get("domain") == domain)]
        removed = len(recs) - len(kept)
        for t, sc, tg in new_arts:
            kept.append({"kind": "ascii_art", "text": t, "url": args.url, "domain": domain,
                         "score": sc, "tags": tg, "seen_on": 1, "ts": now})
        shutil.copy2(path, path.with_suffix(f".jsonl.bak-{datetime.now():%Y%m%d-%H%M%S}"))
        with path.open("w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return removed, len(new_arts)

    for p in (MAIN, SCOPED):
        if p.is_file():
            rem, add = swap(p)
            print(f"{p.relative_to(HERE)}: removed {rem} old, added {add} new")
    base2 = load(MAIN)
    kept, dropped, _ = ch.export_public(base2, PUB, cap=0)
    print(f"Re-exported {PUB.name}: {kept} published ({dropped} withheld).")


if __name__ == "__main__":
    main()
