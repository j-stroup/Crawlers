"""Fold the finds collected from your own sites (mysites/finds.jsonl) into the
main v0rt dataset (finds.jsonl), de-duplicated, then re-export public_finds.json.

Backs up the main finds.jsonl first. Safe to run repeatedly - a find already in
the main set is skipped, so re-running after another scoped crawl only adds what's
new.
"""
import json, shutil
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
import comment_hunter as ch   # noqa: E402

MAIN = HERE / "finds.jsonl"
NEW  = HERE / "mysites" / "finds.jsonl"
PUB  = HERE / "public_finds.json"


def load(p):
    out = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    base = load(MAIN)
    extra = load(NEW)
    if not extra:
        print(f"No finds at {NEW} - run the scoped crawl first.")
        return
    keys = {ch._find_key(r["kind"], r["text"], r.get("domain", "")) for r in base}
    added = 0
    by_site = {}
    for r in extra:
        k = ch._find_key(r["kind"], r["text"], r.get("domain", ""))
        if k not in keys:
            keys.add(k); base.append(r); added += 1
            by_site[r.get("domain", "?")] = by_site.get(r.get("domain", "?"), 0) + 1
    if added:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(MAIN, MAIN.with_suffix(f".jsonl.bak-{stamp}"))
        with MAIN.open("w", encoding="utf-8") as f:
            for r in base:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    kept, dropped, _ = ch.export_public(base, PUB, cap=0)
    print(f"Added {added} new find(s) from your sites; main total now {len(base)}.")
    for site, n in sorted(by_site.items(), key=lambda kv: -kv[1]):
        print(f"   +{n:<4} {site}")
    print(f"Re-exported {PUB.name}: {kept} published ({dropped} withheld as sensitive).")
    if not added:
        print("(Nothing new - everything from your sites was already in the dataset.)")


if __name__ == "__main__":
    main()
