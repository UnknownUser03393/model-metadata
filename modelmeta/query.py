"""Read-side queries, including the banded price access path.

`price_at` is registered as a SQL scalar function rather than exposed as a VIEW, because
SQLite views cannot take parameters. Shipping this one function is the difference
between consumers getting banded pricing right and each of them reimplementing it
wrongly: dashscope/qwen-flash carries no flat input_cost_per_token at all, so a naive
`SELECT price FROM price WHERE direction='input'` returns nothing useful for it.
"""

import os
import sqlite3

DEFAULT_DB = "modelmeta.db"

# The one supported band resolution rule: the band with the largest band_start that is
# still <= the token count. Base price is band_start = 0; '_above_200k_tokens' rows are
# surcharge bands in the same slot, so "largest band_start <= n" is correct for both.
PRICE_AT_SQL = """
SELECT p.price
FROM price p JOIN source s ON s.id = p.source_id
WHERE p.model_id = :model_id
  AND p.direction = :direction
  AND p.unit = 'per_token'
  AND (p.band_start IS NULL OR p.band_start <= :n_tokens)
  AND p.tier IS NULL
  AND p.modality IS NULL
  AND p.cache_ttl IS NULL
  AND (p.qualifier IS NULL OR p.qualifier NOT LIKE '%scaled%')
ORDER BY s.priority DESC, (p.band_start IS NULL), p.band_start DESC
LIMIT 1
"""


def connect(path=DEFAULT_DB, read_only=False, register_price_at=True):
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if register_price_at:
        _register_price_at(conn)

    def price_at(model_id, direction, n_tokens):
        row = conn.execute(
            PRICE_AT_SQL,
            {"model_id": model_id, "direction": direction, "n_tokens": n_tokens},
        ).fetchone()
        return row[0] if row else None

    conn.create_function("price_at", 3, price_at)
    return conn


def _register_price_at(_conn):
    return None


def resolve_model(conn, name):
    """Look a model up by key, exact, or alias. Returns a Row or None."""
    row = conn.execute("SELECT * FROM model WHERE key = ?", (name,)).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT m.* FROM model m JOIN model_alias a ON a.canonical_model_id = m.id "
        "WHERE a.alias = ?",
        (name,),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM model WHERE key = ? ORDER BY length(key) LIMIT 1", (name,)
    ).fetchone()


def model_facts(conn, model_id, fields=None):
    """``fields`` is a list of canonical field names (the CLI's --field is repeatable)."""
    sql = "SELECT * FROM resolved_fact WHERE model_id = ?"
    params = [model_id]
    if fields:
        sql += " AND field IN (%s)" % ",".join("?" * len(fields))
        params.extend(fields)
    sql += " ORDER BY field"
    return conn.execute(sql, params).fetchall()


def model_prices(conn, model_id):
    return conn.execute(
        "SELECT * FROM price WHERE model_id = ? "
        "ORDER BY direction, unit, (band_start IS NULL), band_start, tier, modality",
        (model_id,),
    ).fetchall()


def model_efforts(conn, model_id):
    return conn.execute(
        "SELECT level, encoding, is_default FROM reasoning_effort WHERE model_id = ? "
        "ORDER BY level",
        (model_id,),
    ).fetchall()


def model_schedules(conn, model_id):
    return conn.execute(
        "SELECT raw_field, v_json FROM price_schedule WHERE model_id = ? ORDER BY raw_field",
        (model_id,),
    ).fetchall()


def base_price(conn, model_id, direction="input"):
    """Flat base per-token price (band 0), for search filtering."""
    row = conn.execute(
        "SELECT price FROM price WHERE model_id = ? AND direction = ? AND unit = 'per_token' "
        "AND band_start = 0 AND tier IS NULL AND modality IS NULL AND cache_ttl IS NULL "
        "AND qualifier IS NULL ORDER BY price LIMIT 1",
        (model_id, direction),
    ).fetchone()
    return row[0] if row else None


def schema_present(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model'"
    ).fetchone()
    return row is not None


def db_size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0
