# Model Metadata

A codebase for harness/app to fetch the model metadatas.

Ships as a single SQLite file, `modelmeta.db`, mirrored from a curated snapshot
(`snapshot/model_metainf_2026-10-03.json`). Clone the repo and query it — no build step,
no JSON parsing, no runtime dependency.

The database *is* the interface. Nothing in it is Python-specific, so a TypeScript, Go or
Rust consumer reads it with that language's SQLite driver. Python is only used to build it.

## Read this first: nothing here has been verified against a running model

Every one of the 696 offerings has `inference_tested = 0`, and the snapshot's own
`statistics.inference_calls_performed` is `0`. **No inference call was ever made.** Every
statement in this database — every capability, every limit, every price — means "a catalog
or a provider's documentation states this", never "we measured this".

A `vision = true` row means a catalogue listed `vision` among the modalities. It does not
mean anyone sent an image to that model. The `unverified` view exists so this cannot be
lost, and `modelmeta sources` shows which sources were even reachable: only 1 of 12 was
ever HTTP-probed (`live_http_status` is NULL for the rest, which means "not checked", not
"failed").

## Why not just read the JSON

Reading the snapshot directly is workable until you need a non-obvious answer:

- **The unit of truth is an offering, not a model.** The same model served by two
  providers has different limits and prices, and the snapshot retains both. `get` shows
  them side by side; `conflicts` records where they disagree.
- **Two structurally different kinds of tri-state exist in the same object.** Six
  capabilities are declared modalities and are only ever `true`/`false`. Six others are
  inferred from the presence of a supported parameter and are only ever `true`/`null` —
  `false` never occurs. Two more (`parallel_tool_calls`, `streaming`) are declared and
  never established. Treating `null` as `false` is wrong about a third of the catalogue.
- **Some values are deliberately weird and must stay that way.** 73 offerings report
  `context_tokens = 0`. Five columns are 100% NULL. The snapshot's own rules say not to
  guess; this database does not repair any of it.

## Quick start

```bash
git clone https://github.com/UnknownUser03393/model-metadata
cd model-metadata

sqlite3 modelmeta.db "SELECT catalog_id, authority, provider FROM offering LIMIT 5"
```

```python
import sqlite3
conn = sqlite3.connect("modelmeta.db")
conn.row_factory = sqlite3.Row
```

Contents: 651 models, 696 offerings (647 aggregator + 49 provider-official), 9,093
capability rows, 2,616 prices, 35 offerings with an expiry date, 2 recorded conflicts,
2.3 MB.

## Capabilities are tri-state, and the kind matters

```sql
-- yes
SELECT 1 FROM capability WHERE offering_id = ? AND name = 'vision' AND value = 1;
-- no, and this is a real observation
SELECT 1 FROM capability WHERE offering_id = ? AND name = 'vision' AND value = 0;
-- unknown: no row, or a row whose value is NULL. Not "no".
```

`capability_def` says, per capability, whether `false` is even possible:

| domain | capabilities | `false` means | distribution in this snapshot |
|---|---|---|---|
| `modality` | `vision`, `audio_input`, `video_input`, `image_output`, `audio_output`, `embeddings` | the declared modality list does not include it | `vision` 387 true / 273 false / **0 unknown** |
| `parameter` | `tool_calling`, `structured_outputs`, `json_mode`, `temperature`, `reasoning`, `prompt_cache` | *(cannot occur)* | `tool_calling` 408 true / **249 unknown** / **0 false** |
| `operational` | `parallel_tool_calls`, `streaming` | *(cannot occur)* | declared on 647 offerings, **established on none** |

That distinction is not decoration. It is why these two searches return different things:

```bash
python -m modelmeta search --no-vision    # 273 offerings: false is a declared observation
python -m modelmeta search --no-tools     # 0 offerings:  tool_calling can never be false
```

The second is not a bug. Returning every offering whose `tool_calling` is `null` would be
the bug — it would silently assert that 249 offerings *lack* a capability that simply was
not established.

`model_capability` aggregates over offerings for questions about the model itself, and
still says NULL when no offering established anything.

## CLI

```
python -m modelmeta get    CATALOG_ID [--evidence] [--json]
python -m modelmeta search [--vision] [--no-tools] [--text-only] [--min-context N]
                           [--max-input-price USD_PER_M] [--author] [--provider]
                           [--status] [--expiring-before YYYY-MM-DD] [--json]
python -m modelmeta diff   CATALOG_ID [--json]
python -m modelmeta rules                  # the snapshot's own normalization contract
python -m modelmeta sources                # 12 sources, which were probed
python -m modelmeta verify                 # completeness + consistency
python -m modelmeta update --from-file snapshot/...json
```

`get` shows the model's aggregated capabilities, then each offering in full — availability,
limits, modalities, per-capability tri-state with `(false cannot occur)` annotations,
reasoning efforts, and prices — then any recorded conflicts.

`search` returns **offerings**, because the conditions are offering-level. Capability flags
come in both polarities (`--vision` / `--no-vision`), generated from `capability_def`.
`--max-input-price` compares against the cheapest advertised input rate per million tokens,
off-peak included.

## Prices

Per **million** tokens, USD, from `pricing.token_unit = 1000000`. 657 offerings carry
prices; 39 carry none at all (mostly provider-official entries).

Two offerings price by time of day rather than a flat rate. Those use `time_band`
(`peak` / `off_peak`) and appear as separate rows:

```
[off_peak] input   $0.15
[peak]     input   $0.3
```

The original published strings are kept verbatim in `price_raw` (`{"prompt": "0.0000003"}`),
and `price_note` carries the snapshot's own warning about non-token prices. There are no
token-count bands in this dataset, so unlike the previous iteration there is no
`price_at()` band-resolution function to get wrong.

## Provenance

Resolution is not collapsed: `offering.source_id` names the source each offering came from,
`conflict` retains both sides of every disagreement, and `capability_evidence` holds the
snapshot's own explanation of how each capability group was determined. `normalization_rule`
stores the snapshot's data contract verbatim — six rules, including the two this schema is
built around:

> **null**: Unknown, unreported, or not independently established. Never coerce absent
> supported parameter to false.
>
> **identity**: `catalog_id` is an offering/catalog identity; `canonical_slug` is source
> declared, not global deduplication proof. Do not merge aliases/quantizations/free/batch
> variants by string similarity.

The second is why there is no alias table and no fuzzy model lookup. `get` resolves
`catalog_id`, then `canonical_slug`, then display name — exact matches only.

Availability status is not binary either: `listed` 647, `documented_available` 36,
`existing_users_only` 8, `retired` 4, `plan_restricted` 1.

## Known data quirks, preserved on purpose

| Quirk | Count | What this database does |
|---|---|---|
| `context_tokens = 0` (and `max_output_tokens = 0`) | 73 | stores 0. Does not guess that 0 means unknown. `get` annotates it. |
| `max_input_tokens` | 696 of 696 NULL | the column is unused upstream; kept so it is visibly empty |
| `release_date`, `open_weights`, `license`, `parameter_count` | 100% NULL | the snapshot declines to assert these; so does the mirror |
| near-miss limits (`8191`, `4095`, `16385`, `1048756`) | — | stored as published; the rules forbid guessing K=1000 or 1024 |
| official offerings that are documentation stubs | 12 | kept as offerings with availability + notes and no limits/capabilities/pricing |
| models with no aggregator listing | 4 | they exist via provider documentation only; not dropped |
| `supported_efforts` list order | 29 representations → 26 sets | stored as a set; the reordering carries no information |

## Rebuilding

```bash
python -m modelmeta update --from-file snapshot/model_metainf_2026-10-03.json
python -m modelmeta verify
python -m unittest discover -s tests -t .
```

Zero third-party dependencies — Python 3.8+ stdlib only. The build is deterministic: the
only timestamp is the snapshot's own `snapshot_at`, every iteration is sorted, every JSON
serialisation is key-sorted, and string lists are stored as sets. Rebuilding the same
snapshot yields a byte-identical file, which `verify` asserts by rebuilding and comparing.

Reproducibility is keyed on a **content hash** (a canonical serialisation of the document),
not on the input file's bytes. An earlier iteration hashed raw bytes, so the same data
arriving as UTF-16+CRLF and as UTF-8+LF produced different databases and the committed file
could not be reproduced from the documented path. `tests/test_mirror.py` keeps that as a
regression test.

## Scope and honest limits

- **Unverified, as above.** This is the single most important caveat in this document.
- **This is a curated subset, not a census.** 651 models selected from an aggregator
  catalogue plus targeted official documentation. The snapshot's own `scope` block lists
  the selection rule and its `coverage_gaps`; both are queryable in `scope_item`.
- **`verify` proves the mirror is faithful, not that the metadata is true.** It checks that
  nothing was dropped, that no tri-state was flattened, that no conflict was resolved and
  no odd value repaired, and that rebuilds are identical. It cannot check the claims
  against reality, because nothing here was measured.
- **The snapshot is committed deliberately.** There is no upstream URL to refetch it from —
  it is the output of a curation pipeline — so the JSON in the tree is the source of truth
  and the `.db` is a derived index. Review happens on the JSON; the binary is regenerated.
  The database is ~2.3 MB and `raw_record` is not stored, precisely because the snapshot
  itself is now the lossless artifact.

## Tests

37 unit tests. They cover, among other things: that a wrong-format snapshot is rejected
rather than silently mis-ingested; that all four encodings of the same document parse
identically and build byte-identical databases; that parameter-inferred capabilities never
report false and declared-modality ones never report null; that the two effort orderings of
one model collapse to a single set; and that the CLI's negated parameter filter returns
empty rather than everything.

`tests/fixtures/make_fixture.py` regenerates the fixture and documents why each of its six
models was chosen.
