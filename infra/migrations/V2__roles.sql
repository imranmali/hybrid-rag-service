do $$
begin
  if not exists (select from pg_roles where rolname = 'rag_indexer') then
    create role rag_indexer login password '${indexer_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_reader') then
    create role rag_reader login password '${reader_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_router') then
    create role rag_router login password '${router_password}';
  end if;
  execute format('grant connect, temporary on database %I to rag_indexer', current_database());
  execute format('grant connect on database %I to rag_reader, rag_router', current_database());
end $$;

grant usage on schema public to rag_indexer, rag_reader, rag_router;

-- indexer: DML on everything, sequences, TEMP (used by index benchmarks), MAINTAIN (ANALYZE)
grant select, insert, update, delete
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;
grant usage, select on all sequences in schema public to rag_indexer;
grant maintain
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;  -- PG17: lets the indexer ANALYZE

-- service retrieval path: read-only on everything
grant select on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_reader;
alter role rag_reader set default_transaction_read_only = on;
alter role rag_reader set statement_timeout = '5s';

-- service generated-SQL path: structured tables only, no chunk text/embeddings, no raw_text
grant select on authors, paper_authors, paper_categories to rag_router;
grant select (id, external_id, arxiv_version, title, abstract, primary_category, published_on, updated_on, source_url)
  on documents to rag_router;
alter role rag_router set default_transaction_read_only = on;
alter role rag_router set statement_timeout = '2s';
alter role rag_router set idle_in_transaction_session_timeout = '5s';
