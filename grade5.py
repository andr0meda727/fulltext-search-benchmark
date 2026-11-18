"""Only the grade-5 additions: scaling, aggregate facets, collapse and deep paging.

Run with the isolated docker-compose.grade5.yml stack; see README.md.
Raw samples, exact SQL/URLs and assertions are persisted under results/grade5.
"""
import argparse
import csv
import gzip
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time

os.environ.setdefault("PG_DSN", "postgresql://lab3:lab3@localhost:15432/lab3")
os.environ.setdefault("SOLR_URL", "http://localhost:58983/solr")

import requests
from psycopg import sql as psql
from common import QUERIES, ROOT, SOLR, pg_connect
from load_solr import to_doc

OUT = ROOT / "results" / "grade5"
LOCAL = ROOT / ".grade5.local.json"
LOCAL_CONFIG = json.loads(LOCAL.read_text(encoding="utf-8")) if LOCAL.exists() else {}
if LOCAL_CONFIG.get("storage_root"):
    os.environ.setdefault("GRADE5_STORAGE_ROOT", LOCAL_CONFIG["storage_root"])
CSV = Path(os.environ.get("GRADE5_CSV", LOCAL_CONFIG.get("csv", str(ROOT / "data" / "arxiv.csv"))))
if not CSV.exists() and CSV.with_suffix(CSV.suffix + ".gz").exists():
    CSV = CSV.with_suffix(CSV.suffix + ".gz")
TABLE = CORE = "grade5_arxiv"
SESSION = requests.Session()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def api(path, params=None, body=None):
    # Retry only idempotent document batches, never timed search requests.
    retryable = isinstance(body, list) and path.endswith("/update")
    for attempt in range(9 if retryable else 1):
        try:
            r = SESSION.get(f"{SOLR}/{path}", params=params, timeout=(10, 300)) if body is None else SESSION.post(
                f"{SOLR}/{path}", params=params, json=body, timeout=(10, 300))
            if not retryable or r.status_code not in [429, 502, 503, 504] or attempt == 8:
                break
        except requests.RequestException:
            if not retryable or attempt == 8:
                raise
        delay = min(2**(attempt+1), 30)
        print(f"Transient update failure; retry in {delay}s (same document IDs)", flush=True)
        time.sleep(delay)
    if not r.ok:
        raise RuntimeError(f"{r.url}: {r.status_code} {r.text[:2000]}")
    result = r.json()
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def select(params, core=CORE):
    started = time.perf_counter()
    response = api(f"{core}/select", params)
    elapsed = (time.perf_counter() - started) * 1000
    if response["responseHeader"].get("partialResults"):
        raise RuntimeError("Partial Solr results cannot be used in a benchmark")
    return response, elapsed


def url(params, core=CORE):
    return requests.Request("GET", f"{SOLR}/{core}/select", params=params).prepare().url


def sql_once(conn, statement):
    t = time.perf_counter()
    rows = conn.execute(statement).fetchall()
    elapsed = (time.perf_counter()-t)*1000
    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement).fetchone()[0][0]
    return rows, elapsed, plan


def measure_sql(conn, statement, runs=5):
    conn.execute(statement).fetchall()
    samples, expected, plans = [], None, []
    for _ in range(runs):
        rows, client_ms, plan = sql_once(conn, statement)
        if expected is not None and rows != expected:
            raise AssertionError("SQL results changed during measurement")
        expected = rows
        samples.append({"client_ms": client_ms, "server_ms": plan["Execution Time"]})
        plans.append(plan)
    return {"sql": statement, "result": expected, "samples": samples, "plans": plans,
            "server_ms": statistics.median(s["server_ms"] for s in samples),
            "client_ms": statistics.median(s["client_ms"] for s in samples)}


def measure_solr(params, core=CORE, runs=5):
    select(params, core)
    samples, response = [], None
    for _ in range(runs):
        response, client_ms = select(params, core)
        samples.append({"server_ms": response["responseHeader"]["QTime"], "client_ms": client_ms,
                        "numFound": response["response"]["numFound"]})
    if len({s["numFound"] for s in samples}) != 1:
        raise AssertionError("Solr results changed during measurement")
    return {"params": params, "url": url(params, core), "response": response, "samples": samples,
            "server_ms": statistics.median(s["server_ms"] for s in samples),
            "client_ms": statistics.median(s["client_ms"] for s in samples)}


def create_core(core):
    # This namespace is owned solely by these experiments.
    existing = api("admin/collections", {"action": "LIST"})["collections"]
    if core in existing:
        api("admin/collections", {"action": "DELETE", "name": core})
    # With no configName Solr 10 creates a private mutable copy of _default.
    api("admin/collections", {"action": "CREATE", "name": core, "numShards": 1,
                              "replicationFactor": 1, "waitForFinalState": "true"})
    print(f"Created collection {core} through the Collections API", flush=True)
    api(f"{core}/config", body={"set-user-property": {"update.autoCreateFields": "false"}})
    schema = json.loads((ROOT / "solr" / "schema.json").read_text(encoding="utf-8"))
    schema["add-field"] += [
        {"name": "original_id", "type": "string", "indexed": True, "stored": True, "docValues": True},
        {"name": "copy_no", "type": "pint", "indexed": True, "stored": True, "docValues": True}]
    api(f"{core}/schema", body=schema)
    # Explicitly disable complete result and filter-set caching. File/OS caches stay warm.
    # No random negative query terms: those can change Boolean query semantics.
    api(f"{core}/config", body={"set-property": {"query.queryResultCache.size": 0, "query.filterCache.size": 0}})
    api("admin/collections", {"action": "RELOAD", "name": core})
    overlay = api(f"{core}/config/overlay")
    save(f"config_{core}", {"overlay": overlay, "schema": api(f"{core}/schema")})


def records(max_part=100, limit=None):
    n = 0
    opener = gzip.open if CSV.suffix == ".gz" else open
    with opener(CSV, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if int(row["part"]) >= max_part:
                continue
            yield row
            n += 1
            if limit and n >= limit:
                return


def load_subset(max_part, resume=False):
    print(f"Loading physical subset part < {max_part}", flush=True)
    start = time.perf_counter()
    with pg_connect() as conn:
        reused = False
        copy_s = tsv_s = gin_s = None
        if resume and conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0]:
            expected = sum(1 for _ in records(max_part))
            state = conn.execute(f"SELECT count(*),count(*) FILTER (WHERE part >= {max_part}) FROM {TABLE}").fetchone()
            index_exists = conn.execute("SELECT to_regclass(%s)", (TABLE+"_tsv_gin",)).fetchone()[0]
            reused = state == (expected, 0) and bool(index_exists)
        if not reused:
            conn.execute((ROOT / "sql" / "01_table.sql").read_text(encoding="utf-8").replace("{table}", TABLE))
            with conn.cursor() as cur, cur.copy(f"COPY {TABLE} FROM STDIN") as copy:
                for row in records(max_part):
                    copy.write_row(tuple(row[k] for k in ["id", "title", "abstract", "categories", "main_cat", "sub_cat",
                                                          "license", "year", "pub_date", "n_authors", "part"]))
            copy_s = time.perf_counter()-start
            print(f"PostgreSQL COPY: {copy_s:.1f}s", flush=True)
            t = time.perf_counter()
            conn.execute((ROOT / "sql" / "02_tsvector.sql").read_text(encoding="utf-8").replace("{table}", TABLE))
            tsv_s = time.perf_counter()-t
            print(f"PostgreSQL tsvector: {tsv_s:.1f}s", flush=True)
            t = time.perf_counter()
            conn.execute((ROOT / "sql" / "03_gin_index.sql").read_text(encoding="utf-8").replace("{table}", TABLE))
            gin_s = time.perf_counter()-t
            print(f"PostgreSQL GIN: {gin_s:.1f}s", flush=True)
            conn.execute("CHECKPOINT")
        else:
            print("Resuming Solr import; complete PostgreSQL subset retained", flush=True)
            previous = OUT / f"pg_load_state_p{max_part}.json"
            if previous.exists():
                previous = json.loads(previous.read_text(encoding="utf-8"))
                copy_s, tsv_s, gin_s = (previous.get(k) for k in ["copy_s", "tsv_s", "gin_s"])
        pg_stats = conn.execute(f"SELECT count(*),sum(hashtextextended(id,0)::numeric),pg_total_relation_size('{TABLE}') FROM {TABLE}").fetchone()
    save(f"pg_load_state_p{max_part}", {"rows": pg_stats[0], "copy_s": copy_s, "tsv_s": tsv_s,
                                      "gin_s": gin_s, "reused": reused})
    # A resumed import still rebuilds Solr, so deleted old versions cannot bias
    # the scaling comparison. Only the complete PostgreSQL subset is reused.
    create_core(CORE)
    start = time.perf_counter()
    count = 0
    batch = []
    checksum = hashlib.sha256()
    for row in records(max_part):
        document = to_doc(row)
        document.update(original_id=row["id"], copy_no=0)
        batch.append(document)
        checksum.update((row["id"]+"\n").encode())
        count += 1
        if len(batch) == 1000:
            api(f"{CORE}/update", {"commit": "false"}, body=batch)
            batch = []
            if count % 25_000 == 0:
                print(f"Solr: {count:,}", flush=True)
    if batch:
        api(f"{CORE}/update", {"commit": "false"}, body=batch)
    send_s = time.perf_counter()-start
    t = time.perf_counter()
    api(f"{CORE}/update", body={"commit": {"waitSearcher": True}})
    commit_s = time.perf_counter()-t
    found = select({"q": "*:*", "rows": 0})[0]["response"]["numFound"]
    assert found == count == pg_stats[0], (found, count, pg_stats)
    # Full sorted ID digest on both sides verifies the identity of the indexed sets.
    with pg_connect() as conn:
        pg_ids = hashlib.sha256()
        for (identifier,) in conn.execute(f"SELECT id FROM {TABLE} ORDER BY id COLLATE \"C\""):
            pg_ids.update((identifier+"\n").encode())
    solr_ids = hashlib.sha256()
    cursor = "*"
    while True:
        result = select({"q": "*:*", "rows": 5000, "fl": "id", "sort": "id asc", "cursorMark": cursor})[0]
        for doc in result["response"]["docs"]:
            solr_ids.update((doc["id"]+"\n").encode())
        next_cursor = result["nextCursorMark"]
        if next_cursor == cursor:
            break
        cursor = next_cursor
    assert pg_ids.hexdigest() == solr_ids.hexdigest(), "Different indexed IDs"
    result = {"max_part": max_part, "rows": count, "pg_copy_s": copy_s, "pg_tsv_s": tsv_s,
              "pg_gin_s": gin_s, "pg_total_bytes": pg_stats[2], "solr_send_s": send_s,
              "solr_commit_s": commit_s, "source_id_sha256": checksum.hexdigest(),
              "sorted_pg_solr_id_sha256": pg_ids.hexdigest(), "ids_equal": True}
    result["resumed_import"] = reused
    save(f"load_p{max_part}", result)
    return result


def where(q):
    return f"tsv @@ to_tsquery('english', '{q['tsquery']}') {q['sql_filter']}"


def search_params(q, rows=0):
    params = [("q", q["solr_q"]), ("df", "text_all"), ("rows", rows)]
    if q["fq"]:
        params.append(("fq", q["fq"]))
    return params


def scaling(runs, parts=(10, 50, 100), resume=False):
    result = json.loads((OUT / "scaling.json").read_text(encoding="utf-8")) if resume and (OUT / "scaling.json").exists() else []
    result = [g for g in result if g["part"] not in parts]
    for part in parts:
        load = load_subset(part, resume=resume)
        group = {"part": part, "rows": load["rows"], "queries": {}}
        with pg_connect() as conn:
            for q in QUERIES:
                like = measure_sql(conn, f"SELECT count(*) FROM {TABLE} WHERE {q['like_where']}", runs)
                fts = measure_sql(conn, f"SELECT count(*) FROM {TABLE} WHERE {where(q)}", runs)
                solr = measure_solr(search_params(q), runs=runs)
                group["queries"][q["key"]] = {"LIKE": like, "PostgreSQL FTS": fts, "Solr": solr}
                print(f"{part}% {q['key']}: LIKE {like['server_ms']:.2f}; FTS {fts['server_ms']:.2f}; Solr {solr['server_ms']:.2f} ms", flush=True)
                save(f"scaling_p{part}", group)
        result.append(group)
        result.sort(key=lambda group: group["part"])
        save("scaling", result)
    return result


def paired_counts(flat):
    return dict(zip(flat[::2], flat[1::2]))


def scaling_like(runs):
    """Remeasure only LIKE using physical subsets of the existing full PG table.

    FTS and Solr samples remain unchanged. The smaller tables contain the same
    stored columns (including tsv), not a part filter on a full-size table.
    """
    previous = json.loads((OUT / "scaling.json").read_text(encoding="utf-8"))
    assert [g["part"] for g in previous] == [10, 50, 100]
    table = TABLE + "_like_measurement"
    with pg_connect() as conn:
        assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == previous[-1]["rows"]
        try:
            for group in previous:
                part = group["part"]
                if part < 100:
                    print(f"Preparing physical LIKE subset {part}%", flush=True)
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.execute(f"CREATE TABLE {table} AS SELECT * FROM {TABLE} WHERE part < {part}")
                    conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (id)")
                    conn.execute(f"ANALYZE {table}")
                    target = table
                else:
                    target = TABLE
                count = conn.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
                assert count == group["rows"]
                digest = hashlib.sha256()
                for (identifier,) in conn.execute(f'SELECT id FROM {target} ORDER BY id COLLATE "C"'):
                    digest.update((identifier + "\n").encode())
                load = json.loads((OUT / f"load_p{part}.json").read_text(encoding="utf-8"))
                assert digest.hexdigest() == load["sorted_pg_solr_id_sha256"]
                for query in QUERIES:
                    statement = f"SELECT count(*) FROM {target} WHERE {query['like_where']}"
                    assert "ILIKE" not in statement and " LIKE " in statement
                    result = measure_sql(conn, statement, runs)
                    result.update(measured_utc=datetime.now(timezone.utc).isoformat(),
                                  operator="LIKE", case_sensitive=True,
                                  physical_subset_rows=count, sorted_id_sha256=digest.hexdigest(),
                                  physical_subset_method="CTAS with all stored columns" if part < 100 else "existing full table")
                    group["queries"][query["key"]]["LIKE"] = result
                    print(f"{part}% {query['key']}: LIKE {result['server_ms']:.2f} ms; hits {result['result'][0][0]}", flush=True)
                save(f"scaling_like_p{part}", group)
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    for group in previous:
        save(f"scaling_p{group['part']}", group)
    save("scaling", previous)
    print("LIKE samples replaced; original FTS/Solr samples retained", flush=True)


def facets(runs):
    q = QUERIES[0]
    with pg_connect() as conn:
        low, high = conn.execute(f"SELECT min(year),max(year) FROM {TABLE}").fetchone()
        start, end, gap = low // 5 * 5, (high // 5 + 1) * 5, 5
        statements = {
            "main_cat": f"SELECT main_cat,count(*) FROM {TABLE} WHERE {where(q)} GROUP BY main_cat ORDER BY main_cat COLLATE \"C\"",
            "sub_cat": f"SELECT sub_cat,count(*) FROM {TABLE} WHERE {where(q)} GROUP BY sub_cat ORDER BY sub_cat COLLATE \"C\"",
            "year": f"SELECT {start}+((year-{start})/{gap})*{gap} AS bucket,count(*) FROM {TABLE} WHERE {where(q)} AND year >= {start} AND year < {end} GROUP BY bucket ORDER BY bucket"}
        # Time the equivalent three-query SQL workload as one operation too.
        # One MATERIALIZED match set + GROUPING SETS gives an additional single SQL alternative.
        combined = (f"WITH matches AS MATERIALIZED (SELECT main_cat,sub_cat,{start}+((year-{start})/{gap})*{gap} AS bucket FROM {TABLE} WHERE {where(q)}) "
                    "SELECT main_cat,sub_cat,bucket,count(*) FROM matches GROUP BY GROUPING SETS ((main_cat),(sub_cat),(bucket)) ORDER BY main_cat NULLS FIRST,sub_cat NULLS FIRST,bucket NULLS FIRST")
        sql_results = {name: measure_sql(conn, statement, runs) for name, statement in statements.items()}
        combined_results = measure_sql(conn, combined, runs)
        for statement in statements.values():
            conn.execute(statement).fetchall()
        workload_samples = []
        for _ in range(runs):
            t = time.perf_counter()
            for statement in statements.values():
                conn.execute(statement).fetchall()
            workload_samples.append((time.perf_counter()-t)*1000)
    params = search_params(q) + [("facet", "true"), ("facet.field", "main_cat"), ("facet.field", "sub_cat"),
        ("facet.limit", -1), ("facet.mincount", 1), ("facet.sort", "index"), ("facet.range", "year"),
        ("facet.range.start", start), ("facet.range.end", end), ("facet.range.gap", gap),
        ("facet.range.include", "lower"), ("facet.range.hardend", "true"), ("facet.range.method", "dv")]
    solr = measure_solr(params, runs=runs)
    counts = solr["response"]["facet_counts"]
    comparisons = {}
    for name in ["main_cat", "sub_cat", "year"]:
        raw = counts["facet_ranges"]["year"]["counts"] if name == "year" else counts["facet_fields"][name]
        actual = {str(k): v for k, v in paired_counts(raw).items() if v}
        expected = {str(k): v for k, v in sql_results[name]["result"] if v}
        comparisons[name] = {"sql": expected, "solr": actual, "equal": expected == actual}
        # Different stemming/tokenization can change the result set: report actual differences, never conceal them.
        comparisons[name]["differences"] = {k: {"sql": expected.get(k, 0), "solr": actual.get(k, 0)}
            for k in sorted(expected.keys() | actual.keys()) if expected.get(k, 0) != actual.get(k, 0)}
    # A controlled same-document-set check isolates aggregation from analyzer differences.
    control_params = [(k, "*:*" if k == "q" else v) for k, v in params]
    control = measure_solr(control_params, runs=runs)
    with pg_connect() as conn:
        control_sql = {name: measure_sql(conn, statement.replace(where(q), "TRUE"), runs)
                       for name, statement in statements.items()}
    control_counts = control["response"]["facet_counts"]
    for name in ["main_cat", "sub_cat", "year"]:
        flat = control_counts["facet_ranges"]["year"]["counts"] if name == "year" else control_counts["facet_fields"][name]
        actual = {str(k): v for k, v in paired_counts(flat).items() if v}
        expected = {str(k): v for k, v in control_sql[name]["result"] if v}
        assert actual == expected, f"Aggregation mismatch for identical documents: {name}"
    result = {"query": q, "range": {"start": start, "end": end, "gap": gap, "interval": "[lower,upper)"},
              "sql": sql_results, "sql_combined": combined_results, "sql_workload_client_samples": workload_samples,
              "sql_workload_client_ms": statistics.median(workload_samples), "solr": solr,
              "comparison": comparisons, "control_all_docs": {"solr": control, "sql": control_sql, "equal": True}}
    save("facets", result)
    print("Facets complete; identical-document control passed", flush=True)
    return result


def collapse(runs):
    core = "grade5_duplicates"
    create_core(core)
    originals = 10_000
    copies = 3
    batch, ids = [], set()
    for row in records(limit=originals):
        ids.add(row["id"])
        for copy_no in range(copies):
            doc = to_doc(row)
            doc.update(id=f"{row['id']}__copy{copy_no}", original_id=row["id"], copy_no=copy_no)
            batch.append(doc)
        if len(batch) >= 999:
            api(f"{core}/update", {"commit": "false"}, body=batch)
            batch = []
    if batch:
        api(f"{core}/update", {"commit": "false"}, body=batch)
    api(f"{core}/update", body={"commit": {"waitSearcher": True}})
    outputs = {}
    for key, query in [("all", "*:*"), ("Q1", "galaxy")]:
        base = [("q", query), ("df", "text_all"), ("rows", 10),
                ("fl", "id,original_id,copy_no,title"), ("sort", "original_id asc,copy_no asc,id asc"),
                ("facet", "true"), ("facet.field", "main_cat"), ("facet.limit", -1), ("facet.mincount", 1)]
        plain = measure_solr(base, core, runs)
        collapsed = measure_solr(base+[("fq", "{!collapse field=original_id sort='copy_no asc,id asc'}")], core, runs)
        n_plain = plain["response"]["response"]["numFound"]
        n_collapsed = collapsed["response"]["response"]["numFound"]
        assert n_plain == copies*n_collapsed and n_collapsed > 0
        docs = collapsed["response"]["response"]["docs"]
        assert len({d["original_id"] for d in docs}) == len(docs)
        assert all(d["copy_no"] == 0 for d in docs)
        f_plain = paired_counts(plain["response"]["facet_counts"]["facet_fields"]["main_cat"])
        f_collapsed = paired_counts(collapsed["response"]["facet_counts"]["facet_fields"]["main_cat"])
        assert f_plain == {k: copies*v for k, v in f_collapsed.items()}
        outputs[key] = {"plain": plain, "collapsed": collapsed, "counts_factor_verified": copies,
                        "top10_unique_plain": len({d["original_id"] for d in plain["response"]["response"]["docs"]}),
                        "top10_unique_collapsed": len({d["original_id"] for d in docs})}
    # Validate the COMPLETE collapsed result stream, not only the top ten.
    # collapse + cursorMark support varies; retrieve the small controlled set at once.
    all_docs = select({"q": "*:*", "rows": originals, "fq": "{!collapse field=original_id}",
                       "fl": "original_id", "sort": "id asc"}, core)[0]["response"]["docs"]
    assert len(all_docs) == len(ids) == originals
    assert {d["original_id"] for d in all_docs} == ids
    result = {"originals": originals, "copies_per_original": copies, "indexed_docs": originals*copies,
              "all_original_ids_verified": True, "queries": outputs}
    save("collapse", result)
    print("Collapse: full set and 3x facet counts verified", flush=True)
    return result


def pagination(runs):
    # Stable sort includes uniqueKey; unchanged index throughout the experiment.
    rows = 1000
    total = select({"q": "*:*", "rows": 0})[0]["response"]["numFound"]
    offsets = sorted({0, total//2//rows*rows, (total-rows)//rows*rows})
    params = [("q", "*:*"), ("sort", "year asc,id asc"), ("fl", "id,year"), ("rows", rows)]
    # Store real cursor marks by traversing all preceding pages. This setup cost is reported.
    cursor = "*"
    offset = 0
    marks, traversals, seen = {}, [], set()
    while offset <= max(offsets):
        if offset in offsets:
            marks[offset] = cursor
        result, elapsed = select(params+[("cursorMark", cursor)])
        docs = result["response"]["docs"]
        new_ids = {d["id"] for d in docs}
        assert len(new_ids) == len(docs) and not (seen & new_ids)
        seen.update(new_ids)
        traversals.append({"offset": offset, "client_ms": elapsed, "server_ms": result["responseHeader"]["QTime"],
                           "url": url(params+[("cursorMark", cursor)]), "count": len(docs)})
        next_cursor = result["nextCursorMark"]
        if not docs or cursor == next_cursor:
            break
        cursor = next_cursor
        offset += len(docs)
        if len(traversals) % 50 == 0:
            print(f"Cursor traversal: {len(seen):,}/{total:,}", flush=True)
    results = []
    for offset in offsets:
        by_offset = measure_solr(params+[("start", offset)], runs=runs)
        by_cursor = measure_solr(params+[("cursorMark", marks[offset])], runs=runs)
        a = [d["id"] for d in by_offset["response"]["response"]["docs"]]
        b = [d["id"] for d in by_cursor["response"]["response"]["docs"]]
        assert a == b and a, f"Different pages at offset={offset}"
        cost = sum(t["client_ms"] for t in traversals if t["offset"] < offset)
        results.append({"offset": offset, "page_1_based": offset//rows+1, "offset_method": by_offset,
                        "cursor_method": by_cursor, "equal_ids": True, "cursor_setup_requests": offset//rows,
                        "cursor_setup_client_ms": cost, "cursor_setup_plus_page_client_ms": cost+by_cursor["client_ms"]})
    assert len(seen) == total, (len(seen), total)
    # Confirm end-of-stream cursor termination.
    terminal = select(params+[("cursorMark", cursor)])[0]
    assert not terminal["response"]["docs"] and terminal["nextCursorMark"] == cursor
    result = {"query": "*:*", "sort": "year asc,id asc", "page_size": rows, "total": total,
              "complete_traversal_unique_ids": len(seen), "no_duplicates_or_skips": True,
              "traversal": traversals, "pages": results,
              "note": "Page timing assumes saved cursor. Cursor setup walks every preceding page; random access uses start."}
    save("pagination", result)
    print("Pagination: equal pages and complete traversal verified", flush=True)
    return result


def environment():
    with pg_connect() as conn:
        pg = conn.execute("SELECT version()").fetchone()[0]
    info = api("admin/info/system")
    result = {"utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "python": platform.python_version(), "postgres": pg, "solr": info["lucene"],
              "packages": {name: version(name) for name in ["psycopg", "requests", "matplotlib", "reportlab", "pyarrow"]},
              "jvm": info["jvm"], "docker": json.loads(subprocess.run(
                  ["docker", "info", "--format", "{{json .}}"], check=True, capture_output=True, text=True).stdout),
              "cache_policy": "queryResultCache.size=0, filterCache.size=0, warm JVM/OS caches",
              "effective_query_config": api(f"{CORE}/config")["config"]["query"],
              "csv_path": str(CSV), "storage_root": os.environ.get("GRADE5_STORAGE_ROOT", "Docker named volumes"),
              "method": "1 warm-up + 5 samples; PG EXPLAIN ANALYZE BUFFERS; Solr QTime; separate client timing; sequential workloads"}
    # Avoid recording unrelated Docker registry details or proxy configuration.
    result["docker"] = {k: result["docker"].get(k) for k in ["NCPU", "MemTotal", "Driver", "KernelVersion", "OperatingSystem", "ServerVersion"]}
    result["docker_images"] = subprocess.run(["docker", "image", "inspect", "solr:10", "postgres:17", "--format", "{{json .RepoDigests}}"],
                                              check=True, capture_output=True, text=True).stdout.strip()
    if os.name == "nt":
        command = "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress"
        result["cpu"] = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout.strip()
        command = "Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | ConvertTo-Json -Compress"
        result["host_ram"] = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout.strip()
        command = "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size | ConvertTo-Json -Compress"
        result["disks"] = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout.strip()
        drive = Path(os.environ.get("GRADE5_STORAGE_ROOT", str(ROOT))).drive
        if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
            command = f"Get-Partition -DriveLetter {drive[0]} | Get-Disk | Select-Object FriendlyName,BusType,Size | ConvertTo-Json -Compress"
            result["index_disk"] = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout.strip()
    manifest = (CSV.with_suffix("") if CSV.suffix == ".gz" else CSV).with_suffix(".manifest.json")
    if manifest.exists():
        result["dataset"] = json.loads(manifest.read_text(encoding="utf-8"))
    save("environment", result)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["all", "scaling", "scaling-like", "facets", "collapse", "pagination", "environment"])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--parts", nargs="+", type=int, choices=[10, 50, 100], default=[10, 50, 100])
    ap.add_argument("--resume", action="store_true", help="Reuse a complete matching PG subset after an interrupted Solr import")
    args = ap.parse_args()
    if args.runs < 5:
        ap.error("Use at least five measured runs")
    deadline = time.monotonic()+240
    while True:
        try:
            with pg_connect() as conn:
                conn.execute("SELECT 1")
            api("admin/info/system")
            break
        except Exception as error:
            if time.monotonic() >= deadline:
                raise RuntimeError("Laboratory services did not become ready") from error
            print("Waiting for PostgreSQL/Solr startup...", flush=True)
            time.sleep(3)
    if args.stage == "scaling-like":
        scaling_like(args.runs)
        return
    if args.stage in ["all", "scaling"]:
        scaling(args.runs, args.parts, args.resume)
    if args.stage in ["all", "facets"]:
        facets(args.runs)
    if args.stage in ["all", "collapse"]:
        collapse(args.runs)
    if args.stage in ["all", "pagination"]:
        pagination(args.runs)
    environment()


if __name__ == "__main__":
    main()
