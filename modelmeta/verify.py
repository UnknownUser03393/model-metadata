"""Completeness and self-consistency checks.

What this can and cannot prove: LiteLLM is itself the upstream truth, so there is no
higher authority to check against. These checks prove that nothing was dropped
(accounting, round-trip) and that the database is internally consistent (tri-state,
band resolution, aliasing, determinism). They do NOT prove the values are accurate --
if upstream says something wrong, modelmeta faithfully records it as wrong.

Expectations are recomputed from the source snapshot rather than hardcoded, so the
checks stay valid as upstream evolves. Field routing is taken from
``normalize.route_field`` -- the same function ingest uses -- because two copies of that
decision is exactly what makes a completeness check disagree with its loader.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter

from . import DOC_ENTRIES, SOURCE_ID
from . import normalize as N
from .fetch import read_source
from .ingest import build

LITELLM = SOURCE_ID["litellm"]


def _all_fields(records):
    return {f for r in records.values() if isinstance(r, dict) for f in r}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expectations(records):
    """Per field: expected number of rows in each destination table."""
    exp = {}
    for key, rec in records.items():
        if not isinstance(rec, dict):
            continue
        for field, value in rec.items():
            e = exp.setdefault(
                field, {"fact": 0, "price": 0, "schedule": 0, "effort": 0}
            )
            route = N.route_field(field, value)
            if route == N.ROUTE_FACT:
                e["fact"] += 1
            elif route == N.ROUTE_PRICE:
                e["price"] += 1
            elif route == N.ROUTE_SCHEDULE:
                e["schedule"] += 1
            elif field in N.EFFORT_BOOL_FIELDS:
                if value is True:
                    e["effort"] += 1
            elif field == N.EFFORT_LIST_FIELD:
                if isinstance(value, list):
                    e["effort"] += sum(1 for v in value if isinstance(v, str))
            # default_reasoning_effort is verified via is_default in check 6, because a
            # default that names an unsupported level legitimately produces no row.
    return exp


def verify(db_path, source_path, check_determinism=True):
    records, source_sha, _origin = read_source(path=source_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    results = []

    def check(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    check(
        "snapshot matches the one the DB was built from",
        meta.get("source_sha256") == source_sha,
        f"db={meta.get('source_sha256','?')[:12]} source={source_sha[:12]}",
    )

    # -- 1. entry reconciliation ---------------------------------------------------
    model_count = conn.execute("SELECT COUNT(*) FROM model").fetchone()[0]
    doc_count = conn.execute("SELECT COUNT(*) FROM model WHERE is_doc_entry = 1").fetchone()[0]
    expected_doc = len([k for k in records if k in DOC_ENTRIES])
    check(
        "every source entry produced a model row",
        model_count == len(records) and doc_count == expected_doc,
        f"models={model_count}/{len(records)} doc_entries={doc_count}/{expected_doc}",
    )

    allf = _all_fields(records)
    price_named = {f for f in allf if N.is_price_named(f)}

    # -- 2. value-type census on the lossless spine --------------------------------
    src_types = Counter()
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        for field, value in rec.items():
            if N.route_field(field, value) == N.ROUTE_FACT:
                src_types[N.classify(value)[0]] += 1
    db_types = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT value_type, COUNT(*) FROM fact WHERE is_derived = 0 GROUP BY value_type"
        )
    }
    check(
        "fact value_type counts equal the source census",
        all(src_types[t] == db_types.get(t, 0) for t in src_types),
        " ".join(f"{t}={src_types[t]}/{db_types.get(t,0)}" for t in sorted(src_types)),
    )

    # -- 3. round-trip -------------------------------------------------------------
    if meta.get("include_raw") == "1":
        stored = {
            r["model_id"]: r["v_json"]
            for r in conn.execute("SELECT model_id, v_json FROM raw_record")
        }
        keymap = {r["id"]: r["key"] for r in conn.execute("SELECT id, key FROM model")}
        bad = [
            keymap[mid]
            for mid, blob in stored.items()
            if json.loads(blob) != records.get(keymap[mid])
        ]
        check(
            "raw_record round-trips every source record",
            len(stored) == len(records) and not bad,
            f"stored={len(stored)}/{len(records)} mismatched={len(bad)}"
            + (f" e.g. {bad[:3]}" if bad else ""),
        )
    else:
        check("raw_record round-trip", True, "skipped (built with --no-raw)")

    # -- 4. spot check on a model that exists only as a schedule -------------------
    if "dashscope/qwen-flash" in records:
        src = records["dashscope/qwen-flash"]
        mid = conn.execute(
            "SELECT id FROM model WHERE key='dashscope/qwen-flash'"
        ).fetchone()["id"]
        facts = {
            r["field"]: r
            for r in conn.execute("SELECT * FROM resolved_fact WHERE model_id = ?", (mid,))
        }
        bands = [
            (r["price"], r["band_start"], r["band_end"])
            for r in conn.execute(
                "SELECT price, band_start, band_end FROM price WHERE model_id=? AND "
                "direction='input' AND unit='per_token' ORDER BY band_start",
                (mid,),
            )
        ]
        expect = [(5e-08, 0, 256000), (2.5e-07, 256000, 1000000)]
        ok = (
            facts.get("context_window") is not None
            and facts["context_window"]["v_num"] == src.get("max_input_tokens")
            and facts.get("max_output_tokens") is not None
            and facts["max_output_tokens"]["v_num"] == src.get("max_output_tokens")
            and len(bands) == 2
            and all(
                abs(b[0] - e[0]) < 1e-15 and b[1] == e[1] and b[2] == e[2]
                for b, e in zip(bands, expect)
            )
            and "input_cost_per_token" not in src
        )
        cw = facts["context_window"]["v_num"] if facts.get("context_window") else None
        check(
            "dashscope/qwen-flash resolves limits and banded prices",
            ok,
            f"context_window={cw} bands={bands}",
        )
    else:
        check("dashscope/qwen-flash spot check", True, "model absent from this snapshot")

    # -- 5. tri-state capability semantics ----------------------------------------
    v_true = v_false = 0
    for rec in records.values():
        if not isinstance(rec, dict) or "supports_vision" not in rec:
            continue
        if rec["supports_vision"] is True:
            v_true += 1
        elif rec["supports_vision"] is False:
            v_false += 1
    src_unknown = len(records) - v_true - v_false

    db_true = conn.execute(
        "SELECT COUNT(*) FROM resolved_fact WHERE field='supports_vision' AND v_bool = 1"
    ).fetchone()[0]
    db_false = conn.execute(
        "SELECT COUNT(*) FROM resolved_fact WHERE field='supports_vision' AND v_bool = 0"
    ).fetchone()[0]
    db_unknown = model_count - db_true - db_false

    # What `search --vision` returns must be exactly the models that say true -- neither
    # the explicit false ones nor the ones that are silent.
    search_returns = conn.execute(
        "SELECT COUNT(*) FROM model m WHERE m.is_doc_entry = 0 AND EXISTS "
        "(SELECT 1 FROM resolved_fact f WHERE f.model_id = m.id "
        "AND f.field = 'supports_vision' AND f.v_bool = 1)"
    ).fetchone()[0]
    check(
        "tri-state: unknown is not collapsed into false",
        db_true == v_true
        and db_false == v_false
        and db_unknown == src_unknown
        and search_returns <= db_true,
        f"true={db_true}/{v_true} false={db_false}/{v_false} "
        f"unknown={db_unknown}/{src_unknown} ({src_unknown / len(records):.4f}) "
        f"search_returns={search_returns}",
    )

    # -- 6. reasoning-effort merge -------------------------------------------------
    bool_models = {
        k
        for k, r in records.items()
        if isinstance(r, dict) and any(r.get(f) is True for f in N.EFFORT_BOOL_FIELDS)
    }
    # An empty list ('friendliai/*' carries reasoning_effort_levels: []) contributes no
    # levels, so it must not be counted as a model with reasoning-effort support.
    list_models = {
        k
        for k, r in records.items()
        if isinstance(r, dict)
        and any(isinstance(v, str) for v in (r.get(N.EFFORT_LIST_FIELD) or []))
    }
    union = bool_models | list_models
    db_effort = conn.execute("SELECT COUNT(DISTINCT model_id) FROM reasoning_effort").fetchone()[0]
    # A default is only recorded when it names a level the model actually supports.
    expect_defaults = 0
    for k, r in records.items():
        if not isinstance(r, dict):
            continue
        dflt = r.get(N.EFFORT_DEFAULT_FIELD)
        if not isinstance(dflt, str):
            continue
        levels = {lv for lv in N.EFFORT_BOOL_FIELDS.values() if r.get(
            next(f for f, l in N.EFFORT_BOOL_FIELDS.items() if l == lv)) is True}
        if isinstance(r.get(N.EFFORT_LIST_FIELD), list):
            levels |= {v for v in r[N.EFFORT_LIST_FIELD] if isinstance(v, str)}
        if dflt in levels:
            expect_defaults += 1
    db_defaults = conn.execute(
        "SELECT COUNT(*) FROM reasoning_effort WHERE is_default = 1"
    ).fetchone()[0]
    check(
        "both reasoning-effort encodings merged into one table",
        db_effort == len(union) and db_defaults == expect_defaults,
        f"boolean_encoded={len(bool_models)} list_encoded={len(list_models)} "
        f"overlap={len(bool_models & list_models)} union={len(union)} db={db_effort} "
        f"defaults={db_defaults}/{expect_defaults}",
    )

    # -- 7. price classification coverage -----------------------------------------
    db_price_fields = {r[0] for r in conn.execute("SELECT DISTINCT raw_field FROM price")}
    db_sched_fields = {r[0] for r in conn.execute("SELECT DISTINCT raw_field FROM price_schedule")}
    unclassified = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT raw_field FROM ingest_issue WHERE severity = 'unclassified'"
        )
    }
    uncovered = price_named - db_price_fields - db_sched_fields - unclassified
    stranded = {
        f
        for f in unclassified
        if conn.execute(
            "SELECT COUNT(*) FROM fact WHERE raw_field = ? AND is_derived = 0", (f,)
        ).fetchone()[0]
        == 0
    }
    check(
        "every price-named field is classified, scheduled, or reported",
        not uncovered and not stranded,
        f"price={len(db_price_fields)} schedule={len(db_sched_fields)} "
        f"unclassified={len(unclassified)} uncovered={sorted(uncovered)[:5]} "
        f"stranded={sorted(stranded)[:5]}",
    )

    # -- 8. no-dropped-values invariant -------------------------------------------
    exp = _expectations(records)
    fact_counts = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT raw_field, COUNT(*) FROM fact WHERE is_derived = 0 GROUP BY raw_field"
        )
    }
    # Decomposed schedule bands carry origin_field, so they are attributed to the
    # schedule, not to the inner field name they happen to share with a flat price.
    price_counts = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT raw_field, COUNT(*) FROM price WHERE origin_field IS NULL GROUP BY raw_field"
        )
    }
    sched_counts = {
        r[0]: r[1]
        for r in conn.execute("SELECT raw_field, COUNT(*) FROM price_schedule GROUP BY raw_field")
    }
    effort_counts = {
        r[0]: r[1]
        for r in conn.execute("SELECT raw_field, COUNT(*) FROM reasoning_effort GROUP BY raw_field")
    }
    mismatches = []
    for field, e in sorted(exp.items()):
        if field == N.EFFORT_DEFAULT_FIELD:
            continue  # covered by check 6
        got = (
            fact_counts.get(field, 0) * (1 if e["fact"] else 0)
            + price_counts.get(field, 0) * (1 if e["price"] else 0)
            + sched_counts.get(field, 0) * (1 if e["schedule"] else 0)
            + effort_counts.get(field, 0) * (1 if e["effort"] else 0)
        )
        want = e["fact"] + e["price"] + e["schedule"] + e["effort"]
        if got != want:
            mismatches.append(f"{field}({got}!={want})")
    check(
        "no field lost values between source and database",
        not mismatches,
        f"fields={len(exp)} mismatched={len(mismatches)} "
        + (f"e.g. {mismatches[:5]}" if mismatches else ""),
    )

    # -- 9. alias sanity -----------------------------------------------------------
    bad_aliases = conn.execute(
        "SELECT a.alias FROM model_alias a "
        "JOIN model m1 ON m1.id = a.canonical_model_id "
        "JOIN price p1 ON p1.model_id = m1.id AND p1.raw_field='input_cost_per_token' "
        "AND p1.origin_field IS NULL "
        "WHERE EXISTS (SELECT 1 FROM model m2 JOIN price p2 ON p2.model_id = m2.id "
        "AND p2.raw_field='input_cost_per_token' AND p2.origin_field IS NULL "
        "WHERE m2.key = a.alias AND p2.price != p1.price) LIMIT 5"
    ).fetchall()
    alias_count = conn.execute("SELECT COUNT(*) FROM model_alias").fetchone()[0]
    check("no alias points at a differently-priced model", not bad_aliases, f"aliases={alias_count}")

    # -- 10. determinism -----------------------------------------------------------
    if check_determinism:
        # origin must be the recorded one: it is stored in meta, so passing anything
        # else changes the file's bytes for a reason unrelated to the data.
        observed_at = meta.get("built_at", "1970-01-01T00:00:00Z")
        include_raw = meta.get("include_raw") == "1"
        origin = meta.get("source_origin", "")
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            build(records, tmp.name, observed_at, source_sha, origin=origin, include_raw=include_raw)
            check(
                "rebuild with identical inputs is byte-identical",
                _sha256_file(tmp.name) == _sha256_file(db_path),
                f"db={os.path.basename(db_path)}",
            )
        finally:
            os.unlink(tmp.name)
            for sidecar in ("-journal", "-wal", "-shm"):
                if os.path.exists(tmp.name + sidecar):
                    os.unlink(tmp.name + sidecar)
    else:
        check("determinism", True, "skipped")

    conn.close()
    return results
