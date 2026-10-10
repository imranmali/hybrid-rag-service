create extension if not exists vector;
create extension if not exists pg_trgm;

-- One row per arXiv paper (the version that was ingested).
create table documents (
  id                   uuid primary key,          -- uuid5(NAMESPACE_URL, 'arxiv:' || external_id), computed by the indexer
  external_id          text not null unique,      -- arXiv id WITHOUT version, e.g. '2403.01234'
  arxiv_version        int  not null,             -- ingested version number, e.g. 2
  title                text not null,
  abstract             text not null,
  primary_category     text not null,             -- e.g. 'cs.IR'
  published_on         date not null,             -- arXiv <published> (v1 submission date)
  updated_on           date not null,             -- arXiv <updated>
  source_url           text not null,             -- 'https://arxiv.org/html/' || external_id || 'v' || arxiv_version
  raw_text             text not null,             -- normalized full text, sections joined, references removed
  context_summary      text not null,             -- LLM doc-level situating sentence (<= 50 words)
  content_hash         text not null,             -- sha256 hex, see indexer design
  pipeline_fingerprint text not null,             -- sha256 hex of indexing config, see indexer design
  ingested_at          timestamptz not null default now()
);
create index documents_category_date on documents (primary_category, published_on desc);
create index documents_published_on  on documents (published_on);

create table authors (
  id              bigserial primary key,
  name            text not null,                  -- display form as first seen on arXiv
  name_normalized text not null unique            -- see contract C4 (author-name normalization)
);
create index authors_name_trgm on authors using gin (name_normalized gin_trgm_ops);

create table paper_authors (
  document_id uuid   not null references documents(id) on delete cascade,
  author_id   bigint not null references authors(id),
  position    int    not null,                    -- 1-based author order
  primary key (document_id, position),
  unique (document_id, author_id)
);
create index paper_authors_author on paper_authors (author_id);

create table paper_categories (
  document_id uuid    not null references documents(id) on delete cascade,
  category    text    not null,                   -- e.g. 'cs.IR', 'cs.LG'
  is_primary  boolean not null,
  primary key (document_id, category)
);
create index paper_categories_category on paper_categories (category);

create table chunks (
  id             bigserial primary key,
  document_id    uuid not null references documents(id) on delete cascade,
  ordinal        int  not null,                   -- 0-based within the document; stable for identical text + chunker config
  section_path   text not null,                   -- e.g. '3 Method > 3.2 Index construction'
  content        text not null,                   -- chunk body (no header)
  context_header text not null,                   -- 'Paper: <title> (<primary_category>, <year>). Section: <section_path>. <context_summary>'
  token_count    int  not null,                   -- bge-m3 tokenizer count of content
  content_sha256 text not null,                   -- sha256 hex of content (UTF-8)
  embedding      vector(1024) not null,           -- bge-m3(content)
  embedding_ctx  vector(1024) not null,           -- bge-m3(context_header || E'\n\n' || content)
  tsv            tsvector generated always as (to_tsvector('english', content)) stored,
  tsv_ctx        tsvector generated always as (to_tsvector('english', context_header || ' ' || content)) stored,
  unique (document_id, ordinal)
);
create index chunks_embedding_hnsw     on chunks using hnsw (embedding     vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_embedding_ctx_hnsw on chunks using hnsw (embedding_ctx vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_tsv_gin            on chunks using gin (tsv);
create index chunks_tsv_ctx_gin        on chunks using gin (tsv_ctx);
create index chunks_document           on chunks (document_id);

-- Written by the indexer at the end of every ingest; read by the service (/v1/info) and evals.
create table index_metadata (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);
