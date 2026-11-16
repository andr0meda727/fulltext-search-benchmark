"""Timing: LIKE vs PostgreSQL full-text vs Solr, median of 5 runs (after one warm-up).

Also prints the load/indexing times saved by load_pg.py and load_solr.py, and the
software versions for the report.

Usage:
    python bench.py
"""
import requests

from common import (QUERIES, RESULTS, SOLR, fts_where, load_json, md_table, pg_connect, pg_measure,
                    save_json, solr_basic_params, solr_measure)

TABLE = COLLECTION = "arxiv"


def measure(conn, runs=5):
    out = {}
    for q in QUERIES:
        like_sql = f"SELECT count(*) FROM {TABLE} WHERE {q['like_where']}"
        fts_sql = f"SELECT count(*) FROM {TABLE} WHERE {fts_where(q)}"
        like = pg_measure(conn, like_sql, runs)
        fts = pg_measure(conn, fts_sql, runs)
        solr = solr_measure(solr_basic_params(q), COLLECTION, runs)
        like["count"] = conn.execute(like_sql).fetchone()[0]
        fts["count"] = conn.execute(fts_sql).fetchone()[0]
        out[q["key"]] = {"LIKE": like, "PostgreSQL FTS": fts, "Solr": solr}
        print(f"  {q['key']}: LIKE {like['server_ms']:.1f} ms, FTS {fts['server_ms']:.1f} ms, "
              f"Solr {solr['server_ms']} ms")
    return out


def versions(conn):
    info = requests.get(f"{SOLR}/admin/info/system").json()
    return {"postgres": conn.execute("SELECT version()").fetchone()[0],
            "solr": info["lucene"]["solr-spec-version"],
            "solr_heap_max_mb": round(info["jvm"]["memory"]["raw"]["max"] / 2**20)}


def load_table():
    pg, solr = load_json(f"load_pg_{TABLE}.json"), load_json(f"load_solr_{COLLECTION}.json")
    if not (pg and solr):
        return "_brak plików results/load_*.json – uruchom najpierw load_pg.py i load_solr.py_"
    return md_table(
        ["Rekordy", "PG COPY [s]", "PG tsvector [s]", "PG GIN [s]", "PG razem [s]",
         "Solr wysyłanie [s]", "Solr commit [s]", "Solr razem [s]", "Rozmiar PG", "Rozmiar Solr"],
        [[f"{pg['rows']:,}", pg["copy_s"], pg["tsvector_s"], pg["gin_index_s"], pg["total_s"],
          solr["send_s"], solr["commit_s"], solr["total_s"], pg["table_size_total"], f"{solr['index_size_mb']} MB"]])


def main():
    with pg_connect() as conn:
        result = {"versions": versions(conn), "timings": measure(conn)}
    save_json("bench.json", result)

    rows = []
    for q in QUERIES:
        t = result["timings"][q["key"]]
        rows.append([q["label"], f"{t['LIKE']['server_ms']:.1f}", f"{t['PostgreSQL FTS']['server_ms']:.1f}",
                     t["Solr"]["server_ms"], f"{t['Solr']['client_ms']:.1f}",
                     f"{t['LIKE']['count']:,}", f"{t['PostgreSQL FTS']['count']:,}"])
    md = "\n\n".join([
        "## Czasy ładowania i indeksowania", load_table(),
        "## Czasy zapytań – mediana z 5 uruchomień [ms]",
        md_table(["Zapytanie", "LIKE", "PG FTS", "Solr QTime", "Solr klient", "Wyniki LIKE", "Wyniki FTS"], rows),
        f"Wersje: {result['versions']}"])
    (RESULTS / "bench.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
