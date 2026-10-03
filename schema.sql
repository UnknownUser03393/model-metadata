-- modelmeta schema v2 -- a faithful mirror of the curated model metadata snapshot.
--
-- Design notes that are not obvious from the DDL:
--
--  * This is a MIRROR, not a re-derivation. The snapshot arrives already normalised and
--    carries its own contract (`normalization_rules`), per-capability evidence, recorded
--    conflicts and per-offering availability. The loader's only permitted value transform
--    is the identity: true/false/null -> 1/0/NULL. There is no inference anywhere.
--
--  * The unit of truth is an OFFERING, not a model. The same model served by two
--    providers has different limits and prices, and the snapshot records that rather than
--    resolving it. `model_capability` aggregates only to answer "does this model support
--    X at all", and says NULL when no offering declares it.
--
--  * capability.value is genuinely tri-state. NULL means "not established", which is not
--    false. `capability_def.false_is_meaningful` records whether upstream can even say
--    false for that capability -- for the parameter-inferred ones it structurally cannot,
--    so a 0 there would be a bug, not a fact.
--
--  * `offering.provider_model_id` is NULL for 12 official offerings that are documentation
--    stubs (they carry only availability + notes). So the natural key is anchored on the
--    owning model: (model_id, authority, provider, source_id). Provider+model_id is NOT
--    unique among the stubs.
--
--  * Prices are per MILLION tokens. time_band is NULL for flat pricing and peak/off_peak
--    for time-of-day tiers -- there are no token-count bands in this dataset, so the v1
--    price_at() band resolution was deleted rather than carried over.
--
--  * `source.live_http_status` NULL means "never probed", which is not the same as
--    "failed". Only 1 of 12 sources was ever HTTP-checked.

PRAGMA user_version = 2;

-- ------------------------------------------------------------------ document metadata

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- List-valued prose from `scope`, kept in document order where order is meaningful.
CREATE TABLE scope_item (
  kind    TEXT NOT NULL,          -- 'mainstream_author_namespace' | 'coverage_gap'
  ordinal INTEGER NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (kind, ordinal)
);

-- The snapshot's own data contract, quoted verbatim so a consumer can read the rules the
-- data was built under without leaving the database.
CREATE TABLE normalization_rule (name TEXT PRIMARY KEY, text TEXT NOT NULL);

CREATE TABLE statistic (name TEXT PRIMARY KEY, value REAL NOT NULL);

CREATE TABLE source (
  id                TEXT PRIMARY KEY,
  url               TEXT,
  kind              TEXT NOT NULL,
  retrieved_at      TEXT,
  live_http_status  INTEGER     -- NULL = never probed (distinct from "failed")
);

CREATE TABLE provider_namespace_count (namespace TEXT PRIMARY KEY, n INTEGER NOT NULL);

CREATE TABLE endpoint_discovery_check (
  id             INTEGER PRIMARY KEY,
  model_id       TEXT,
  url            TEXT,
  status         TEXT,
  response       TEXT,
  checked_at     TEXT,
  endpoint_count INTEGER
);

-- --------------------------------------------------------------- capability taxonomy

-- Makes "false" vs "unknown" self-documenting in SQL. Mirrors modelmeta/schema.py;
-- a test asserts the two agree.
CREATE TABLE capability_def (
  name                 TEXT PRIMARY KEY,
  domain               TEXT NOT NULL CHECK (domain IN ('modality','parameter','operational')),
  evidence_aspect      TEXT NOT NULL,
  false_is_meaningful  INTEGER NOT NULL CHECK (false_is_meaningful IN (0,1))
);

-- -------------------------------------------------------------------------- models

CREATE TABLE model (
  id                INTEGER PRIMARY KEY,
  catalog_id        TEXT NOT NULL UNIQUE,
  canonical_slug    TEXT,          -- source-declared; 4 records lack it. Not a dedup key.
  name              TEXT NOT NULL,
  author            TEXT NOT NULL,
  mainstream_author INTEGER,
  catalog_order     INTEGER,
  common_candidate  INTEGER,
  usage_verified    INTEGER,
  hugging_face_id   TEXT,
  release_date      TEXT,          -- NULL for 100% of the current snapshot
  catalog_created_at TEXT,
  knowledge_cutoff  TEXT,
  open_weights      INTEGER,       -- NULL for 100%
  license           TEXT,          -- NULL for 100%
  parameter_count   INTEGER        -- NULL for 100%
);

-- ------------------------------------------------------------------------ offerings

CREATE TABLE offering (
  id                      INTEGER PRIMARY KEY,
  model_id                INTEGER NOT NULL REFERENCES model(id),
  authority               TEXT NOT NULL CHECK (authority IN ('catalog','official')),
  provider                TEXT NOT NULL,
  provider_model_id       TEXT,     -- NULL for the 12 official documentation stubs
  source_id               TEXT NOT NULL REFERENCES source(id),
  availability_status     TEXT,     -- see modelmeta.schema.AVAILABILITY_STATUSES
  inference_tested        INTEGER,  -- 0 for 100% of rows; see the `unverified` view
  account_access_verified INTEGER,  -- NULL for 12
  expiration_date         TEXT,
  endpoint_count_observed INTEGER,
  context_tokens          INTEGER,  -- 0 appears 73x; stored as-is, never silently nulled
  max_output_tokens       INTEGER,  -- 0 appears 73x
  max_input_tokens        INTEGER,  -- NULL for 100% of rows
  tokenizer               TEXT,
  notes                   TEXT,
  knowledge_cutoff        TEXT,
  UNIQUE (model_id, authority, provider, source_id)
);

CREATE TABLE offering_source (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  source_id   TEXT NOT NULL REFERENCES source(id),
  PRIMARY KEY (offering_id, source_id)
);

CREATE TABLE api_protocol (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  protocol    TEXT NOT NULL,
  PRIMARY KEY (offering_id, protocol)
);

-- ------------------------------------------------------- capabilities (tri-state)

CREATE TABLE capability (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  name        TEXT NOT NULL REFERENCES capability_def(name),
  value       INTEGER,   -- 1 = yes, 0 = no, NULL = not established. Never coerce NULL to 0.
  PRIMARY KEY (offering_id, name)
);

-- How each capability group was determined, verbatim from the snapshot.
CREATE TABLE capability_evidence (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  aspect      TEXT NOT NULL,
  text        TEXT NOT NULL,
  PRIMARY KEY (offering_id, aspect)
);

-- Modality sets. Stored per item because the snapshot's list order is inconsistent
-- (['text','image','video'] and ['image','text','video'] both occur).
CREATE TABLE modality (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  direction   TEXT NOT NULL CHECK (direction IN ('input','output')),
  modality    TEXT NOT NULL,
  PRIMARY KEY (offering_id, direction, modality)
);

CREATE TABLE supported_parameter (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  parameter   TEXT NOT NULL,
  PRIMARY KEY (offering_id, parameter)
);

CREATE TABLE default_parameter (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  name        TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  PRIMARY KEY (offering_id, name)
);

CREATE TABLE supported_voice (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  voice       TEXT NOT NULL,
  PRIMARY KEY (offering_id, voice)
);

-- ---------------------------------------------------- reasoning (order-insensitive)

CREATE TABLE reasoning_profile (
  offering_id     INTEGER PRIMARY KEY REFERENCES offering(id),
  mandatory       INTEGER,
  default_enabled INTEGER,
  default_effort  TEXT
);

-- One row per supported level. The snapshot has 31 distinct list representations but
-- only 27 distinct sets -- the difference is pure ordering, which is discarded here.
CREATE TABLE reasoning_effort (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  effort      TEXT NOT NULL,
  PRIMARY KEY (offering_id, effort)
);

-- -------------------------------------------------------------------------- pricing

CREATE TABLE price (
  offering_id        INTEGER NOT NULL REFERENCES offering(id),
  kind               TEXT NOT NULL CHECK (kind IN ('input','output','cache_read','cache_write')),
  per_million_tokens REAL,          -- may be NULL (the snapshot writes null prices)
  currency           TEXT NOT NULL,
  token_unit         INTEGER NOT NULL,
  time_band          TEXT,          -- NULL = flat; 'peak'/'off_peak' for time-of-day tiers
  -- Only the tiered official offerings declare a per-price source; NULL elsewhere.
  source_id          TEXT REFERENCES source(id),
  PRIMARY KEY (offering_id, kind, time_band)
);

-- Original string values exactly as published, uninterpreted (e.g. {'prompt':'0.0000003'}).
CREATE TABLE price_raw (
  offering_id INTEGER NOT NULL REFERENCES offering(id),
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY (offering_id, key)
);

CREATE TABLE price_note (
  offering_id INTEGER PRIMARY KEY REFERENCES offering(id),
  note        TEXT NOT NULL
);

-- ------------------------------------------------------------------------ conflicts

-- Where the official source and the aggregator disagree. Retained rather than resolved:
-- the snapshot's own rule is "retain both; values belong to different serving offerings".
CREATE TABLE conflict (
  id                  INTEGER PRIMARY KEY,
  model_id            INTEGER NOT NULL REFERENCES model(id),
  field               TEXT NOT NULL,
  official_value_json TEXT,
  aggregator_value_json TEXT,
  resolution          TEXT NOT NULL,
  source_id           TEXT
);

-- ------------------------------------------------------------------------ ledger

-- Everything the loader could not place, could not classify, or saw change semantics.
-- Recognising a key is not the same as understanding it, so this is where the difference
-- is recorded. A non-empty ledger is not a failure; a *silently* non-empty one would be.
CREATE TABLE ingest_issue (
  id         INTEGER PRIMARY KEY,
  severity   TEXT NOT NULL,
  scope      TEXT,
  detail     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------- views

-- "Does this model support X at all?" aggregated over its offerings.
-- 1  = at least one offering says yes
-- 0  = no offering says yes AND at least one offering could have said no
-- NULL = nobody established it
CREATE VIEW model_capability AS
SELECT m.id AS model_id, m.catalog_id, d.name AS capability,
       CASE
         WHEN SUM(CASE WHEN c.value = 1 THEN 1 ELSE 0 END) > 0 THEN 1
         WHEN d.false_is_meaningful = 1
              AND COUNT(c.value) > 0
              AND SUM(CASE WHEN c.value = 0 THEN 1 ELSE 0 END) = COUNT(c.value) THEN 0
         ELSE NULL
       END AS value
FROM model m
CROSS JOIN capability_def d
LEFT JOIN offering o ON o.model_id = m.id
LEFT JOIN capability c ON c.offering_id = o.id AND c.name = d.name
GROUP BY m.id, d.name;

-- Flat (non-tiered) prices, for price filtering and ordering.
CREATE VIEW offering_price_flat AS
SELECT offering_id, kind, per_million_tokens, currency, token_unit
FROM price WHERE time_band IS NULL;

CREATE VIEW expiring_offering AS
SELECT o.id AS offering_id, m.catalog_id, o.authority, o.provider,
       o.availability_status, o.expiration_date
FROM offering o JOIN model m ON m.id = o.model_id
WHERE o.expiration_date IS NOT NULL;

-- Every row here is unverified by construction. Named so that a consumer cannot
-- reasonably assume otherwise.
CREATE VIEW unverified AS
SELECT o.id AS offering_id, m.catalog_id, o.authority, o.provider,
       o.availability_status, o.inference_tested, o.account_access_verified
FROM offering o JOIN model m ON m.id = o.model_id
WHERE o.inference_tested = 0;

-- --------------------------------------------------------------------------- indexes

CREATE INDEX idx_offering_model     ON offering (model_id);
CREATE INDEX idx_offering_provider  ON offering (provider);
CREATE INDEX idx_offering_authority ON offering (authority);
CREATE INDEX idx_capability_lookup  ON capability (name, value);
CREATE INDEX idx_modality_lookup    ON modality (direction, modality);
CREATE INDEX idx_price_lookup       ON price (kind, per_million_tokens);
CREATE INDEX idx_model_author       ON model (author);
CREATE INDEX idx_offering_expiry    ON offering (expiration_date)
  WHERE expiration_date IS NOT NULL;
