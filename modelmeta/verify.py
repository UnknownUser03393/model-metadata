"""Completeness and self-consistency checks against the snapshot.

What this can and cannot prove. The snapshot is the truth source; there is no higher
authority. These checks prove that the mirror is faithful -- nothing dropped, no
tri-state flattened, no conflict resolved, no suspicious value quietly repaired -- and
that a rebuild is byte-identical. They do NOT prove the metadata is *correct*: the
snapshot itself states that inference_calls_performed is 0, so nothing here has been
verified against a running model, and this database faithfully records that too.

Expectations are recomputed from the snapshot rather than hardcoded, so the checks keep
working as the snapshot evolves. The counts printed in each detail string are the current
values for humans to eyeball.
"""

import hashlib
import json
import os
import sqlite3
import tempfile

from . import schema as S
from . import query as Q
from .fetch import read_snapshot
from .ingest import build, content_hash


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_counts(doc):
    """Row counts the snapshot implies, computed the same way the loader splits it."""
    models = doc["models"]
    ex = {
        "model": len(models),
        "offering": 0,
        "capability": 0,
        "capability_evidence": 0,
        "modality": 0,
        "supported_parameter": 0,
        "default_parameter": 0,
        "supported_voice": 0,
        "reasoning_profile": 0,
        "reasoning_effort": 0,
        "price": 0,
        "price_raw": 0,
        "price_note": 0,
        "offering_source": 0,
        "api_protocol": 0,
        "conflict": 0,
    }
    for m in models:
        ex["conflict"] += len([c for c in (m.get("conflicts") or []) if isinstance(c, dict)])
        for group in ("offerings", "official_offerings"):
            for off in m.get(group) or []:
                if not isinstance(off, dict):
                    continue
                ex["offering"] += 1
                caps = off.get("capabilities")
                if isinstance(caps, dict):
                    ex["capability"] += len(
                        [k for k in caps if k in S.CAPABILITY_NAMES]
                    )
                ev = off.get("capability_evidence")
                if isinstance(ev, dict):
                    ex["capability_evidence"] += len(ev)
                mods = off.get("modalities") or {}
                for direction in ("input", "output"):
                    ex["modality"] += len(
                        {v for v in (mods.get(direction) or []) if isinstance(v, str)}
                    )
                ex["supported_parameter"] += len(
                    {v for v in (off.get("supported_parameters") or []) if isinstance(v, str)}
                )
                ex["default_parameter"] += len(off.get("default_parameters") or {})
                ex["supported_voice"] += len(
                    {v for v in (off.get("supported_voices") or []) if isinstance(v, str)}
                )
                r = off.get("reasoning")
                if isinstance(r, dict):
                    ex["reasoning_profile"] += 1
                    ex["reasoning_effort"] += len(
                        {v for v in (r.get("supported_efforts") or []) if isinstance(v, str)}
                    )
                p = off.get("pricing")
                if isinstance(p, dict):
                    tiers = p.get("tiers")
                    if isinstance(tiers, list):
                        for t in tiers:
                            if isinstance(t, dict):
                                ex["price"] += len([k for k in S.PRICE_KINDS.values() if k in t])
                    else:
                        ex["price"] += len([k for k in S.PRICE_KINDS.values() if k in p])
                    raw = p.get("raw")
                    if isinstance(raw, dict):
                        ex["price_raw"] += len(raw)
                    if p.get("notes"):
                        ex["price_note"] += 1
                ex["offering_source"] += len(
                    {v for v in (off.get("additional_source_ids") or []) if isinstance(v, str)}
                )
                ex["api_protocol"] += len(
                    {v for v in (off.get("api_protocols") or []) if isinstance(v, str)}
                )
    return ex


def verify(db_path, snapshot_path, check_determinism=True):
    doc, _file_sha = read_snapshot(snapshot_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    results = []

    def check(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    def scalar(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    # -- 0. identity ---------------------------------------------------------------
    check(
        "database was built from this snapshot's content",
        meta.get("content_sha256") == content_hash(doc),
        f"db={str(meta.get('content_sha256'))[:12]} snapshot={content_hash(doc)[:12]}",
    )

    # -- 1. row-count reconciliation ------------------------------------------------
    ex = expected_counts(doc)
    mismatched = []
    for table in sorted(ex):
        got = scalar(f"SELECT COUNT(*) FROM {table}")
        if got != ex[table]:
            mismatched.append(f"{table}({got}!={ex[table]})")
    check(
        "every key in the snapshot has the same number of rows",
        not mismatched,
        f"tables={len(ex)} mismatched={len(mismatched)} "
        + (f"e.g. {mismatched}" if mismatched else
           f"| models={ex['model']} offerings={ex['offering']} "
           f"capabilities={ex['capability']} prices={ex['price']}"),
    )

    n_catalog = scalar("SELECT COUNT(*) FROM offering WHERE authority='catalog'")
    n_official = scalar("SELECT COUNT(*) FROM offering WHERE authority='official'")
    empty_models = scalar(
        "SELECT COUNT(*) FROM model m WHERE NOT EXISTS "
        "(SELECT 1 FROM offering o WHERE o.model_id = m.id)"
    )
    # The snapshot states these counts itself, so they are an independent cross-check on
    # the loader. Fixtures may omit the block; then there is nothing to cross-check.
    stats_block = doc.get("statistics") or {}
    want_catalog = stats_block.get("aggregator_catalog_records")
    want_official = stats_block.get("officially_cross_checked_records")
    if want_catalog is None or want_official is None:
        check("offering split matches the snapshot's own statistics", True,
              "skipped (no statistics block in this snapshot)")
    else:
        check(
            "offering split matches the snapshot's own statistics",
            n_catalog == want_catalog and n_official == want_official,
            f"catalog={n_catalog}/{want_catalog} official={n_official}/{want_official} "
            f"models_without_offering={empty_models}",
        )

    # -- 2. tri-state shape per capability kind ------------------------------------
    impossible = sorted(S.FALSE_IS_IMPOSSIBLE)
    zeros_where_impossible = {
        name: scalar("SELECT COUNT(*) FROM capability WHERE name=? AND value=0", (name,))
        for name in impossible
    }
    bad = {k: v for k, v in zeros_where_impossible.items() if v}
    check(
        "no capability reports false where upstream cannot say false",
        not bad,
        f"checked {len(impossible)} parameter/operational capabilities; offenders={bad or 'none'}",
    )

    meaningful = sorted(S.FALSE_IS_MEANINGFUL)
    nulls_where_meaningful = {
        name: scalar("SELECT COUNT(*) FROM capability WHERE name=? AND value IS NULL", (name,))
        for name in meaningful
    }
    bad = {k: v for k, v in nulls_where_meaningful.items() if v}
    check(
        "no declared-modality capability lost a value to null",
        not bad,
        f"checked {len(meaningful)} modality capabilities; offenders={bad or 'none'}",
    )

    # Not "as many nulls as offerings" -- only the offerings that declare a capabilities
    # object have these keys at all. The invariant that matters is that none of them ever
    # carries a value: upstream declares the field and never establishes it.
    never = {}
    for name in ("parallel_tool_calls", "streaming"):
        declared = scalar("SELECT COUNT(*) FROM capability WHERE name = ?", (name,))
        established = scalar(
            "SELECT COUNT(*) FROM capability WHERE name = ? AND value IS NOT NULL", (name,)
        )
        never[name] = (established, declared)
    check(
        "capabilities that are never established stay null, not false",
        all(established == 0 and declared > 0 for established, declared in never.values()),
        " ".join(
            f"{name}: {established} established of {declared} declared"
            for name, (established, declared) in never.items()
        ),
    )

    # -- 3. nothing was verified, and the database says so --------------------------
    tested = scalar("SELECT COUNT(*) FROM offering WHERE inference_tested = 1")
    calls = scalar("SELECT value FROM statistic WHERE name='inference_calls_performed'")
    check(
        "the snapshot's zero-verification status is preserved and visible",
        tested == 0 and (calls is None or calls == 0),
        f"offerings_with_inference_tested=1: {tested}  "
        f"statistic.inference_calls_performed={calls}",
    )

    # -- 4. suspicious values were preserved, not repaired --------------------------
    zero_ctx = scalar("SELECT COUNT(*) FROM offering WHERE context_tokens = 0")
    zero_out = scalar("SELECT COUNT(*) FROM offering WHERE max_output_tokens = 0")
    src_zero_ctx = sum(
        1
        for m in doc["models"]
        for g in ("offerings", "official_offerings")
        for off in (m.get(g) or [])
        if isinstance(off, dict) and (off.get("limits") or {}).get("context_tokens") == 0
    )
    check(
        "zero-valued token limits are stored verbatim, not silently nulled",
        zero_ctx == src_zero_ctx and zero_ctx > 0,
        f"context_tokens=0: {zero_ctx} (snapshot {src_zero_ctx})  max_output_tokens=0: {zero_out}",
    )

    # -- 5. columns the snapshot deliberately leaves empty --------------------------
    empties = {
        (tbl, col): scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {col} IS NOT NULL")
        for tbl, col in (
            ("offering", "max_input_tokens"),
            ("model", "release_date"),
            ("model", "open_weights"),
            ("model", "license"),
            ("model", "parameter_count"),
        )
    }
    check(
        "fields the snapshot leaves unestablished are empty, not fabricated",
        all(v == 0 for v in empties.values()),
        " ".join(f"{t}.{c}={n}" for (t, c), n in empties.items()),
    )

    # -- 6. sparse official offerings ------------------------------------------------
    # Counted from the snapshot, not hardcoded: the number of documentation stubs is a
    # property of the data, not of the loader.
    src_stubs = sum(
        1
        for m in doc["models"]
        for off in (m.get("official_offerings") or [])
        if isinstance(off, dict) and off.get("model_id") is None
    )
    stub_count = scalar(
        "SELECT COUNT(*) FROM offering WHERE authority='official' "
        "AND provider_model_id IS NULL"
    )
    stub_shape = scalar(
        "SELECT COUNT(*) FROM offering o WHERE o.authority='official' "
        "AND o.provider_model_id IS NULL AND o.notes IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM capability c WHERE c.offering_id = o.id) "
        "AND NOT EXISTS (SELECT 1 FROM price p WHERE p.offering_id = o.id)"
    )
    check(
        "official documentation stubs are represented as stubs",
        stub_count == src_stubs and stub_shape == stub_count,
        f"official stubs={stub_count} (snapshot {src_stubs}) "
        f"with_notes_and_no_detail={stub_shape}",
    )

    # -- 7. effort sets: order discarded, membership kept ----------------------------
    snap_reps = set()
    snap_sets = set()
    for m in doc["models"]:
        for g in ("offerings", "official_offerings"):
            for off in (m.get(g) or []):
                if not isinstance(off, dict):
                    continue
                reasoning = off.get("reasoning")
                if isinstance(reasoning, dict) and reasoning.get("supported_efforts") is not None:
                    levels = tuple(reasoning["supported_efforts"])
                    snap_reps.add(levels)
                    snap_sets.add(frozenset(levels))

    db_sets = {
        tuple(
            r[0]
            for r in conn.execute(
                "SELECT effort FROM reasoning_effort WHERE offering_id = ? ORDER BY effort",
                (row[0],),
            )
        )
        for row in conn.execute("SELECT DISTINCT offering_id FROM reasoning_effort")
    }
    check(
        "reasoning effort is stored as a set; list ordering is discarded",
        db_sets == {tuple(sorted(s)) for s in snap_sets} and len(snap_reps) > len(snap_sets),
        f"snapshot has {len(snap_reps)} list representations collapsing to {len(snap_sets)} "
        f"distinct sets; database holds {len(db_sets)} distinct sets",
    )

    # -- 8. conflicts retained, not resolved ----------------------------------------
    conflict_rows = scalar("SELECT COUNT(*) FROM conflict")
    src_conflicts = ex["conflict"]
    both_retained = scalar(
        "SELECT COUNT(*) FROM conflict WHERE official_value_json IS NOT NULL "
        "AND aggregator_value_json IS NOT NULL AND resolution <> ''"
    )
    check(
        "recorded conflicts keep both values and their resolution",
        conflict_rows == src_conflicts and both_retained == conflict_rows,
        f"conflicts={conflict_rows}/{src_conflicts} with_both_values={both_retained}",
    )

    # -- 9. capability registry and database agree ----------------------------------
    db_defs = {
        (r["name"], r["domain"], r["evidence_aspect"], r["false_is_meaningful"])
        for r in conn.execute(
            "SELECT name, domain, evidence_aspect, false_is_meaningful FROM capability_def"
        )
    }
    check(
        "the in-code capability registry matches capability_def",
        db_defs == set(S.capability_rows()),
        f"python={len(S.capability_rows())} db={len(db_defs)}",
    )

    # -- 10. determinism, including the encoding regression ------------------------
    if check_determinism:
        origin = meta.get("origin", "")
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            build(doc, tmp.name, origin=origin)
            same = _sha256_file(tmp.name) == _sha256_file(db_path)
            check(
                "rebuild from the same snapshot is byte-identical",
                same,
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


def verify_regression_encodings(snapshot_path):
    """The previous iteration's specific defect, as a test.

    The same data encoded as UTF-16+CRLF and as UTF-8+LF must produce byte-identical
    databases. It did not before, because the recorded hash was of the raw file bytes.
    """
    doc, _ = read_snapshot(snapshot_path)
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)
    variants = {
        "utf8-lf": text.encode("utf-8"),
        "utf8-bom": ("﻿" + text).encode("utf-8"),
        "utf16-crlf": text.replace("\n", "\r\n").encode("utf-16"),
    }
    hashes = {}
    for label, raw in variants.items():
        from .fetch import load_document

        d = load_document(raw)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            build(d, tmp.name, origin="regression")
            hashes[label] = _sha256_file(tmp.name)
        finally:
            os.unlink(tmp.name)
            for sidecar in ("-journal", "-wal", "-shm"):
                if os.path.exists(tmp.name + sidecar):
                    os.unlink(tmp.name + sidecar)
    return hashes
