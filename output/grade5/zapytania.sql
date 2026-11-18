-- 50103 dokumentów; Q1; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%galax%';

-- 50103 dokumentów; Q1; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy') ;

-- 50103 dokumentów; Q2; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%dark matter%' AND main_cat = 'astro-ph';

-- 50103 dokumentów; Q2; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'dark <-> matter') AND main_cat = 'astro-ph';

-- 50103 dokumentów; Q3; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%neural%' AND ((title || ' ' || abstract) LIKE '%image%' OR (title || ' ' || abstract) LIKE '%speech%') AND (title || ' ' || abstract) NOT LIKE '%quantum%';

-- 50103 dokumentów; Q3; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'neural & (image | speech) & !quantum') ;

-- 250183 dokumentów; Q1; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%galax%';

-- 250183 dokumentów; Q1; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy') ;

-- 250183 dokumentów; Q2; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%dark matter%' AND main_cat = 'astro-ph';

-- 250183 dokumentów; Q2; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'dark <-> matter') AND main_cat = 'astro-ph';

-- 250183 dokumentów; Q3; LIKE
SELECT count(*) FROM grade5_arxiv_like_measurement WHERE (title || ' ' || abstract) LIKE '%neural%' AND ((title || ' ' || abstract) LIKE '%image%' OR (title || ' ' || abstract) LIKE '%speech%') AND (title || ' ' || abstract) NOT LIKE '%quantum%';

-- 250183 dokumentów; Q3; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'neural & (image | speech) & !quantum') ;

-- 500000 dokumentów; Q1; LIKE
SELECT count(*) FROM grade5_arxiv WHERE (title || ' ' || abstract) LIKE '%galax%';

-- 500000 dokumentów; Q1; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy') ;

-- 500000 dokumentów; Q2; LIKE
SELECT count(*) FROM grade5_arxiv WHERE (title || ' ' || abstract) LIKE '%dark matter%' AND main_cat = 'astro-ph';

-- 500000 dokumentów; Q2; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'dark <-> matter') AND main_cat = 'astro-ph';

-- 500000 dokumentów; Q3; LIKE
SELECT count(*) FROM grade5_arxiv WHERE (title || ' ' || abstract) LIKE '%neural%' AND ((title || ' ' || abstract) LIKE '%image%' OR (title || ' ' || abstract) LIKE '%speech%') AND (title || ' ' || abstract) NOT LIKE '%quantum%';

-- 500000 dokumentów; Q3; PostgreSQL FTS
SELECT count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'neural & (image | speech) & !quantum') ;

-- Facet vs GROUP BY: main_cat
SELECT main_cat,count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy')  GROUP BY main_cat ORDER BY main_cat COLLATE "C";

-- Facet vs GROUP BY: sub_cat
SELECT sub_cat,count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy')  GROUP BY sub_cat ORDER BY sub_cat COLLATE "C";

-- Facet vs GROUP BY: year
SELECT 1990+((year-1990)/5)*5 AS bucket,count(*) FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy')  AND year >= 1990 AND year < 2030 GROUP BY bucket ORDER BY bucket;

-- Jedno zapytanie SQL z trzema agregacjami
WITH matches AS MATERIALIZED (SELECT main_cat,sub_cat,1990+((year-1990)/5)*5 AS bucket FROM grade5_arxiv WHERE tsv @@ to_tsquery('english', 'galaxy') ) SELECT main_cat,sub_cat,bucket,count(*) FROM matches GROUP BY GROUPING SETS ((main_cat),(sub_cat),(bucket)) ORDER BY main_cat NULLS FIRST,sub_cat NULLS FIRST,bucket NULLS FIRST;