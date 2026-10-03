"""Read-side helpers.

The interesting one is why there is no `price_at()` any more. The previous dataset priced
by *token-count bands* (`input_cost_per_token` plus surcharge rows above 200k/272k tokens),
so a band-resolution function was the only correct way in. This dataset prices by
*time-of-day band* (peak / off-peak) in whole dollars per million tokens, and has no token
bands at all. Carrying the old function over would have been worse than deleting it: it
would have returned a plausible number for a question nobody can ask.

Prices here are `per_million_tokens`. Multiply before comparing against a per-token figure.
"""

import os
import sqlite3

DEFAULT_DB = "modelmeta.db"


def connect(path=DEFAULT_DB, read_only=False):
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_model(conn, name):
    """Resolve by catalog_id, then canonical_slug, then display name. Exact matches only.

    No fuzzy matching: this dataset's own `identity` rule forbids merging by string
    similarity, and guessing which of several similarly-named models was meant is exactly
    the kind of wrong answer a metadata registry must not give.
    """
    for column in ("catalog_id", "canonical_slug", "name"):
        row = conn.execute(
            f"SELECT * FROM model WHERE {column} = ?", (name,)
        ).fetchone()
        if row:
            return row
    return None


def model_capabilities(conn, model_pk):
    """Aggregated tri-state per capability for a model (see the view's definition)."""
    rows = conn.execute(
        "SELECT capability, value FROM model_capability WHERE model_id = ? ORDER BY capability",
        (model_pk,),
    ).fetchall()
    return {r["capability"]: r["value"] for r in rows}


def offerings_for(conn, model_pk):
    return conn.execute(
        "SELECT * FROM offering WHERE model_id = ? ORDER BY authority, provider, source_id",
        (model_pk,),
    ).fetchall()


def capability_rows(conn, offering_pk):
    """Tri-state values for one offering, with the taxonomy so callers can explain them."""
    return conn.execute(
        "SELECT c.name, c.value, d.domain, d.evidence_aspect, d.false_is_meaningful "
        "FROM capability c JOIN capability_def d ON d.name = c.name "
        "WHERE c.offering_id = ? ORDER BY c.name",
        (offering_pk,),
    ).fetchall()


def evidence_for(conn, offering_pk):
    return {
        r["aspect"]: r["text"]
        for r in conn.execute(
            "SELECT aspect, text FROM capability_evidence WHERE offering_id = ? ORDER BY aspect",
            (offering_pk,),
        )
    }


def modalities_for(conn, offering_pk):
    out = {"input": [], "output": []}
    for r in conn.execute(
        "SELECT direction, modality FROM modality WHERE offering_id = ? "
        "ORDER BY direction, modality",
        (offering_pk,),
    ):
        out[r["direction"]].append(r["modality"])
    return out


def prices_for(conn, offering_pk):
    return conn.execute(
        "SELECT kind, per_million_tokens, currency, token_unit, time_band, source_id "
        "FROM price WHERE offering_id = ? "
        "ORDER BY (time_band IS NOT NULL), time_band, kind",
        (offering_pk,),
    ).fetchall()


def price_raw_for(conn, offering_pk):
    return {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM price_raw WHERE offering_id = ? ORDER BY key",
            (offering_pk,),
        )
    }


def price_note_for(conn, offering_pk):
    row = conn.execute(
        "SELECT note FROM price_note WHERE offering_id = ?", (offering_pk,)
    ).fetchone()
    return row["note"] if row else None


def reasoning_for(conn, offering_pk):
    profile = conn.execute(
        "SELECT mandatory, default_enabled, default_effort FROM reasoning_profile "
        "WHERE offering_id = ?",
        (offering_pk,),
    ).fetchone()
    if profile is None:
        return None
    efforts = [
        r["effort"]
        for r in conn.execute(
            "SELECT effort FROM reasoning_effort WHERE offering_id = ? ORDER BY effort",
            (offering_pk,),
        )
    ]
    return {"profile": profile, "efforts": efforts}


def supported_parameters_for(conn, offering_pk):
    return [
        r["parameter"]
        for r in conn.execute(
            "SELECT parameter FROM supported_parameter WHERE offering_id = ? ORDER BY parameter",
            (offering_pk,),
        )
    ]


def voices_for(conn, offering_pk):
    return [
        r["voice"]
        for r in conn.execute(
            "SELECT voice FROM supported_voice WHERE offering_id = ? ORDER BY voice",
            (offering_pk,),
        )
    ]


def protocols_for(conn, offering_pk):
    return [
        r["protocol"]
        for r in conn.execute(
            "SELECT protocol FROM api_protocol WHERE offering_id = ? ORDER BY protocol",
            (offering_pk,),
        )
    ]


def conflicts_for(conn, model_pk):
    return conn.execute(
        "SELECT * FROM conflict WHERE model_id = ? ORDER BY field, source_id", (model_pk,)
    ).fetchall()


def normalization_rules(conn):
    return conn.execute(
        "SELECT name, text FROM normalization_rule ORDER BY name"
    ).fetchall()


def model_count(conn):
    return conn.execute("SELECT COUNT(*) FROM model").fetchone()[0]


def db_size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0
