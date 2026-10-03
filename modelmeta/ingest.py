"""Build modelmeta.db from a LiteLLM metadata snapshot.

Determinism is a hard requirement, not a nicety: the .db is committed to git, and a
rebuild that changed nothing must produce an identical file. Every iteration order is
sorted, every JSON serialisation is key-sorted, and the only timestamp in the database
is the caller-supplied ``observed_at`` (never the wall clock), so two builds with the
same inputs are byte-identical.
"""

import hashlib
import json
import os
import sqlite3

from . import DOC_ENTRIES, GENERATOR, SCHEMA_VERSION, SOURCES, SOURCE_ID
from . import normalize as N

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)

LITELLM = SOURCE_ID["litellm"]

FACT_COLUMNS = (
    "model_id, source_id, field, raw_field, value_type, v_bool, v_num, v_text, v_json, "
    "is_derived, observed_at"
)


def _dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _value_hash(pairs):
    return hashlib.sha256(_dumps(sorted(pairs)).encode("utf-8")).hexdigest()


def field_value_hashes(records):
    """Per-field hash of every (model_key, value) pair.

    Used for freshness carry-forward: an unchanged hash means the field did not change
    between snapshots, so first_seen/last_changed survive a rebuild.
    """
    by_field = {}
    for key in sorted(records):
        rec = records[key]
        if not isinstance(rec, dict):
            continue
        for field in sorted(rec):
            by_field.setdefault(field, []).append((key, rec[field]))
    return {f: _value_hash(pairs) for f, pairs in by_field.items()}


def _read_prior_freshness(path):
    """Read freshness rows from an existing DB before it is replaced."""
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT source_id, field, value_hash, first_seen, last_seen, last_changed "
            "FROM source_field_freshness"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in rows}


def _normalize_key(key, provider=None):
    """Spelling-normalised model name, for grouping spelling variants.

    The leading segment is stripped ONLY when it genuinely is the provider. Stripping it
    unconditionally collides distinct models: for
    '512-x-512/50-steps/stability.stable-diffusion-xl-v0' the first segment is a
    resolution, so it would merge with 'max-x-max/50-steps/...'.

    'deepseek/deepseek-chat' and 'deepseek-chat' both normalise to 'deepseek-chat'.
    """
    parts = key.split("/")
    if provider and len(parts) > 1 and parts[0] == provider:
        parts = parts[1:]
    joined = "-".join(parts)
    for ch in (".", "_", "/"):
        joined = joined.replace(ch, "-")
    return "-".join(p for p in joined.lower().split("-") if p)


def _strip_provider_prefix(key, provider):
    if provider and key.startswith(provider + "/"):
        return key[len(provider) + 1 :]
    parts = key.split("/", 1)
    return parts[1] if len(parts) == 2 else key


def _decompose_schedule(model_id, source_id, field, value, issues):
    """Split a tiered price schedule into banded `price` rows.

    tiered_pricing is a list of {<cost_field>: <number>, "range": [lo, hi]} objects --
    explicitly banded, so it maps onto `price` perfectly. Decomposing it means
    price_at() works for models that have *only* a schedule (dashscope/qwen-flash has no
    flat input_cost_per_token at all). The raw JSON still goes to price_schedule, which
    remains the authority for the exact band edges.
    """
    rows = []
    if not isinstance(value, list):
        return rows
    for band in value:
        if not isinstance(band, dict):
            continue
        rng = band.get("range")
        lo = hi = None
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo, hi = rng
        for cost_field in sorted(band):
            if cost_field == "range" or not N.is_price_named(cost_field):
                continue
            price = band[cost_field]
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            spec = N.parse_price_name(cost_field)
            if spec is None:
                issues.append(
                    ("unclassified", None, cost_field, f"price field inside {field} band not classifiable")
                )
                continue
            rows.append(
                (
                    model_id,
                    source_id,
                    cost_field,
                    field,  # origin_field: the schedule this band came from
                    spec["direction"],
                    spec["unit"],
                    spec["modality"],
                    spec["tier"],
                    spec["cache_ttl"],
                    int(lo) if lo is not None else None,
                    int(hi) if hi is not None else None,
                    float(price),
                    spec["qualifier"],
                )
            )
    return rows


def build(records, out_path, observed_at, source_sha256, origin="", include_raw=True):
    """Create the database. Returns (stats, issues)."""
    issues = []
    prior_freshness = _read_prior_freshness(out_path)
    hashes = field_value_hashes(records)

    if os.path.exists(out_path):
        os.remove(out_path)
    for sidecar in ("-journal", "-wal", "-shm"):
        if os.path.exists(out_path + sidecar):
            os.remove(out_path + sidecar)

    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    conn = sqlite3.connect(out_path)
    conn.executescript(schema_sql)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    cur = conn.cursor()

    for sid, name, priority, kind in SOURCES:
        cur.execute(
            "INSERT INTO source (id, name, priority, kind) VALUES (?,?,?,?)",
            (sid, name, priority, kind),
        )

    price_fields_seen = set()
    schedule_fields_seen = set()
    aliases = {}
    by_norm = {}

    # No explicit BEGIN: sqlite3's default isolation_level already opens a transaction
    # before the first INSERT, and executescript() above has committed the DDL.
    model_id = 0
    for key in sorted(records):
        rec = records[key]
        if not isinstance(rec, dict):
            issues.append(("drop", key, None, f"record is {type(rec).__name__}, expected object"))
            continue

        model_id += 1
        is_doc = 1 if key in DOC_ENTRIES else 0
        mode_raw = rec.get("mode")
        mode, mode_issue = N.coerce_mode(mode_raw)
        if mode_issue:
            issues.append(("coerce", key, "mode", mode_issue))
        provider = N.provider_of(rec)
        dep = rec.get("deprecation_date")
        if not isinstance(dep, str):
            dep = None

        # Doc entries carry literal placeholder strings everywhere, so only the key,
        # provider and mode_raw are meaningful.
        if is_doc:
            dep = None

        cur.execute(
            "INSERT INTO model (id, key, family_id, mode, mode_raw, provider, is_doc_entry, "
            "deprecation_date) VALUES (?,?,?,?,?,?,?,?)",
            (
                model_id,
                key,
                None,
                mode,
                mode_raw if isinstance(mode_raw, str) else None,
                provider,
                is_doc,
                dep,
            ),
        )

        cur.execute(
            "INSERT INTO provider_offer (model_id, provider, provider_model_id) VALUES (?,?,?)",
            (model_id, provider or "unknown", _strip_provider_prefix(key, provider)),
        )

        if include_raw:
            cur.execute(
                "INSERT INTO raw_record (model_id, v_json, sha256) VALUES (?,?,?)",
                (
                    model_id,
                    _dumps(rec),
                    hashlib.sha256(_dumps(rec).encode("utf-8")).hexdigest(),
                ),
            )

        # ---- facts / prices -------------------------------------------------------
        for field in sorted(rec):
            value = rec[field]
            route = N.route_field(field, value)

            if route == N.ROUTE_SCHEDULE:
                schedule_fields_seen.add(field)
                cur.execute(
                    "INSERT INTO price_schedule (model_id, source_id, raw_field, v_json) "
                    "VALUES (?,?,?,?)",
                    (model_id, LITELLM, field, _dumps(value)),
                )
                if field == "tiered_pricing":
                    # Decomposed so price_at() works for schedules too; the raw JSON
                    # above remains the authority for exact band edges.
                    for row in _decompose_schedule(model_id, LITELLM, field, value, issues):
                        cur.execute(
                            "INSERT INTO price (model_id, source_id, raw_field, origin_field, "
                            "direction, unit, modality, tier, cache_ttl, band_start, band_end, "
                            "price, qualifier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            row,
                        )
                        price_fields_seen.add(row[2])
                continue

            if route == N.ROUTE_EFFORT:
                continue  # owned by reasoning_effort, written below

            if route == N.ROUTE_PRICE:
                spec = N.parse_price_name(field)
                # Band semantics: a flat per-token price is the base band (0); the
                # '_above_200k_tokens' family are surcharge bands sharing that slot.
                band = spec["band_start"]
                if band is None and spec["unit"] == "per_token":
                    band = 0
                cur.execute(
                    "INSERT INTO price (model_id, source_id, raw_field, origin_field, "
                    "direction, unit, modality, tier, cache_ttl, band_start, band_end, "
                    "price, qualifier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        model_id, LITELLM, field, None, spec["direction"], spec["unit"],
                        spec["modality"], spec["tier"], spec["cache_ttl"], band, None,
                        float(value), spec["qualifier"],
                    ),
                )
                price_fields_seen.add(field)
                continue

            if N.is_price_named(field):
                # ROUTE_FACT for a price-named field means the grammar gave up. Keep the
                # number in fact so it stays queryable, and record why. Never dropped.
                issues.append(("unclassified", key, field, "price-named field not classifiable"))

            value_type, v_bool, v_num, v_text, v_json = N.classify(value)
            cur.execute(
                f"INSERT INTO fact ({FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    model_id, LITELLM, N.normalize_field_name(field), field, value_type,
                    v_bool, v_num, v_text, v_json, 0, observed_at,
                ),
            )

        # ---- derived canonical facts ---------------------------------------------
        derived, limit_issues = N.normalize_limits(rec, mode)
        for issue in limit_issues:
            issues.append(("collision", key, issue[1], issue[2]))
        for dfield, draw, dtype, db_, dn_, dt_, dj_ in derived:
            cur.execute(
                f"INSERT INTO fact ({FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (model_id, LITELLM, dfield, draw, dtype, db_, dn_, dt_, dj_, 1, observed_at),
            )

        for level, raw_field, encoding, is_default in N.normalize_efforts(rec):
            cur.execute(
                "INSERT OR IGNORE INTO reasoning_effort "
                "(model_id, level, source_id, raw_field, encoding, is_default) VALUES (?,?,?,?,?,?)",
                (model_id, level, LITELLM, raw_field, encoding, is_default),
            )

        by_norm.setdefault((provider, _normalize_key(key, provider)), []).append(key)

    # ---- spelling aliases ---------------------------------------------------------
    # A spelling alias says "these two names are the same model". Grouping is by
    # (provider, normalised name), never by name alone: 'azure/X' and 'azure_ai/X' are
    # different offers from different providers, not two spellings of one model, and
    # recording them as aliases would return one provider's pricing for the other.
    # Pricing must also be byte-identical. A wrong alias returns a wrong answer, so
    # every condition here is deliberately conservative.
    alias_count = 0
    for norm_key in sorted(by_norm, key=lambda p: (p[0] or "", p[1])):
        _provider, norm = norm_key
        group = by_norm[norm_key]
        if len(group) < 2:
            continue
        priced = {
            key: _dumps(
                {
                    f: records[key][f]
                    for f in sorted(records[key])
                    if N.is_price_named(f) or f in N.PRICE_SCHEDULE_FIELDS
                }
            )
            for key in group
        }
        # Canonical is the most-qualified spelling; ties broken alphabetically so the
        # choice is deterministic.
        canonical = sorted(group, key=lambda k: (-len(k), k))[0]
        canon_price = priced[canonical]
        same_priced = [k for k in group if priced[k] == canon_price]

        for key in sorted(same_priced):
            if key != canonical:
                cur.execute(
                    "INSERT OR IGNORE INTO model_alias (alias, canonical_model_id) "
                    "SELECT ?, id FROM model WHERE key = ?",
                    (key, canonical),
                )
                alias_count += cur.rowcount

        # The bare name is the useful alias ('qwen3-max' -> 'qwen/qwen3-max'), but only
        # when at least two spellings agree and it does not shadow a real model key.
        if len(same_priced) >= 2 and norm not in records:
            cur.execute(
                "INSERT OR IGNORE INTO model_alias (alias, canonical_model_id) "
                "SELECT ?, id FROM model WHERE key = ?",
                (norm, canonical),
            )
            alias_count += cur.rowcount
            if cur.rowcount:
                aliases[norm] = canonical

    # ---- freshness ----------------------------------------------------------------
    for field in sorted(hashes):
        h = hashes[field]
        prior = prior_freshness.get((LITELLM, field))
        if prior and prior[0] == h:
            value_hash, first_seen, _last_seen, last_changed = prior
        else:
            value_hash, first_seen, last_changed = h, observed_at, observed_at
        cur.execute(
            "INSERT INTO source_field_freshness "
            "(source_id, field, value_hash, first_seen, last_seen, last_changed) VALUES (?,?,?,?,?,?)",
            (LITELLM, field, value_hash, first_seen, observed_at, last_changed),
        )

    # ---- ledger -------------------------------------------------------------------
    for severity, model_key, raw_field, detail in issues:
        cur.execute(
            "INSERT INTO ingest_issue (severity, model_key, raw_field, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (severity, model_key, raw_field, detail, observed_at),
        )

    counts = {
        "input_entry_count": len(records),
        "model_count": cur.execute("SELECT COUNT(*) FROM model").fetchone()[0],
        "doc_entry_count": cur.execute(
            "SELECT COUNT(*) FROM model WHERE is_doc_entry = 1"
        ).fetchone()[0],
    }
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "generator": GENERATOR,
        "built_at": observed_at,
        "source_origin": origin,
        "source_sha256": source_sha256,
        "include_raw": "1" if include_raw else "0",
        **{k: str(v) for k, v in counts.items()},
        "issue_count": str(len(issues)),
    }
    for k in sorted(meta):
        cur.execute("INSERT INTO meta (key, value) VALUES (?,?)", (k, meta[k]))

    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("VACUUM")
    conn.execute("PRAGMA optimize")
    conn.commit()
    conn.close()

    stats = {
        **counts,
        "issue_count": len(issues),
        "alias_count": alias_count,
        "price_fields": sorted(price_fields_seen),
        "schedule_fields": sorted(schedule_fields_seen),
        "source_sha256": source_sha256,
    }
    return stats, issues
