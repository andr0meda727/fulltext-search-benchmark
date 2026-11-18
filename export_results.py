"""Export lab results: tables, scaling chart, exact queries and raw measurements."""
import csv
import json
import os
import shutil
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".cache" / "matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from grade5 import OUT, ROOT, CSV, records

DEST = ROOT / "output" / "grade5"


def read(name):
    return json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))


def fmt(value):
    if value is None:
        return "brak"
    return f"{value:,.2f}".replace(",", " ")


def md_table(header, rows):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|"*len(header)] +
                     ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |" for row in rows])


def main():
    scale, facets, collapse, paging, env = [read(n) for n in ["scaling", "facets", "collapse", "pagination", "environment"]]
    assert len(scale) == 3 and scale[-1]["rows"] >= 500_000
    assert all("ILIKE" not in q["LIKE"]["sql"] for g in scale for q in g["queries"].values()), "Rerun scaling with LIKE before exporting"
    grouped = {"main_cat": {}, "sub_cat": {}, "year": {}}
    for main_cat, sub_cat, year, count in facets["sql_combined"]["result"]:
        key, value = ("main_cat", main_cat) if main_cat is not None else (("sub_cat", sub_cat) if sub_cat is not None else ("year", year))
        grouped[key][str(value)] = count
    for key in grouped:
        assert grouped[key] == {str(k): v for k, v in facets["sql"][key]["result"]}, f"GROUPING SETS mismatch: {key}"
    assert facets["control_all_docs"]["equal"] and collapse["all_original_ids_verified"] and paging["no_duplicates_or_skips"]
    DEST.mkdir(parents=True, exist_ok=True)
    raw = DEST / "raw"
    raw.mkdir(exist_ok=True)
    for file in OUT.glob("*.json"):
        shutil.copy2(file, raw / file.name)
    if (OUT / "source_card.md").exists():
        shutil.copy2(OUT / "source_card.md", raw / "source_card.md")
    example_stream = records(limit=1)
    example_record = next(example_stream)
    example_stream.close()
    (DEST / "przyklad_rekordu.json").write_text(json.dumps(example_record, ensure_ascii=False, indent=2), encoding="utf-8")

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.facecolor": "white"})
    fig, axes = plt.subplots(1, 3, figsize=(10, 4), constrained_layout=True)
    palette = {"LIKE": "#ba453b", "PostgreSQL FTS": "#1b7392", "Solr": "#318455"}
    for index, ax in enumerate(axes):
        key = f"Q{index+1}"
        for engine, color in palette.items():
            values = [g["queries"][key][engine]["server_ms"] for g in scale]
            ax.plot([g["rows"]/1000 for g in scale], values, marker="o", color=color, label=engine, linewidth=2)
        ax.set_title(["Q1: galaxy", 'Q2: "dark matter" + filtr', "Q3: logika i negacja"][index])
        ax.set_xlabel("Liczba dokumentów [tys.]")
        ax.set_ylabel("Mediana czasu serwera [ms]")
        ax.set_yscale("symlog", linthresh=1)
        ax.grid(alpha=.18)
    axes[0].legend(fontsize=10)
    fig.suptitle("Skalowanie: fizyczne indeksy 10%, 50% i 100%\n1 rozgrzewka + 5 pomiarów; skala Y symlog (liniowa do 1 ms)", fontsize=12)
    fig.savefig(DEST / "skalowanie.png", dpi=180)
    plt.close(fig)

    scaling_rows, counts_rows = [], []
    for group in scale:
        for key, values in group["queries"].items():
            scaling_rows.append([f"{group['rows']:,}".replace(",", " "), key] + [fmt(values[e]["server_ms"]) for e in palette] + [fmt(values["Solr"]["client_ms"])])
            counts_rows.append([group["rows"], key, values["LIKE"]["result"][0][0],
                                values["PostgreSQL FTS"]["result"][0][0], values["Solr"]["response"]["response"]["numFound"]])
    facet_rows = [[name, fmt(data["server_ms"]), fmt(data["client_ms"])] for name, data in facets["sql"].items()]
    facet_rows += [["SQL GROUPING SETS (całość)", fmt(facets["sql_combined"]["server_ms"]), fmt(facets["sql_combined"]["client_ms"])],
                   ["Solr: 3 facety razem", fmt(facets["solr"]["server_ms"]), fmt(facets["solr"]["client_ms"])],
                   ["SQL: 3 zapytania kolejno", "-", fmt(facets["sql_workload_client_ms"])]]
    collapse_rows = []
    for name, data in collapse["queries"].items():
        a, b = data["plain"], data["collapsed"]
        collapse_rows.append([name, a["response"]["response"]["numFound"], b["response"]["response"]["numFound"],
                              f"{data['top10_unique_plain']} / {data['top10_unique_collapsed']}", fmt(a["server_ms"]), fmt(b["server_ms"])])
    paging_rows = [[p["page_1_based"], p["offset"], fmt(p["offset_method"]["server_ms"]),
                    fmt(p["cursor_method"]["server_ms"]), fmt(p["cursor_setup_client_ms"])] for p in paging["pages"]]

    conclusions = []
    for key, values in scale[-1]["queries"].items():
        like, fts, solr = [values[e]["server_ms"] for e in palette]
        conclusions.append(f"{key}, {scale[-1]['rows']:,} dokumentów: LIKE {fmt(like)} ms, PostgreSQL FTS {fmt(fts)} ms, Solr {fmt(solr)} ms. "
                           + (f"FTS skrócił czas LIKE {like/fts:.1f} razy." if fts > 0 else "Czas FTS poniżej rozdzielczości pomiaru."))
    conclusions.append("Nie wyznaczono uniwersalnego progu liczby dokumentów. Dopuszczalny czas zależy od zapytania, sprzętu, współbieżności i wymagań aplikacji. "
                       "Test ma jednego klienta i rozgrzany cache; nie jest testem obciążenia wielu użytkowników. LIKE nie zapewnia tej samej semantyki co stemming i tokenizacja FTS.")
    conclusions.append("Indeks FTS w bazie wystarcza, gdy zmierzone opóźnienie i funkcje SQL spełniają wymagania aplikacji. "
                       "Solr daje gotowe facety, collapse i kursory; same czasy z tych pomiarów nie dowodzą jego przewagi dla każdego zapytania.")
    conclusions.append("Serie Solra nie muszą rosnąć monotonicznie. Niższy czas dla większego zbioru w pojedynczym punkcie nie dowodzi, że więcej danych przyspiesza zapytanie. "
                       "Możliwe czynniki to rozrzut czasu, cache, układ segmentów i praca JVM w tle; nie izolowano ich osobnymi eksperymentami.")
    last = paging["pages"][-1]
    conclusions.append(f"Daleka strona {last['page_1_based']}: start/rows {fmt(last['offset_method']['server_ms'])} ms, "
                       f"kursor z gotowym znacznikiem {fmt(last['cursor_method']['server_ms'])} ms. "
                       f"Zdobycie znacznika wymagało {last['cursor_setup_requests']} wcześniejszych żądań i {fmt(last['cursor_setup_client_ms'])} ms po stronie klienta. "
                       "Kursor służy do kolejnego przeglądania/eksportu; nie zapewnia bezpośredniego skoku na dowolną stronę.")

    queries = []
    urls = []
    for group in scale:
        for key, values in group["queries"].items():
            for engine in ["LIKE", "PostgreSQL FTS"]:
                queries.append(f"-- {group['rows']} dokumentów; {key}; {engine}\n{values[engine]['sql']};")
            urls.append(f"Skalowanie {group['rows']} {key}\n{values['Solr']['url']}")
    for name, data in facets["sql"].items():
        queries.append(f"-- Facet vs GROUP BY: {name}\n{data['sql']};")
    queries.append("-- Jedno zapytanie SQL z trzema agregacjami\n"+facets["sql_combined"]["sql"]+";")
    urls.append("Facety\n"+facets["solr"]["url"])
    for name, data in collapse["queries"].items():
        for method in ["plain", "collapsed"]:
            urls.append(f"Collapse {name} {method}\n{data[method]['url']}")
    for p in paging["pages"]:
        for method in ["offset_method", "cursor_method"]:
            urls.append(f"Strona {p['page_1_based']} {method}\n{p[method]['url']}")
    (DEST / "zapytania.sql").write_text("\n\n".join(queries), encoding="utf-8")
    (DEST / "solr_urls.txt").write_text("\n\n".join(urls), encoding="utf-8")

    facet_details = []
    for field in ["main_cat", "sub_cat", "year"]:
        comp = facets["comparison"][field]
        keys = sorted(comp["sql"].keys() | comp["solr"].keys(), key=(int if field == "year" else str))
        facet_details += [f"### Liczebności: {field}", md_table(["Koszyk", "SQL", "Solr"],
            [[k, comp["sql"].get(k, 0), comp["solr"].get(k, 0)] for k in keys])]
    like_note = "LIKE rozróżnia wielkość liter i wyszukuje podciągi w oryginalnym title i abstract; nie użyto ILIKE ani lower()."
    if scale[0]["queries"]["Q1"]["LIKE"].get("measured_utc"):
        like_note += " Serię LIKE zmierzono ponownie " + scale[0]["queries"]["Q1"]["LIKE"]["measured_utc"][:10] + "; serie FTS i Solra pochodzą z pierwotnych pomiarów. Dla LIKE odtworzono fizyczne podzbiory z pełnej tabeli i potwierdzono ich identyfikatory SHA256."
    md = ["# Wyniki części na 5.0", "Rzeczywiste wyniki z lokalnego uruchomienia.", like_note,
          "## Skalowanie - mediana czasu serwera [ms]", md_table(["Dokumenty", "Q", "LIKE", "PG FTS", "Solr QTime", "Solr klient"], scaling_rows),
          "![Skalowanie](skalowanie.png)", "## Liczby trafień", md_table(["Dokumenty", "Q", "LIKE", "PG FTS", "Solr"], counts_rows),
          "## Facety kontra GROUP BY [ms]", md_table(["Operacja", "Serwer", "Klient"], facet_rows),
          "Zgodność facetów dla zapytania pełnotekstowego: "+str({k: v["equal"] for k, v in facets["comparison"].items()}),
          "Kontrola agregacji dla identycznego zbioru (*:* / TRUE): zgodna we wszystkich koszykach.",
          *facet_details,
          "## Collapse", md_table(["Q", "Przed", "Po", "Unikalne top10 przed/po", "Przed [ms]", "Po [ms]"], collapse_rows),
          "## Głębokie stronicowanie", md_table(["Strona", "start", "start QTime", "cursor QTime", "Przygotowanie kursora [ms klient]"], paging_rows),
          "## Wnioski", *conclusions,
          "## Odtwarzalność", "Surowe próbki, plany EXPLAIN, pełne odpowiedzi Solra i konfiguracja: raw/. Dokładne SQL: zapytania.sql. URL: solr_urls.txt."]
    (DEST / "wyniki.md").write_text("\n\n".join(md), encoding="utf-8")
    with (DEST / "skalowanie.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["rows", "query", "like_server_ms", "pg_fts_server_ms", "solr_qtime_ms", "solr_client_ms"])
        for group in scale:
            for key, values in group["queries"].items():
                writer.writerow([group["rows"], key] + [values[e]["server_ms"] for e in palette]
                                + [values["Solr"]["client_ms"]])

    print(DEST / "wyniki.md")


if __name__ == "__main__":
    main()
