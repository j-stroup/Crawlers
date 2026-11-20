"""Re-export public_finds.json from the current finds.jsonl.

Run this after any crawl you did WITHOUT the --public flag (a plain
`comment_hunter.py ...` run updates finds.jsonl but NOT the published export).
It redacts secrets, refreshes `domains_crawled`/`total_collected`/timestamp, and
also updates the local site/data copy for previewing. Upload the resulting
public_finds.json to the server's data/ folder.

    python publish.py
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import comment_hunter as ch  # noqa: E402

FINDS = HERE / "finds.jsonl"
PUB = HERE / "public_finds.json"
SITE_COPY = HERE / "site" / "data" / "public_finds.json"


def main():
    finds = [json.loads(l) for l in FINDS.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept, dropped, _ = ch.export_public(finds, PUB, cap=0)
    d = json.loads(PUB.read_text(encoding="utf-8"))
    if SITE_COPY.parent.is_dir():
        SITE_COPY.write_text(PUB.read_text(encoding="utf-8"), encoding="utf-8")
        site_note = f" (local preview copy refreshed)"
    else:
        site_note = ""
    print(f"published {kept} finds ({dropped} withheld as sensitive)")
    print(f"  domains_crawled : {d['domains_crawled']:,}")
    print(f"  total_collected : {d['total_collected']:,}")
    print(f"  generated       : {d['generated']}")
    print(f"\nWrote {PUB}{site_note}")
    print("Upload public_finds.json to the server's data/ folder to go live.")


if __name__ == "__main__":
    main()
