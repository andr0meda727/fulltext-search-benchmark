CREATE INDEX {table}_tsv_gin ON {table} USING gin (tsv);
ANALYZE {table};
