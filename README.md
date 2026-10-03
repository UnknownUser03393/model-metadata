# Model Metadata

A codebase for harness/app to fetch the model metadatas.

Ships as a single SQLite file, `modelmeta.db`, built from LiteLLM's
[`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).
Clone the repo and query it — no ingest step, no JSON parsing, no runtime dependency on
LiteLLM being up.

The DB *is* the interface. Nothing in it is Python-specific, so a TypeScript, Go or Rust
consumer reads it with that language's SQLite driver. Python is only used to build it.

## Why not just read the JSON

Reading upstream directly is workable until you need a non-obvious answer:

- **62% of the payload is pricing** (137 of 219 distinct fields). Field names are not
  uniform — most are `<x>_cost_per_<unit>`, cache pricing instead uses `<x>_token_cost`,
  and markers stack in either order (`..._token_cost_above_1hr_above_200k_tokens`).
- **The natural names are wrong.** There is no `supports_tools`, no
  `supports_structured_output`. The real fields are `supports_function_calling` +
  `supports_tool_choice`, and `supports_response_schema` + `supports_native_structured_output`.
- **Absent means unknown, not false.** Only 2212 of 4460 entries mention
  `supports_vision`: 1755 true, 457 false, **2248 silent**. Anything that defaults a
  missing capability to `false` is wrong about half the dataset.
- **One fact is encoded two ways.** Reasoning-effort support is either five discrete
  booleans (`supports_xhigh_reasoning_effort`, …) or a list (`reasoning_effort_levels`).
  No entry uses both, so a query for "supports reasoning at level X" misses one group
  unless you merge them.
- **Some models have no flat price at all.** `dashscope/qwen-flash` has no
  `input_cost_per_token`; it is priced only by a `tiered_pricing` band list. A naive
  `SELECT` returns nothing useful for it.

## Quick start

```bash
git clone https://github.com/UnknownUser03393/model-metadata
cd model-metadata

sqlite3 modelmeta.db "SELECT key FROM model WHERE mode='chat' LIMIT 5"
```

```python
import sqlite3
conn = sqlite3.connect("modelmeta.db")
conn.row_factory = sqlite3.Row
```

## The one access path you must use for pricing

Banded pricing cannot be read with a plain `SELECT`. The supported rule is **the band
with the largest `band_start` that is still ≤ your token count**, and it is shipped as
SQL so nobody has to reimplement it:

```sql
SELECT p.price
FROM price p JOIN source s ON s.id = p.source_id
WHERE p.model_id = :model_id
  AND p.direction = 'input'
  AND p.unit = 'per_token'
  AND (p.band_start IS NULL OR p.band_start <= :n_tokens)
  AND p.tier IS NULL AND p.modality IS NULL AND p.cache_ttl IS NULL
  AND (p.qualifier IS NULL OR p.qualifier NOT LIKE '%scaled%')
ORDER BY s.priority DESC, (p.band_start IS NULL), p.band_start DESC
LIMIT 1
```

The Python API exposes it as a scalar function of the same name:

```python
from modelmeta import query
conn = query.connect("modelmeta.db", read_only=True)
conn.execute("SELECT price_at(?, 'input', 300000)", (model_id,)).fetchone()[0]
```

This rule covers both shapes in the data: a flat price is `band_start = 0`, and the
`_above_200k_tokens` family are surcharge bands in the same slot. Tiered schedules are
decomposed into real bands at ingest time, so `price_at` works for those models too
(their raw JSON is still in `price_schedule` as the authority on band edges).

## Capabilities are tri-state

```sql
-- supported
SELECT 1 FROM resolved_fact WHERE model_id = ? AND field = 'supports_vision' AND v_bool = 1;
-- unsupported
SELECT 1 FROM resolved_fact WHERE model_id = ? AND field = 'supports_vision' AND v_bool = 0;
-- unknown: no row. Do not treat this as unsupported.
```

`modelmeta search --vision` compiles to the first form, so models that are silent about
vision are excluded rather than assumed false.

## CLI

```bash
python -m modelmeta get dashscope/qwen-flash          # facts, efforts, banded prices
python -m modelmeta get sora-2 --json                 # resolves through aliases
python -m modelmeta search --vision --tools --mode chat --max-input-price 1e-06
python -m modelmeta diff <model> --field supports_vision
python -m modelmeta sources                           # precedence table
python -m modelmeta verify                            # completeness + consistency
```

`search` flags expand to the real upstream field names — `--tools` is
`supports_function_calling`, not an invented `supports_tools`. Nothing is OR-ed together
on your behalf.

`diff` reads the raw `fact` table rather than the resolved view, so it shows losing
values as well as winning ones.

## Schema

| Table | Contents |
|---|---|
| `model` | one row per source entry (4460), including 2 flagged `is_doc_entry` placeholders |
| `fact` | the lossless spine: typed `v_bool`/`v_num`/`v_text`/`v_json`, `raw_field` preserved |
| `price` | `direction × unit × modality × tier × cache_ttl`, token `band_start`/`band_end` |
| `price_schedule` | price structures that cannot be flattened, as JSON |
| `reasoning_effort` | both encodings merged into one level-per-model table |
| `model_alias` | spelling variants only |
| `provider_offer` | one model family sold through different providers |
| `raw_record` | verbatim source record (round-trip proof; omit with `--no-raw`) |
| `source_field_freshness` | per-field `first_seen` / `last_seen` / `last_changed` |
| `ingest_issue` | every drop / coercion / collision / unclassified field |
| `resolved_fact` | **VIEW** — winner per `(model, field)` by source priority |

Current contents: 49,429 facts, 15,480 prices, 4460 raw records, 36 aliases, 16.4 MB.

Derived facts carry `is_derived = 1`: `context_window` comes from `max_input_tokens` (or
from `max_tokens` for embedding/rerank/ocr modes, where it is the input capacity, not an
output length). Raw inputs are always kept alongside, so every derivation is auditable.

## Provenance and priority

Resolution order is `local_override (100) > provider_official (80) > openrouter (60) >
litellm (40)`. `resolved_fact` recomputes the winner on read from the `fact` table, which
is the only source of truth — there is no second copy to drift out of sync, and losers
are retained so `diff` can report disagreement.

**Only the `litellm` source has a loader today.** The other three rows exist so the
resolver, `diff` and the freshness tables are already correct, but `diff` cannot show
cross-source disagreement until a second loader is written. `modelmeta sources` says so
too.

## Rebuilding

```bash
python -m modelmeta update                      # fetch upstream
python -m modelmeta update --from-file snap.json
python -m modelmeta update --no-raw             # ~13.8 MB instead of 16.4 MB
python -m modelmeta verify
python -m unittest discover -s tests -t .
```

Zero third-party dependencies — Python 3.8+ stdlib only (`sqlite3`, `argparse`,
`urllib`, `json`). The build is deterministic: the only timestamp is the caller-supplied
`observed_at`, all iteration is sorted, and all JSON is key-sorted, so rebuilding the
same input yields a byte-identical file. `verify` asserts this.

## Scope and honest limits

- **`verify` proves completeness, not accuracy.** It checks that nothing was dropped
  (per-field accounting, exact round-trip) and that the DB is self-consistent
  (tri-state, band resolution, aliases, determinism). LiteLLM *is* the upstream truth, so
  there is no higher authority to validate against — if upstream is wrong, this records
  it faithfully as wrong.
- **`modelmeta.db` is a ~16 MB binary in git.** SQLite pages delta-compress reasonably
  between rebuilds, but binaries do not line-diff: no `git blame`, and a PR shows "blob
  replaced". `ingest_issue` is the reviewable text artifact. History grows by roughly the
  file size per rebuild; if it passes ~50 MB, move to a GitHub Release asset or LFS.
- **`ingest_issue` is not decoration.** A price-named field the grammar cannot classify
  still lands in `fact` as a number and is logged as `unclassified` — "went to `fact`
  rather than `price`", never "was dropped". Today's snapshot produces 0 of these.
- The shipped `.db` is `VACUUMed` and in `journal_mode=DELETE`, so it is readable
  standalone (no `-wal` sidecar needed).

## Tests

40 unit tests, including the price-name grammar against every awkward real shape and a
UTF-16LE fixture — the original snapshot in this repo was UTF-16 with a BOM, and a wrong
encoding guess silently mangles non-ASCII model names instead of raising.
