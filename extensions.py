"""Extension: boost - relevance multiplied by recency in both systems.

Usage:
    python extensions.py

Writes results/ext_boost.json and .md.
"""
import statistics

from common import QUERIES, RESULTS, md_table, pg_connect, pg_query, save_json, solr_select, solr_url

TABLE = COLLECTION = "arxiv"


def write(name, data, md):
    save_json(f"ext_{name}.json", data)
    (RESULTS / f"ext_{name}.md").write_text(md)
    print(md)


# ---------- boost: relevance multiplied by 1 + (year - 1990) / 10 in both systems ----------

def boost():
    q = QUERIES[2]
    factor_sql = "(1 + (year - 1990) / 10.0)"
    base = (f"SELECT id, year, title, ts_rank_cd(tsv, query){{factor}} AS score FROM {TABLE},"
            f" to_tsquery('english', '{q['tsquery']}') query WHERE tsv @@ query ORDER BY score DESC LIMIT 10")
    sql_plain, sql_boost = base.format(factor=""), base.format(factor=" * " + factor_sql)
    common = [("defType", "edismax"), ("q", q["solr_q"]), ("qf", "title^3 abstract"),
              ("fl", "id,year,title,score"), ("rows", 10)]
    solr_plain, solr_boost = common, common + [("boost", "sum(1,div(sub(year,1990),10))")]

    out, md = {}, [f"Zapytanie: {q['label']}, boost = 1 + (rok − 1990) / 10", ""]
    with pg_connect() as conn:
        for name, sql, params in [("bez boostu", sql_plain, solr_plain), ("z boostem", sql_boost, solr_boost)]:
            sql_top = pg_query(conn, sql)[0]
            solr_top = solr_select(params, COLLECTION)[0]["response"]["docs"]
            out[name] = {"sql": [list(r) for r in sql_top], "solr": solr_top}
            md += [f"### {name}: średni rok w top-10 – SQL {statistics.mean(r[1] for r in sql_top):.1f},"
                   f" Solr {statistics.mean(d['year'] for d in solr_top):.1f}",
                   md_table(["#", "SQL", "Solr"],
                            [[i + 1, f"{s[1]} {s[2][:60]}", f"{d['year']} {d['title'][:60]}"]
                             for i, (s, d) in enumerate(zip(sql_top, solr_top))]),
                   "```sql", sql + ";", "```", f"Solr: <{solr_url(params, COLLECTION)}>", ""]
    write("boost", out, "\n".join(md))


if __name__ == "__main__":
    boost()
