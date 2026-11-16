"""Load data/arxiv.csv into PostgreSQL with COPY and build the full-text index.

Each step is timed and saved to results/load_pg_<table>.json.

Usage:
    python load_pg.py                                   # full data -> table arxiv
    python load_pg.py --table arxiv_p10 --max-part 10   # 10% subset for the scaling test
"""
import argparse
import csv
import time

from common import DATA_CSV, ROOT, pg_connect, save_json


def run_sql_file(conn, name, table):
    sql = (ROOT / "sql" / name).read_text().replace("{table}", table)
    t0 = time.perf_counter()
    conn.execute(sql)
    return time.perf_counter() - t0


def copy_csv(conn, table, max_part):
    copy_sql = f"COPY {table} FROM STDIN WITH (FORMAT csv, HEADER true)"
    with conn.cursor() as cur, cur.copy(copy_sql) as copy, open(DATA_CSV, encoding="utf-8", newline="") as f:
        if max_part >= 100:
            while block := f.read(1 << 20):
                copy.write(block)
        else:
            reader = csv.reader(f)
            header = next(reader)
            part_idx = header.index("part")
            writer = csv.writer(_CopyWriter(copy))
            writer.writerow(header)
            for row in reader:
                if int(row[part_idx]) < max_part:
                    writer.writerow(row)


class _CopyWriter:
    """Lets csv.writer write straight into a COPY stream."""
    def __init__(self, copy):
        self.copy = copy

    def write(self, s):
        self.copy.write(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="arxiv")
    ap.add_argument("--max-part", type=int, default=100, help="load rows with part < N (N%% of data)")
    args = ap.parse_args()

    with pg_connect() as conn:
        run_sql_file(conn, "01_table.sql", args.table)

        t0 = time.perf_counter()
        copy_csv(conn, args.table, args.max_part)
        copy_s = time.perf_counter() - t0
        print(f"COPY: {copy_s:.1f} s")

        tsv_s = run_sql_file(conn, "02_tsvector.sql", args.table)
        print(f"tsvector column: {tsv_s:.1f} s")
        gin_s = run_sql_file(conn, "03_gin_index.sql", args.table)
        print(f"GIN index + ANALYZE: {gin_s:.1f} s")

        rows, table_size, gin_size = conn.execute(
            f"SELECT count(*), pg_size_pretty(pg_total_relation_size('{args.table}')),"
            f" pg_size_pretty(pg_relation_size('{args.table}_tsv_gin')) FROM {args.table}").fetchone()

    result = {"table": args.table, "rows": rows, "copy_s": round(copy_s, 1), "tsvector_s": round(tsv_s, 1),
              "gin_index_s": round(gin_s, 1), "total_s": round(copy_s + tsv_s + gin_s, 1),
              "table_size_total": table_size, "gin_size": gin_size}
    save_json(f"load_pg_{args.table}.json", result)
    print(result)


if __name__ == "__main__":
    main()
