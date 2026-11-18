-- Queries used in the comparison, for the full table "arxiv".
-- The scripts (run_queries.py, bench.py, ...) run exactly these and log them to results/.

-- ===== Basic queries =====
-- Q1: one word in any form (galaxy, galaxies)
SELECT count(*) FROM arxiv WHERE tsv @@ to_tsquery('english', 'galaxy');
-- Q1 with LIKE, for comparison
SELECT count(*) FROM arxiv WHERE (title || ' ' || abstract) LIKE '%galax%';
-- Q2: phrase "dark matter" (words next to each other, in this order), category astro-ph
SELECT count(*) FROM arxiv WHERE tsv @@ to_tsquery('english', 'dark <-> matter') AND main_cat = 'astro-ph';
-- Q3: neural AND (image OR speech) NOT quantum
SELECT count(*) FROM arxiv WHERE tsv @@ to_tsquery('english', 'neural & (image | speech) & !quantum');

-- Results listing (same as Solr fl/sort/rows)
SELECT id, year, title FROM arxiv
WHERE tsv @@ to_tsquery('english', 'galaxy')
ORDER BY year DESC, id COLLATE "C" LIMIT 10;

-- ===== Ranking, GROUP BY, boost =====
-- Ranking: top 10 by ts_rank_cd (title weight A = 1.0, abstract weight B = 0.4)
SELECT id, title, ts_rank_cd(tsv, query) AS rank
FROM arxiv, to_tsquery('english', 'neural & (image | speech) & !quantum') query
WHERE tsv @@ query
ORDER BY rank DESC LIMIT 10;

-- GROUP BY a category over full-text results
SELECT main_cat, count(*) FROM arxiv
WHERE tsv @@ to_tsquery('english', 'neural & (image | speech) & !quantum')
GROUP BY main_cat ORDER BY count(*) DESC LIMIT 10;

-- Extension: boost - relevance multiplied by recency
SELECT id, year, title, ts_rank_cd(tsv, query) * (1 + (year - 1990) / 10.0) AS score
FROM arxiv, to_tsquery('english', 'neural & (image | speech) & !quantum') query
WHERE tsv @@ query
ORDER BY score DESC LIMIT 10;
