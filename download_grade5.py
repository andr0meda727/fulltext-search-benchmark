"""Download a pinned public arXiv Parquet mirror to the existing CSV format.

Only one Parquet shard is stored temporarily. The first N usable records are retained;
this is a source-order convenience sample, NOT a random sample of all arXiv.
Hash-based part assignment provides nested, approximately 10/50/100% subsets.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import time

import requests
import pyarrow.parquet as pq

from prepare_data import FIELDS, to_row

REPO = "librarian-bots/arxiv-metadata-snapshot"
REVISION = "c428226b689a0e17db2cd55265133b9eca15d97f"


def source_records(folder, shards, sources):
    for shard in shards:
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{shard['path']}"
        path = folder / "download-grade5.parquet"
        digest = hashlib.sha256()
        print(f"Downloading {shard['path']} ({shard['size']/2**20:.0f} MiB)", flush=True)
        with requests.get(url, stream=True, timeout=(30, 120)) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_content(1024*1024):
                    output.write(chunk)
                    digest.update(chunk)
        expected = shard.get("lfs", {}).get("oid")
        if expected and digest.hexdigest() != expected:
            raise RuntimeError("Parquet download SHA256 mismatch")
        sources.append({"url": url, "sha256": digest.hexdigest(), "bytes": path.stat().st_size})
        parquet = pq.ParquetFile(path)
        try:
            for batch in parquet.iter_batches(batch_size=1000, columns=["id", "title", "abstract", "categories", "license", "versions", "update_date", "authors_parsed"]):
                yield from batch.to_pylist()
        finally:
            parquet.close()
            path.unlink()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=500_000)
    ap.add_argument("--output", type=Path, default=Path(os.environ.get("GRADE5_CSV", "data/arxiv.csv")))
    args = ap.parse_args()
    if args.rows < 500_000:
        ap.error("Grade 5.0 requires at least 500000 records")
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}; move it first or use another --output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(f"https://huggingface.co/api/datasets/{REPO}/tree/{REVISION}/data", timeout=60)
    response.raise_for_status()
    shards = sorted([r for r in response.json() if r["path"].endswith(".parquet")], key=lambda r: r["path"])
    sources = []
    temporary = args.output.with_suffix(".partial.csv")
    n = total_length = duplicates = 0
    parts = [0] * 100
    years = {}
    seen = set()
    digest = hashlib.sha256()
    started = time.perf_counter()
    stream = source_records(args.output.parent, shards, sources)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS)
            writer.writeheader()
            for rec in stream:
                if isinstance(rec.get("versions"), str):
                    rec["versions"] = json.loads(rec["versions"])
                if isinstance(rec.get("authors_parsed"), str):
                    rec["authors_parsed"] = json.loads(rec["authors_parsed"])
                if not rec.get("abstract", "").strip() or not rec.get("categories"):
                    continue
                row = to_row(rec)
                if row["id"] in seen:
                    duplicates += 1
                    continue
                seen.add(row["id"])
                row["part"] = int(hashlib.md5(row["id"].encode()).hexdigest(), 16) % 100
                writer.writerow(row)
                total_length += len(row["abstract"])
                parts[row["part"]] += 1
                years[row["year"]] = years.get(row["year"], 0) + 1
                digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()+b"\n")
                n += 1
                if n % 25_000 == 0:
                    print(f"Downloaded/prepared {n:,} records in {time.perf_counter()-started:.0f}s", flush=True)
                if n == args.rows:
                    break
    finally:
        stream.close()
    if n != args.rows or total_length / n < 300:
        raise RuntimeError(f"Dataset requirements failed: rows={n}, mean abstract={total_length/max(n,1)}")
    temporary.replace(args.output)
    manifest = {"source_url": f"https://huggingface.co/datasets/{REPO}/tree/{REVISION}", "source_files": sources, "source_repository": REPO, "revision": REVISION,
                "metadata_license": "CC0-1.0 (mirror declaration); individual paper license retained in CSV",
                "sampling": "first N unique nonempty abstracts in source order; source-order convenience sample",
                "rows": n, "mean_abstract_chars": total_length / n, "csv_bytes": args.output.stat().st_size,
                "source_duplicate_ids_skipped": duplicates,
                "source_records_sha256": digest.hexdigest(), "part_counts": parts, "year_counts": years,
                "seconds": time.perf_counter()-started}
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
