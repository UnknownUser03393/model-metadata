"""Mirror the curated snapshot into SQLite.

The governing rule is that this loader **does not interpret**. It moves values into typed
columns, splits sets into rows, and reports anything it does not recognise. It never
infers a capability, never coerces a NULL to false, never repairs a suspicious token limit
and never resolves a conflict. If upstream says something strange, the database says the
same strange thing and `ingest_issue` records that it was seen.

Determinism is a hard requirement: the .db is committed, and a rebuild of unchanged input
must be byte-identical. Every iteration order is sorted, every JSON serialisation is
key-sorted, the only timestamp is the snapshot's own `snapshot_at` (never the wall clock),
and string lists are stored as sets so that upstream's inconsistent list ordering cannot
change the output.
"""

import hashlib
import json
import os
import sqlite3

from . import schema as S

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonicalize(obj):
    """A canonical form: dict keys sorted, lists reduced to a deterministic order.

    Lists of scalars are sorted, because the snapshot stores set-valued lists
    (modalities, supported_parameters, supported_efforts) in inconsistent order and the
    database stores them as sets. Two documents that differ only in such ordering are the
    same data and must hash alike.
    """
    if isinstance(obj, dict):
        return {k: canonicalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        items = [canonicalize(v) for v in obj]
        if all(isinstance(v, (str, int, float, bool)) or v is None for v in items):
            return sorted(items, key=lambda v: (v is None, str(v)))
        return sorted(items, key=dumps)
    return obj


def content_hash(doc):
    """Hash of what the document *means*, not of its bytes.

    The previous iteration hashed the raw file, so the same data arriving as UTF-16+CRLF
    and as UTF-8+LF produced different hashes and the committed database could not be
    reproduced from the documented path. This hash is stable across encoding, indentation
    and set ordering.
    """
    return hashlib.sha256(dumps(canonicalize(doc)).encode("utf-8")).hexdigest()


def _as_bool_int(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    return None


def _as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_text(value):
    return value if isinstance(value, str) else None


def _string_set(value):
    """A set-valued list, order discarded. Non-strings are dropped (and are unusual)."""
    if not isinstance(value, list):
        return []
    return sorted({v for v in value if isinstance(v, str)})


def build(doc, out_path, origin=""):
    """Create the database. Returns (stats, issues)."""
    issues = []

    def issue(severity, scope, detail):
        issues.append((severity, scope, detail))

    # ---- shape drift reporting ---------------------------------------------------
    for key in sorted(set(doc) - S.KNOWN_TOP_LEVEL):
        issue("unknown_key", "document", f"unrecognised top-level key {key!r}")

    models = list(doc["models"])

    # Any source referenced anywhere must exist, or the foreign keys fail. A reference to
    # an undeclared source is a real upstream gap, so it gets a stub row AND an issue
    # rather than being silently dropped.
    declared_sources = set(doc.get("sources") or {})
    referenced = set()
    for m in models:
        for group in ("offerings", "official_offerings"):
            for off in m.get(group) or []:
                if isinstance(off, dict):
                    referenced.add(off.get("source_id"))
                    for s in off.get("additional_source_ids") or []:
                        referenced.add(s)
                    pricing = off.get("pricing")
                    if isinstance(pricing, dict):
                        referenced.add(pricing.get("source_id"))
    referenced.discard(None)
    undeclared = sorted(referenced - declared_sources)
    for sid in undeclared:
        issue(
            "undeclared_source",
            sid,
            "referenced by an offering but absent from the snapshot's `sources` map",
        )

    built_at = _as_text(doc.get("snapshot_at")) or ""

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

    # ---- document metadata -------------------------------------------------------
    for key in ("schema_version", "snapshot_at", "as_of_date"):
        value = doc.get(key)
        if value is not None:
            cur.execute("INSERT INTO meta (key, value) VALUES (?,?)", (key, str(value)))

    scope = doc.get("scope") or {}
    for i, name in enumerate(scope.get("mainstream_author_namespaces") or []):
        cur.execute(
            "INSERT INTO scope_item (kind, ordinal, value) VALUES (?,?,?)",
            ("mainstream_author_namespace", i, name),
        )
    for i, text in enumerate(scope.get("coverage_gaps") or []):
        cur.execute(
            "INSERT INTO scope_item (kind, ordinal, value) VALUES (?,?,?)",
            ("coverage_gap", i, text),
        )

    for name in sorted(doc.get("normalization_rules") or {}):
        cur.execute(
            "INSERT INTO normalization_rule (name, text) VALUES (?,?)",
            (name, doc["normalization_rules"][name]),
        )

    for name in sorted(doc.get("statistics") or {}):
        cur.execute(
            "INSERT INTO statistic (name, value) VALUES (?,?)",
            (name, float(doc["statistics"][name])),
        )

    for sid in sorted(declared_sources):
        src = doc["sources"][sid] or {}
        cur.execute(
            "INSERT INTO source (id, url, kind, retrieved_at, live_http_status) "
            "VALUES (?,?,?,?,?)",
            (
                sid,
                _as_text(src.get("url")),
                _as_text(src.get("kind")) or "unknown",
                _as_text(src.get("retrieved_at")),
                _as_int(src.get("live_http_status")),
            ),
        )
    for sid in undeclared:
        cur.execute(
            "INSERT INTO source (id, url, kind, retrieved_at, live_http_status) "
            "VALUES (?,?,?,?,?)",
            (sid, None, "referenced_but_undeclared", None, None),
        )

    for namespace in sorted(doc.get("provider_namespace_counts") or {}):
        cur.execute(
            "INSERT INTO provider_namespace_count (namespace, n) VALUES (?,?)",
            (namespace, int(doc["provider_namespace_counts"][namespace])),
        )

    for i, check in enumerate(doc.get("endpoint_discovery_checks") or []):
        if not isinstance(check, dict):
            issue("drop", "endpoint_discovery_checks", f"entry {i} is not an object")
            continue
        cur.execute(
            "INSERT INTO endpoint_discovery_check "
            "(id, model_id, url, status, response, checked_at, endpoint_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                i,
                _as_text(check.get("model_id")),
                _as_text(check.get("url")),
                _as_text(check.get("status")),
                _as_text(check.get("response")),
                _as_text(check.get("checked_at")),
                _as_int(check.get("endpoint_count")),
            ),
        )

    # ---- capability taxonomy (mirrors modelmeta/schema.py) ------------------------
    for name, domain, aspect, false_ok in S.capability_rows():
        cur.execute(
            "INSERT INTO capability_def (name, domain, evidence_aspect, false_is_meaningful) "
            "VALUES (?,?,?,?)",
            (name, domain, aspect, false_ok),
        )

    # ---- models and offerings ----------------------------------------------------
    counts = {"capability": 0, "modality": 0, "price": 0, "conflict": 0}
    model_id = 0
    offering_id = 0

    for model in sorted(models, key=lambda m: m["catalog_id"]):
        catalog_id = model["catalog_id"]

        for key in sorted(set(model) - S.KNOWN_MODEL_KEYS):
            issue("unknown_key", catalog_id, f"unrecognised model key {key!r}")

        model_id += 1
        selection = model.get("selection") or {}
        identity = model.get("model_identity") or {}
        cur.execute(
            "INSERT INTO model (id, catalog_id, canonical_slug, name, author, "
            "mainstream_author, catalog_order, common_candidate, usage_verified, "
            "hugging_face_id, release_date, catalog_created_at, knowledge_cutoff, "
            "open_weights, license, parameter_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                model_id,
                catalog_id,
                _as_text(model.get("canonical_slug")),
                model.get("name") or catalog_id,
                model.get("author") or "unknown",
                _as_bool_int(selection.get("mainstream_author")),
                _as_int(selection.get("catalog_order")),
                _as_bool_int(selection.get("common_candidate")),
                _as_bool_int(selection.get("usage_verified")),
                _as_text(identity.get("hugging_face_id")),
                _as_text(identity.get("release_date")),
                _as_text(identity.get("catalog_created_at")),
                _as_text(identity.get("knowledge_cutoff")),
                _as_bool_int(identity.get("open_weights")),
                _as_text(identity.get("license")),
                _as_int(identity.get("parameter_count")),
            ),
        )

        for conflict in sorted(
            (c for c in (model.get("conflicts") or []) if isinstance(c, dict)),
            key=lambda c: (c.get("field") or "", c.get("source_id") or ""),
        ):
            counts["conflict"] += 1
            cur.execute(
                "INSERT INTO conflict (model_id, field, official_value_json, "
                "aggregator_value_json, resolution, source_id) VALUES (?,?,?,?,?,?)",
                (
                    model_id,
                    _as_text(conflict.get("field")) or "unknown",
                    dumps(conflict.get("official_value")),
                    dumps(conflict.get("aggregator_value")),
                    _as_text(conflict.get("resolution")) or "",
                    _as_text(conflict.get("source_id")),
                ),
            )

        for authority, group in (
            ("catalog", "offerings"),
            ("official", "official_offerings"),
        ):
            entries = [o for o in (model.get(group) or []) if isinstance(o, dict)]
            for off in sorted(
                entries, key=lambda o: (o.get("provider") or "", o.get("source_id") or "")
            ):
                offering_id += 1

                for key in sorted(set(off) - S.KNOWN_OFFERING_KEYS):
                    issue("unknown_key", catalog_id, f"unrecognised offering key {key!r}")

                availability = off.get("availability") or {}
                limits = off.get("limits") or {}
                status = _as_text(availability.get("status"))
                if status is not None and status not in S.AVAILABILITY_STATUSES:
                    issue(
                        "unknown_enum",
                        catalog_id,
                        f"availability.status={status!r} is not in the known vocabulary",
                    )

                cur.execute(
                    "INSERT INTO offering (id, model_id, authority, provider, "
                    "provider_model_id, source_id, availability_status, inference_tested, "
                    "account_access_verified, expiration_date, endpoint_count_observed, "
                    "context_tokens, max_output_tokens, max_input_tokens, tokenizer, "
                    "notes, knowledge_cutoff) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        offering_id,
                        model_id,
                        authority,
                        off.get("provider") or "unknown",
                        _as_text(off.get("model_id")),
                        off.get("source_id") or "referenced_but_undeclared",
                        status,
                        _as_bool_int(availability.get("inference_tested")),
                        _as_bool_int(availability.get("account_access_verified")),
                        _as_text(availability.get("expiration_date")),
                        _as_int(availability.get("endpoint_count_observed")),
                        # Stored verbatim, including the 73 zero-valued rows. The snapshot's
                        # own rule is "do not guess", so deciding that 0 means unknown is
                        # not this loader's call.
                        _as_int(limits.get("context_tokens")),
                        _as_int(limits.get("max_output_tokens")),
                        _as_int(limits.get("max_input_tokens")),
                        _as_text(off.get("tokenizer")),
                        _as_text(off.get("notes")),
                        _as_text(off.get("knowledge_cutoff")),
                    ),
                )

                for extra in _string_set(off.get("additional_source_ids")):
                    cur.execute(
                        "INSERT OR IGNORE INTO offering_source (offering_id, source_id) "
                        "VALUES (?,?)",
                        (offering_id, extra),
                    )
                for protocol in _string_set(off.get("api_protocols")):
                    cur.execute(
                        "INSERT INTO api_protocol (offering_id, protocol) VALUES (?,?)",
                        (offering_id, protocol),
                    )

                # --- capabilities: the identity transform, and nothing else ---
                caps = off.get("capabilities")
                if isinstance(caps, dict):
                    for name in sorted(caps):
                        if name not in S.CAPABILITY_NAMES:
                            issue(
                                "unknown_capability",
                                catalog_id,
                                f"capability {name!r} is not in the registry; add it to "
                                f"modelmeta/schema.py so its false-semantics are declared",
                            )
                            continue
                        value = _as_bool_int(caps[name])
                        if value == 0 and name not in S.FALSE_IS_MEANINGFUL:
                            # Upstream changing semantics, not a loader bug. Reported
                            # loudly rather than accepted, because a consumer reading this
                            # 0 as a fact would be wrong.
                            issue(
                                "false_where_impossible",
                                catalog_id,
                                f"{name}=false, but upstream only ever reports true/null "
                                f"for this capability; the semantics may have changed",
                            )
                        counts["capability"] += 1
                        cur.execute(
                            "INSERT INTO capability (offering_id, name, value) VALUES (?,?,?)",
                            (offering_id, name, value),
                        )

                for aspect in sorted(off.get("capability_evidence") or {}):
                    cur.execute(
                        "INSERT INTO capability_evidence (offering_id, aspect, text) "
                        "VALUES (?,?,?)",
                        (offering_id, aspect, off["capability_evidence"][aspect]),
                    )

                modalities = off.get("modalities") or {}
                for direction in ("input", "output"):
                    for modality in _string_set(modalities.get(direction)):
                        counts["modality"] += 1
                        cur.execute(
                            "INSERT INTO modality (offering_id, direction, modality) "
                            "VALUES (?,?,?)",
                            (offering_id, direction, modality),
                        )

                for param in _string_set(off.get("supported_parameters")):
                    cur.execute(
                        "INSERT INTO supported_parameter (offering_id, parameter) "
                        "VALUES (?,?)",
                        (offering_id, param),
                    )
                for name in sorted(off.get("default_parameters") or {}):
                    cur.execute(
                        "INSERT INTO default_parameter (offering_id, name, value_json) "
                        "VALUES (?,?,?)",
                        (offering_id, name, dumps(off["default_parameters"][name])),
                    )
                for voice in _string_set(off.get("supported_voices")):
                    cur.execute(
                        "INSERT INTO supported_voice (offering_id, voice) VALUES (?,?)",
                        (offering_id, voice),
                    )

                # --- reasoning: effort set, order discarded ---
                reasoning = off.get("reasoning")
                if isinstance(reasoning, dict):
                    cur.execute(
                        "INSERT INTO reasoning_profile (offering_id, mandatory, "
                        "default_enabled, default_effort) VALUES (?,?,?,?)",
                        (
                            offering_id,
                            _as_bool_int(reasoning.get("mandatory")),
                            _as_bool_int(reasoning.get("default_enabled")),
                            _as_text(reasoning.get("default_effort")),
                        ),
                    )
                    for effort in _string_set(reasoning.get("supported_efforts")):
                        cur.execute(
                            "INSERT INTO reasoning_effort (offering_id, effort) "
                            "VALUES (?,?)",
                            (offering_id, effort),
                        )

                # --- pricing ---
                pricing = off.get("pricing")
                if isinstance(pricing, dict):
                    currency = _as_text(pricing.get("currency")) or "USD"
                    token_unit = _as_int(pricing.get("token_unit")) or 0
                    tiers = pricing.get("tiers")
                    if isinstance(tiers, list):
                        for tier in tiers:
                            if not isinstance(tier, dict):
                                continue
                            band = _as_text(tier.get("time_band"))
                            for kind, key in S.PRICE_KINDS.items():
                                if key not in tier:
                                    continue
                                counts["price"] += 1
                                cur.execute(
                                    "INSERT INTO price (offering_id, kind, "
                                    "per_million_tokens, currency, token_unit, time_band, "
                                    "source_id) VALUES (?,?,?,?,?,?,?)",
                                    (
                                        offering_id,
                                        kind,
                                        float(tier[key]) if isinstance(tier[key], (int, float))
                                        and not isinstance(tier[key], bool) else None,
                                        currency,
                                        token_unit,
                                        band,
                                        _as_text(pricing.get("source_id")),
                                    ),
                                )
                    else:
                        for kind, key in S.PRICE_KINDS.items():
                            if key not in pricing:
                                continue
                            counts["price"] += 1
                            value = pricing[key]
                            cur.execute(
                                "INSERT INTO price (offering_id, kind, per_million_tokens, "
                                "currency, token_unit, time_band, source_id) "
                                "VALUES (?,?,?,?,?,?,?)",
                                (
                                    offering_id,
                                    kind,
                                    float(value)
                                    if isinstance(value, (int, float))
                                    and not isinstance(value, bool)
                                    else None,
                                    currency,
                                    token_unit,
                                    None,
                                    _as_text(pricing.get("source_id")),
                                ),
                            )
                    for raw_key in sorted(pricing.get("raw") or {}):
                        cur.execute(
                            "INSERT INTO price_raw (offering_id, key, value) VALUES (?,?,?)",
                            (offering_id, raw_key, str(pricing["raw"][raw_key])),
                        )
                    note = _as_text(pricing.get("notes"))
                    if note:
                        cur.execute(
                            "INSERT INTO price_note (offering_id, note) VALUES (?,?)",
                            (offering_id, note),
                        )

    # ---- ledger ------------------------------------------------------------------
    for severity, scope_key, detail in issues:
        cur.execute(
            "INSERT INTO ingest_issue (severity, scope, detail, created_at) VALUES (?,?,?,?)",
            (severity, scope_key, detail, built_at),
        )

    fid = content_hash(doc)
    stats = {
        "model_count": model_id,
        "offering_count": offering_id,
        **counts,
        "issue_count": len(issues),
    }
    meta = {
        "content_sha256": fid,
        "origin": origin,
        "generator": S.GENERATOR,
        "built_at": built_at,
        **{k: str(v) for k, v in stats.items()},
    }
    for key in sorted(meta):
        cur.execute("INSERT INTO meta (key, value) VALUES (?,?)", (key, meta[key]))

    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("VACUUM")
    conn.execute("PRAGMA optimize")
    conn.commit()
    conn.close()

    return stats, issues
