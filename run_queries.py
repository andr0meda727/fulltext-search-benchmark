"""Functional comparison of PostgreSQL and Solr on the full data set.

Basics: Q1-Q3 in PostgreSQL (tsvector) and Solr (q, fq, fl, sort, rows) + Q1 with LIKE.
     Counts and times from the second run.
More: ts_rank_cd top-10 vs eDisMax (qf with weights) top-10 and their overlap,
     GROUP BY vs facet.field on two fields, highlighting.

Writes results/queries.json and results/queries.md (tables + exact SQL and Solr URLs).
"""
from common import (QUERIES, RESULTS, fts_where, md_table, no_cache, pg_connect, pg_explain_ms, pg_query,
                    save_json, solr_basic_params, solr_select, solr_url)

TABLE = COLLECTION = "arxiv"
EDISMAX_QF = "title^3 abstract"
FACET_QUERY = QUERIES[2]   # Q3 is spread over several categories


def second_run_pg(conn, sql):
    pg_query(conn, sql)
    rows, client = pg_query(conn, sql)
    return rows, pg_explain_ms(conn, sql), client


def second_run_solr(params):
    # The second run bypasses the result cache - see common.no_cache.
    solr_select(params, COLLECTION)
    return solr_select([(k, no_cache(v) if k == "q" else v) for k, v in params], COLLECTION)


def basic_queries(conn, log):
    table = []
    out = []
    for q in QUERIES:
        count_sql = f"SELECT count(*) FROM {TABLE} WHERE {fts_where(q)}"
        list_sql = (f"SELECT id, year, title FROM {TABLE} WHERE {fts_where(q)}"
                    f" ORDER BY year DESC, id COLLATE \"C\" LIMIT 10")
        [(cnt,)], sql_ms, sql_client = second_run_pg(conn, count_sql)
        sql_top = pg_query(conn, list_sql)[0]

        params = solr_basic_params(q)
        js, qtime, solr_client = second_run_solr(params)
        url = solr_url(params, COLLECTION)

        table.append([q["label"], f"{cnt:,}", f"{js['response']['numFound']:,}",
                      f"{sql_ms:.1f}", qtime])
        log += [f"### {q['label']}", "```sql", count_sql + ";", list_sql + ";", "```", f"Solr: <{url}>", ""]
        item = {"query": q["key"], "sql_count": cnt, "sql_ms": sql_ms, "sql_client_ms": sql_client,
                "solr_numFound": js["response"]["numFound"], "solr_qtime": qtime,
                "solr_client_ms": solr_client, "solr_url": url,
                "sql_top10": [list(r) for r in sql_top],
                "solr_top10": [[d["id"], d["year"], d["title"]] for d in js["response"]["docs"]]}

        if q["key"] == "Q1":
            like_sql = f"SELECT count(*) FROM {TABLE} WHERE {q['like_where']}"
            [(like_cnt,)], like_ms, _ = second_run_pg(conn, like_sql)
            table.append([q["label"] + " – LIKE", f"{like_cnt:,}", "–", f"{like_ms:.1f}", "–"])
            log += ["```sql", like_sql + ";", "```", ""]
            item |= {"like_count": like_cnt, "like_ms": like_ms}
        out.append(item)

    md = md_table(["Zapytanie", "Liczba wyników SQL", "numFound Solr", "Czas SQL [ms]", "QTime Solr [ms]"], table)
    return out, md


def ranking(conn, log):
    rows, out = [], []
    for q in QUERIES:
        sql = (f"SELECT id, title, ts_rank_cd(tsv, query) AS rank FROM {TABLE},"
               f" to_tsquery('english', '{q['tsquery']}') query WHERE tsv @@ query {q['sql_filter']}"
               f" ORDER BY rank DESC LIMIT 10")
        sql_top = pg_query(conn, sql)[0]
        params = [("defType", "edismax"), ("q", q["solr_q"]), ("qf", EDISMAX_QF),
                  ("fl", "id,title,score"), ("rows", 10)]
        if q["fq"]:
            params.append(("fq", q["fq"]))
        js, _, _ = solr_select(params, COLLECTION)
        solr_top = js["response"]["docs"]
        common_ids = {r[0] for r in sql_top} & {d["id"] for d in solr_top}
        rows.append([q["label"], len(common_ids)])
        log += [f"### Ranking {q['key']}", "```sql", sql + ";", "```", f"Solr: <{solr_url(params, COLLECTION)}>", ""]
        out.append({"query": q["key"], "overlap": len(common_ids), "sql_top10": [list(r) for r in sql_top],
                    "solr_top10": [[d["id"], d["title"], d["score"]] for d in solr_top]})
    md = md_table(["Zapytanie", "Wspólne dokumenty w top-10 (SQL vs Solr)"], rows)
    for item in out:
        md += f"\n\n**{item['query']}** – top-10\n\n" + md_table(
            ["#", "SQL ts_rank_cd", "rank", "Solr eDisMax", "score"],
            [[i + 1, f"{s[0]} {s[1][:60]}", f"{s[2]:.3f}", f"{o[0]} {o[1][:60]}", f"{o[2]:.2f}"]
             for i, (s, o) in enumerate(zip(item["sql_top10"], item["solr_top10"]))])
    return out, md


def groupby_facets(conn, log):
    q = FACET_QUERY
    sql = (f"SELECT main_cat, count(*) FROM {TABLE} WHERE {fts_where(q)}"
           f" GROUP BY main_cat ORDER BY count(*) DESC LIMIT 10")
    groups = pg_query(conn, sql)[0]
    params = [("q", q["solr_q"]), ("df", "text_all"), ("rows", 3), ("fl", "id,title"),
              ("facet", "true"), ("facet.field", "main_cat"), ("facet.field", "sub_cat"),
              ("facet.limit", 10), ("facet.mincount", 1),
              ("hl", "true"), ("hl.fl", "abstract"), ("hl.snippets", 1), ("hl.fragsize", 200)]
    js, _, _ = solr_select(params, COLLECTION)
    ff = js["facet_counts"]["facet_fields"]
    facets = {f: list(zip(v[::2], v[1::2])) for f, v in ff.items()}
    hl = [{"id": d["id"], "title": d["title"], "snippet": js["highlighting"][d["id"]].get("abstract", [""])[0]}
          for d in js["response"]["docs"]]
    log += ["### GROUP BY / facety / podświetlanie", "```sql", sql + ";", "```",
            f"Solr: <{solr_url(params, COLLECTION)}>", ""]

    md = f"Zapytanie: {q['label']}\n\n" + md_table(
        ["main_cat", "SQL GROUP BY", "Solr facet"],
        [[k, n, dict(facets["main_cat"]).get(k, "–")] for k, n in groups])
    md += "\n\nFacet `sub_cat` (top 10):\n\n" + md_table(["sub_cat", "liczba"], facets["sub_cat"])
    md += "\n\nPodświetlanie (hl.fl=abstract):\n\n" + "\n".join(
        f"- **{h['id']}** {h['title']}: …{h['snippet']}…" for h in hl)
    return {"group_by": groups, "facets": facets, "highlighting": hl}, md


def main():
    log = ["## Dokładne zapytania", ""]
    with pg_connect() as conn:
        basic, md_basic = basic_queries(conn, log)
        rank, md_rank = ranking(conn, log)
        gb, md_gb = groupby_facets(conn, log)

    save_json("queries.json", {"basic": basic, "ranking": rank, "groupby_facets": gb})
    md = "\n\n".join(["## Liczba wyników i czasy (2. uruchomienie)", md_basic,
                      "## Ranking: ts_rank_cd vs eDisMax (qf=title^3 abstract)", md_rank,
                      "## GROUP BY vs facety", md_gb, "\n".join(log)])
    (RESULTS / "queries.md").write_text(md)
    print(md_basic)
    print(f"\nFull results: {RESULTS / 'queries.md'}")


if __name__ == "__main__":
    main()
