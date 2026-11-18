"""Shared configuration, query definitions and timing helpers used by every script."""
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import psycopg
import requests

ROOT = Path(__file__).parent
DATA_CSV = ROOT / "data" / "arxiv.csv"
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)

PG_DSN = os.environ.get("PG_DSN", "postgresql://lab3:lab3@localhost:5432/lab3")
SOLR = os.environ.get("SOLR_URL", "http://localhost:8983/solr")
SOLR_CONTAINER = os.environ.get("SOLR_CONTAINER", "solr")

# Text searched by LIKE - the same fields that make up the tsvector and Solr's text_all.
DOC = "(title || ' ' || abstract)"

# The three test queries, written once for each system.
#   tsquery    - PostgreSQL to_tsquery('english', ...)
#   solr_q     - Solr standard query parser, df=text_all
#   sql_filter / fq - the same structured filter on both sides
#   like_where - the closest LIKE equivalent
QUERIES = [
    {
        "key": "Q1",
        "label": "Q1: słowo w dowolnej formie (galaxy → galaxies)",
        "tsquery": "galaxy",
        "solr_q": "galaxy",
        "sql_filter": "",
        "fq": None,
        "like_where": f"{DOC} LIKE '%galax%'",
    },
    {
        "key": "Q2",
        "label": 'Q2: fraza "dark matter" w kategorii astro-ph',
        "tsquery": "dark <-> matter",
        "solr_q": '"dark matter"',
        "sql_filter": "AND main_cat = 'astro-ph'",
        "fq": "main_cat:astro-ph",
        "like_where": f"{DOC} LIKE '%dark matter%' AND main_cat = 'astro-ph'",
    },
    {
        "key": "Q3",
        "label": "Q3: neural AND (image OR speech) NOT quantum",
        "tsquery": "neural & (image | speech) & !quantum",
        "solr_q": "+neural +(image speech) -quantum",
        "sql_filter": "",
        "fq": None,
        "like_where": (f"{DOC} LIKE '%neural%' AND ({DOC} LIKE '%image%' OR {DOC} LIKE '%speech%')"
                       f" AND {DOC} NOT LIKE '%quantum%'"),
    },
]


def fts_where(q):
    return f"tsv @@ to_tsquery('english', '{q['tsquery']}') {q['sql_filter']}"


def pg_connect():
    # ClientCursor binds parameters on the client, so the logged SQL is exactly what ran.
    return psycopg.connect(PG_DSN, autocommit=True, cursor_factory=psycopg.ClientCursor)


def pg_explain_ms(conn, sql):
    """Server-side execution time reported by EXPLAIN (ANALYZE, BUFFERS)."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        return cur.fetchone()[0][0]["Execution Time"]


def pg_query(conn, sql):
    """Run a query, return (rows, client-side ms)."""
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return rows, (time.perf_counter() - t0) * 1000


def pg_measure(conn, sql, runs=5):
    """One warm-up run, then `runs` timed runs. Returns medians and all samples."""
    pg_query(conn, sql)
    server = [pg_explain_ms(conn, sql) for _ in range(runs)]
    client = [pg_query(conn, sql)[1] for _ in range(runs)]
    return {"server_ms": statistics.median(server), "client_ms": statistics.median(client),
            "server_samples": server, "client_samples": client}


SESSION = requests.Session()


def solr_select(params, collection="arxiv"):
    """GET /select. Returns (json, QTime ms, client-side ms)."""
    t0 = time.perf_counter()
    r = SESSION.get(f"{SOLR}/{collection}/select", params=params)
    client = (time.perf_counter() - t0) * 1000
    if not r.ok:
        raise RuntimeError(f"Solr {r.status_code}: {r.text[:500]}")
    js = r.json()
    return js, js["responseHeader"]["QTime"], client


def solr_url(params, collection="arxiv"):
    """Full URL that can be pasted into a browser."""
    return requests.Request("GET", f"{SOLR}/{collection}/select", params=params).prepare().url


def no_cache(q):
    """Append a clause that matches nothing to change the cache key.

    Without it a repeated query is served from Solr's queryResultCache (QTime ~0 ms),
    which would not be a fair comparison with PostgreSQL, which has no result cache.
    """
    return f"{q} -id:nocache_{uuid.uuid4().hex[:8]}"


def solr_basic_params(q, rows=10):
    """Basic form: q, fq, fl, sort, rows."""
    params = [("q", q["solr_q"]), ("df", "text_all"), ("fl", "id,year,title"),
              ("sort", "year desc,id asc"), ("rows", rows)]
    if q["fq"]:
        params.append(("fq", q["fq"]))
    return params


def solr_measure(params, collection="arxiv", runs=5):
    """One warm-up run, then `runs` timed runs with the result cache bypassed."""
    def once():
        p = [(k, no_cache(v) if k == "q" else v) for k, v in params]
        return solr_select(p, collection)
    once()
    qtimes, clients = [], []
    for _ in range(runs):
        _, qt, cl = once()
        qtimes.append(qt)
        clients.append(cl)
    return {"server_ms": statistics.median(qtimes), "client_ms": statistics.median(clients),
            "server_samples": qtimes, "client_samples": clients}


def save_json(name, data):
    (RESULTS / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def load_json(name):
    path = RESULTS / name
    return json.loads(path.read_text()) if path.exists() else None


def md_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)
