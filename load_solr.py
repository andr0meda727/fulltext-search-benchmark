"""Index data/arxiv.csv into Solr through /update: JSON batches, one commit at the end.

Times are saved to results/load_solr_<collection>.json.

Usage:
    python load_solr.py                                       # full data -> collection arxiv
    python load_solr.py --collection arxiv_p10 --max-part 10  # 10% subset
"""
import argparse
import csv
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

from common import DATA_CSV, SOLR, save_json


def to_doc(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "abstract": row["abstract"],
        "categories": row["categories"].split(),
        "main_cat": row["main_cat"],
        "sub_cat": row["sub_cat"],
        "license": row["license"],
        "year": int(row["year"]),
        "pub_date": row["pub_date"] + "T00:00:00Z",
        "n_authors": int(row["n_authors"]),
        "part": int(row["part"]),
    }


def batches(max_part, size):
    batch = []
    with open(DATA_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if int(row["part"]) < max_part:
                batch.append(to_doc(row))
                if len(batch) == size:
                    yield batch
                    batch = []
    if batch:
        yield batch


def index_size(collection):
    """Index size in bytes, summed over the collection's cores."""
    cores = requests.get(f"{SOLR}/admin/cores", params={"action": "STATUS"}).json()["status"]
    return sum(c["index"]["sizeInBytes"] for name, c in cores.items()
               if name == collection or name.startswith(collection + "_shard"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="arxiv")
    ap.add_argument("--max-part", type=int, default=100, help="index rows with part < N (N%% of data)")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    url = f"{SOLR}/{args.collection}/update"

    def send(batch):
        r = requests.post(url, params={"commit": "false"}, json=batch)
        if not r.ok:
            raise RuntimeError(f"{r.status_code}: {r.text[:1000]}")
        return len(batch)

    t0 = time.perf_counter()
    sent = 0
    with ThreadPoolExecutor(args.threads) as pool:
        pending = set()
        for batch in batches(args.max_part, args.batch):
            if len(pending) >= args.threads * 2:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    sent += f.result()
                    if sent % 100_000 < args.batch:
                        print(f"  {sent:,} docs, {time.perf_counter() - t0:.0f} s")
            pending.add(pool.submit(send, batch))
        for f in pending:
            sent += f.result()
    send_s = time.perf_counter() - t0
    print(f"sent {sent:,} docs in {send_s:.1f} s")

    t1 = time.perf_counter()
    requests.get(url, params={"commit": "true"}).raise_for_status()
    commit_s = time.perf_counter() - t1
    print(f"commit: {commit_s:.1f} s")

    num_found = requests.get(f"{SOLR}/{args.collection}/select", params={"q": "*:*", "rows": 0}).json()["response"]["numFound"]
    result = {"collection": args.collection, "rows": num_found, "send_s": round(send_s, 1),
              "commit_s": round(commit_s, 1), "total_s": round(send_s + commit_s, 1),
              "index_size_mb": round(index_size(args.collection) / 2**20), "batch": args.batch,
              "threads": args.threads}
    save_json(f"load_solr_{args.collection}.json", result)
    print(result)


if __name__ == "__main__":
    main()
