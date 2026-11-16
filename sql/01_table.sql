-- Table for the arXiv sample. load_pg.py replaces {table} with arxiv, arxiv_p10 or arxiv_p50.
DROP TABLE IF EXISTS {table};
CREATE TABLE {table} (
    id          text PRIMARY KEY,
    title       text NOT NULL,
    abstract    text NOT NULL,
    categories  text NOT NULL,   -- all categories, space separated
    main_cat    text NOT NULL,   -- archive, e.g. cs, math, astro-ph
    sub_cat     text NOT NULL,   -- primary category, e.g. cs.LG
    license     text NOT NULL,
    year        int  NOT NULL,
    pub_date    date NOT NULL,
    n_authors   int  NOT NULL,
    part        int  NOT NULL    -- 0..99, used for the 10% / 50% / 100% subsets
);
