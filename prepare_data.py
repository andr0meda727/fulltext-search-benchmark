"""Convert the Kaggle arXiv metadata snapshot (JSON lines) into data/arxiv.csv.

Takes an evenly spread sample of --rows records (selected by a hash of the id, so the
result is the same on every machine) and gives each row a `part` value 0-99 used for
the 10% / 50% / 100% scaling test.

Usage:
    python prepare_data.py data/arxiv-metadata-oai-snapshot.json --rows 1000000
"""
import argparse
import csv
import hashlib
import json
import statistics
from email.utils import parsedate_to_datetime

from common import DATA_CSV

FIELDS = ["id", "title", "abstract", "categories", "main_cat", "sub_cat", "license",
          "year", "pub_date", "n_authors", "part"]
BUCKETS = 1_000_000


def bucket(arxiv_id):
    return int(hashlib.md5(arxiv_id.encode()).hexdigest()[:12], 16) % BUCKETS


def clean(text):
    return " ".join(text.split())


def short_license(url):
    # "http://creativecommons.org/licenses/by/4.0/" -> "by/4.0"
    if not url:
        return "none"
    return "/".join(url.rstrip("/").split("/")[-2:])


def first_published(rec):
    try:
        return parsedate_to_datetime(rec["versions"][0]["created"]).date()
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def to_row(rec):
    cats = rec["categories"].split()
    pub = first_published(rec)
    pub_date = pub.isoformat() if pub else rec["update_date"]
    return {
        "id": rec["id"],
        "title": clean(rec["title"]),
        "abstract": clean(rec["abstract"]),
        "categories": " ".join(cats),
        "main_cat": cats[0].split(".")[0],   # archive, e.g. cs, math, astro-ph
        "sub_cat": cats[0],                  # primary category, e.g. cs.LG
        "license": short_license(rec.get("license")),
        "year": int(pub_date[:4]),
        "pub_date": pub_date,
        # Only the number of authors is kept - no personal data needed.
        "n_authors": len(rec.get("authors_parsed") or []) or 1,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="arxiv-metadata-oai-snapshot.json from Kaggle")
    ap.add_argument("--rows", type=int, default=1_000_000)
    args = ap.parse_args()

    with open(args.source, "rb") as f:
        total = sum(1 for _ in f)
    # Slightly over-sample, then stop at --rows.
    threshold = min(BUCKETS, int(BUCKETS * args.rows / total * 1.02) + 1)
    print(f"{total:,} records in source, keeping ~{args.rows:,}")

    DATA_CSV.parent.mkdir(exist_ok=True)
    written, lengths = 0, []
    with open(args.source, encoding="utf-8") as src, open(DATA_CSV, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        for line in src:
            rec = json.loads(line)
            b = bucket(rec["id"])
            if b >= threshold or not rec.get("abstract"):
                continue
            row = to_row(rec)
            row["part"] = b * 100 // threshold
            writer.writerow(row)
            lengths.append(len(row["abstract"]))
            written += 1
            if written >= args.rows:
                break

    print(f"wrote {written:,} rows to {DATA_CSV}")
    print(f"abstract length: mean {statistics.mean(lengths):.0f}, median {statistics.median(lengths):.0f} chars")


if __name__ == "__main__":
    main()
