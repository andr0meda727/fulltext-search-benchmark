-- Full-text column: title (weight A) + abstract (weight B), English stemming and stop words.
ALTER TABLE {table} ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A') ||
        setweight(to_tsvector('english', abstract), 'B')
    ) STORED;
