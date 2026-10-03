-- modelmeta schema (v1)
--
-- Contract shared by ingest.py (writes) and query.py / cli.py (reads).
--
-- Design notes that are not obvious from the DDL:
--
--  * fact.field is the CANONICAL name; fact.raw_field is the name as it appears in
--    the source. For almost every field these are identical. They differ only where a
--    rule derives a canonical fact (e.g. max_input_tokens -> context_window). Raw rows
--    carry is_derived=0 and the derived row carries is_derived=1, so a derivation is
--    always auditable and the raw input is never lost.
--
--  * Price fields live ONLY in price/price_schedule, never duplicated into fact:
--    price.price already *is* the fact. The sole exception is a price-named field the
--    grammar cannot classify -- that lands in fact as value_type='num' plus an
--    ingest_issue row. "unclassified" therefore means "went to fact, not price",
--    never "was dropped".
--
--  * resolved_fact is the only resolution path. There is exactly one source of truth
--    (fact); the view recomputes the winner per (model, field) on read, so a rebuild
--    can never leave a stale "resolved" row behind.
--
--  * mode is deliberately NOT CHECK-constrained. Upstream adds modes regularly
--    (guardrail and vector_store are recent); a CHECK would turn a routine upstream
--    release into a hard ingest failure. The vocabulary is validated in Python instead.

PRAGMA user_version = 1;

-- Build metadata, including the source snapshot hash. Not derived from the clock
-- unless the caller asks for it, so that repeated builds are byte-identical.
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- priority defines resolution order:
--   local_override(100) > provider_official(80) > openrouter(60) > litellm(40)
-- Only 'litellm' currently has a loader; the others exist so the resolver and
-- `diff` are correct the moment a second loader is written.
CREATE TABLE source (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE,
  priority INTEGER NOT NULL,
  kind     TEXT NOT NULL
);

-- family is a SOFT grouping: populated only on high-confidence matches, because the
-- heuristic will misfire (gpt-4o vs gpt-4o-mini share a prefix). A wrong family costs
-- a missing convenience; a wrong alias returns a wrong answer -- hence the asymmetry.
CREATE TABLE family (
  id             INTEGER PRIMARY KEY,
  vendor         TEXT,
  canonical_name TEXT NOT NULL,
  confidence     REAL NOT NULL
);

CREATE TABLE model (
  id               INTEGER PRIMARY KEY,
  key              TEXT NOT NULL UNIQUE,   -- source top-level key, e.g. 'dashscope/qwen-flash'
  family_id        INTEGER REFERENCES family(id),
  mode             TEXT,                   -- validated in Python, not CHECKed here
  mode_raw         TEXT,                   -- the dirty upstream value, kept verbatim
  provider         TEXT,                   -- from litellm_provider
  is_doc_entry     INTEGER NOT NULL DEFAULT 0,  -- sample_spec / fallback_generalizations
  deprecation_date TEXT
);

-- A provider offer is the same model family sold through a different route
-- (openrouter vs dashscope) with its own pricing and limits. Kept distinct from
-- model_alias, which only records spelling variants of one canonical model.
CREATE TABLE provider_offer (
  id                INTEGER PRIMARY KEY,
  model_id          INTEGER NOT NULL REFERENCES model(id),
  provider          TEXT NOT NULL,
  provider_model_id TEXT,
  UNIQUE (model_id, provider)
);

CREATE TABLE model_alias (
  alias              TEXT PRIMARY KEY,
  canonical_model_id INTEGER NOT NULL REFERENCES model(id)
);

-- The lossless spine. Typed columns instead of a single stringly-typed value column so
-- that `supports_vision = 1` is an index seek rather than a string comparison.
CREATE TABLE fact (
  id          INTEGER PRIMARY KEY,
  model_id    INTEGER NOT NULL REFERENCES model(id),
  source_id   INTEGER NOT NULL REFERENCES source(id),
  field       TEXT NOT NULL,        -- canonical name
  raw_field   TEXT NOT NULL,        -- name as it appears in the source
  value_type  TEXT NOT NULL CHECK (value_type IN ('bool','num','text','json')),
  v_bool      INTEGER,
  v_num       REAL,
  v_text      TEXT,
  v_json      TEXT,
  is_derived  INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL,
  UNIQUE (model_id, source_id, field, is_derived)
);

-- reasoning effort is dual-encoded upstream with zero overlap today: 301 models use
-- discrete booleans (supports_xhigh_reasoning_effort, ...), 38 use a list
-- (reasoning_effort_levels). Both encodings are merged here so a consumer asking for
-- the supported efforts gets the union without knowing which encoding was used.
-- raw_field is carried so per-field accounting stays exact.
CREATE TABLE reasoning_effort (
  model_id   INTEGER NOT NULL REFERENCES model(id),
  level      TEXT NOT NULL,
  source_id  INTEGER NOT NULL REFERENCES source(id),
  raw_field  TEXT NOT NULL,
  encoding   TEXT NOT NULL CHECK (encoding IN ('boolean','list')),
  is_default INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (model_id, level, source_id)
);

-- Price dimensions: direction x unit x modality x tier x cache_ttl, with token-range
-- bands. Base price is band_start=0; '_above_200k_tokens' variants are surcharge
-- rows. Deliberately NOT 137 columns.
CREATE TABLE price (
  id        INTEGER PRIMARY KEY,
  model_id  INTEGER NOT NULL REFERENCES model(id),
  source_id INTEGER NOT NULL REFERENCES source(id),
  raw_field TEXT NOT NULL,
  -- NULL when the field was named directly (input_cost_per_token). Set to the schedule
  -- field name (tiered_pricing) when this row was decomposed out of a band list, so
  -- per-field accounting can attribute the row to its true origin.
  origin_field TEXT,
  direction TEXT NOT NULL,   -- input/output/cache_read/cache_creation/search/ocr/annotation/...
  unit      TEXT NOT NULL,   -- per_token/per_second/per_page/per_image/per_pixel/per_query/...
  modality  TEXT,            -- audio/image/video/text
  tier      TEXT,            -- batches/priority/flex/ultrafast/balanced
  cache_ttl TEXT,            -- '1hr'
  band_start INTEGER,
  band_end   INTEGER,
  price     REAL NOT NULL,
  qualifier TEXT             -- residual qualifier after known dimensions are stripped;
                             -- never discarded, so nothing is silently lost
);

-- Price structures that cannot be flattened: tiered_pricing is a list of disjoint
-- token-range bands, off_peak_pricing is a time schedule.
CREATE TABLE price_schedule (
  id        INTEGER PRIMARY KEY,
  model_id  INTEGER NOT NULL REFERENCES model(id),
  source_id INTEGER NOT NULL REFERENCES source(id),
  raw_field TEXT NOT NULL,
  v_json    TEXT NOT NULL
);

-- Verbatim source record per model: the second, independent losslessness proof
-- (the first being the per-field accounting). Droppable with --no-raw.
CREATE TABLE raw_record (
  model_id INTEGER PRIMARY KEY REFERENCES model(id),
  v_json   TEXT NOT NULL,
  sha256   TEXT NOT NULL
);

-- Per-field freshness, so "high priority but 40 days stale" is distinguishable from
-- "low priority but fetched today".
CREATE TABLE source_field_freshness (
  source_id    INTEGER NOT NULL REFERENCES source(id),
  field        TEXT NOT NULL,
  value_hash   TEXT NOT NULL,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  last_changed TEXT NOT NULL,
  PRIMARY KEY (source_id, field)
);

-- Ledger of every drop / coercion / collision / unclassified field.
CREATE TABLE ingest_issue (
  id         INTEGER PRIMARY KEY,
  severity   TEXT NOT NULL CHECK (severity IN ('drop','coerce','unclassified','collision')),
  model_key  TEXT,
  raw_field  TEXT,
  detail     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Winner per (model, field). raw (is_derived=0) beats derived at equal priority, so a
-- genuine upstream max_output_tokens wins over one inferred from max_tokens.
CREATE VIEW resolved_fact AS
SELECT model_id, field, raw_field, source_id, value_type, v_bool, v_num, v_text, v_json,
       is_derived
FROM (
  SELECT f.*, ROW_NUMBER() OVER (
           PARTITION BY f.model_id, f.field
           ORDER BY s.priority DESC, f.is_derived ASC, f.observed_at DESC
         ) AS rn
  FROM fact f JOIN source s ON s.id = f.source_id
) WHERE rn = 1;

CREATE INDEX idx_fact_model_field ON fact (model_id, field);
CREATE INDEX idx_fact_field_bool  ON fact (field, v_bool);
CREATE INDEX idx_fact_field_num   ON fact (field, v_num);
CREATE INDEX idx_fact_raw_field   ON fact (raw_field);
CREATE INDEX idx_price_lookup     ON price (model_id, direction, unit, band_start);
CREATE INDEX idx_price_raw_field  ON price (raw_field);
CREATE INDEX idx_model_mode       ON model (mode);
CREATE INDEX idx_model_provider   ON model (provider);
CREATE INDEX idx_offer_provider   ON provider_offer (provider);
CREATE INDEX idx_issue_severity   ON ingest_issue (severity);
