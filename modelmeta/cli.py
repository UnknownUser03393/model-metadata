"""Command line interface: `python -m modelmeta <command>`."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import GENERATOR, SCHEMA_VERSION, SOURCES
from . import normalize as N
from . import query as Q
from .fetch import UPSTREAM_URL, read_source
from .ingest import build
from .verify import verify as run_verify

# CLI flags expand to the REAL underlying source fields. No synthetic aliases are
# invented: supports_function_calling and supports_tool_choice are separate upstream
# facts, and collapsing them into one "supports_tools" would fabricate an OR that the
# source never stated.
CAPABILITY_FLAGS = {
    "vision": "supports_vision",
    "tools": "supports_function_calling",
    "tool_choice": "supports_tool_choice",
    "parallel_tools": "supports_parallel_function_calling",
    "reasoning": "supports_reasoning",
    "audio_in": "supports_audio_input",
    "audio_out": "supports_audio_output",
    "pdf": "supports_pdf_input",
    "caching": "supports_prompt_caching",
    "structured": "supports_response_schema",
    "computer_use": "supports_computer_use",
    "web_search": "supports_web_search",
}

KNOWN_QUERY_FIELDS = (
    "context_window",
    "max_output_tokens",
    "mode",
    "provider",
    "supports_vision",
    "supports_function_calling",
    "supports_reasoning",
    "supports_parallel_function_calling",
    "supports_response_schema",
    "supports_prompt_caching",
)


def _json_default(o):
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


def _emit(payload, as_json, text_lines):
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default))
    else:
        for line in text_lines:
            print(line)


# ---------------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------------


def cmd_update(args):
    records, sha, origin = read_source(path=args.from_file, url=args.from_url)
    observed_at = args.observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stats, issues = build(
        records,
        args.out,
        observed_at,
        sha,
        origin=origin,
        include_raw=not args.no_raw,
    )

    by_severity = {}
    for severity, *_ in issues:
        by_severity[severity] = by_severity.get(severity, 0) + 1

    print(f"source      : {origin}")
    print(f"sha256      : {sha}")
    print(f"observed_at : {observed_at}")
    print(f"entries     : {stats['input_entry_count']} "
          f"({stats['model_count']} model rows, {stats['doc_entry_count']} doc entries)")
    print(f"aliases     : {stats['alias_count']}")
    print(f"price fields: {len(stats['price_fields'])}   "
          f"schedule fields: {len(stats['schedule_fields'])}")
    print(f"issues      : {len(issues)}"
          + (f"  {by_severity}" if by_severity else ""))
    for severity, model_key, raw_field, detail in issues[:20]:
        print(f"  [{severity}] {model_key} {raw_field or ''}: {detail}")
    if len(issues) > 20:
        print(f"  ... {len(issues) - 20} more (see the ingest_issue table)")

    if args.strict and any(s in ("unclassified", "collision") for s, *_ in issues):
        print("strict mode: unclassified/collision issues present", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------------


def cmd_get(args):
    conn = Q.connect(args.db, read_only=True)
    row = Q.resolve_model(conn, args.model)
    if row is None:
        print(f"no such model: {args.model}", file=sys.stderr)
        return 1
    mid = row["id"]

    facts = Q.model_facts(conn, mid, args.field if args.field else None)
    prices = Q.model_prices(conn, mid)
    efforts = Q.model_efforts(conn, mid)

    if args.json:
        payload = {
            "key": row["key"],
            "mode": row["mode"],
            "mode_raw": row["mode_raw"],
            "provider": row["provider"],
            "is_doc_entry": bool(row["is_doc_entry"]),
            "deprecation_date": row["deprecation_date"],
            "facts": [
                {
                    "field": f["field"],
                    "raw_field": f["raw_field"],
                    "value": f["v_bool"] if f["value_type"] == "bool"
                    else f["v_num"] if f["value_type"] == "num"
                    else f["v_text"] if f["value_type"] == "text"
                    else json.loads(f["v_json"]),
                    "value_type": f["value_type"],
                    "is_derived": bool(f["is_derived"]),
                }
                for f in facts
            ],
            "prices": [dict(p) for p in prices],
            "reasoning_efforts": [
                {"level": e["level"], "encoding": e["encoding"], "is_default": bool(e["is_default"])}
                for e in efforts
            ],
        }
        _emit(payload, True, [])
        return 0

    lines = [f"model    : {row['key']}",
             f"mode     : {row['mode']}" + (f"  (raw: {row['mode_raw']!r})" if row["mode_raw"] else ""),
             f"provider : {row['provider']}"]
    if row["is_doc_entry"]:
        lines.append("           ** documentation entry, not a real model **")
    if row["deprecation_date"]:
        lines.append(f"deprecated: {row['deprecation_date']}")
    if facts:
        lines.append("")
        lines.append("facts (canonical names; resolved across sources by priority):")
        for f in facts:
            val = (
                f["v_bool"] if f["value_type"] == "bool"
                else f["v_num"] if f["value_type"] == "num"
                else f["v_text"] if f["value_type"] == "text"
                else f["v_json"]
            )
            tag = "derived" if f["is_derived"] else f["raw_field"]
            lines.append(f"  {f['field']:<44} {str(val):<28} [{tag}]")
    if efforts:
        lines.append("")
        lines.append("reasoning efforts: " + ", ".join(
            e["level"] + ("*" if e["is_default"] else "") for e in efforts))
    if prices:
        lines.append("")
        lines.append("prices (band_start = token count from which the rate applies):")
        for p in prices:
            band = "" if p["band_start"] is None else f" from {p['band_start']}"
            mod = f" {p['modality']}" if p["modality"] else ""
            tier = f" [{p['tier']}]" if p["tier"] else ""
            ttl = f" ({p['cache_ttl']})" if p["cache_ttl"] else ""
            qual = f"  ~{p['qualifier']}" if p["qualifier"] else ""
            lines.append(
                f"  {p['direction']:<16}{p['unit']:<16}{mod}{tier}{ttl}{band:<14} "
                f"{p['price']:<14.12g} {p['raw_field']}{qual}"
            )
    _emit(None, False, lines)
    return 0


# ---------------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------------


def cmd_search(args):
    conn = Q.connect(args.db, read_only=True)

    sql = ["SELECT m.id, m.key, m.mode, m.provider FROM model m WHERE m.is_doc_entry = 0"]
    params = []

    if args.mode:
        sql.append("AND m.mode = ?")
        params.append(args.mode)
    if args.provider:
        sql.append("AND m.provider = ?")
        params.append(args.provider)

    for flag, field in sorted(CAPABILITY_FLAGS.items()):
        if not getattr(args, flag):
            continue
        # EXISTS ... AND v_bool = 1 deliberately excludes models where the field is
        # ABSENT. Unknown is not true, and is not false either.
        sql.append(
            "AND EXISTS (SELECT 1 FROM resolved_fact f WHERE f.model_id = m.id "
            "AND f.field = ? AND f.v_bool = 1)"
        )
        params.append(field)

    if args.min_context:
        sql.append(
            "AND EXISTS (SELECT 1 FROM resolved_fact f WHERE f.model_id = m.id "
            "AND f.field = 'context_window' AND f.v_num >= ?)"
        )
        params.append(args.min_context)

    sql.append("ORDER BY m.key")
    rows = conn.execute(" ".join(sql), params).fetchall()

    out = []
    for r in rows:
        if args.max_input_price is not None:
            bp = Q.base_price(conn, r["id"], "input")
            if bp is None or bp > args.max_input_price:
                continue
        out.append(r)

    if args.json:
        payload = []
        for r in out:
            facts = Q.model_facts(conn, r["id"])
            payload.append({
                "key": r["key"],
                "mode": r["mode"],
                "provider": r["provider"],
                "facts": {
                    f["field"]: (
                        f["v_bool"] if f["value_type"] == "bool"
                        else f["v_num"] if f["value_type"] == "num"
                        else f["v_text"] if f["value_type"] == "text"
                        else json.loads(f["v_json"])
                    )
                    for f in facts
                },
                "input_cost_per_token": Q.base_price(conn, r["id"], "input"),
            })
        _emit(payload, True, [])
        return 0

    print(f"{len(out)} model(s)")
    for r in out:
        bp = Q.base_price(conn, r["id"], "input")
        ctx = conn.execute(
            "SELECT v_num FROM resolved_fact WHERE model_id = ? AND field = 'context_window'",
            (r["id"],),
        ).fetchone()
        price = f"  in=${bp:.3g}/tok" if bp is not None else ""
        context = f"  ctx={int(ctx['v_num'])}" if ctx and ctx["v_num"] else ""
        print(f"  {r['key']:<52} {str(r['mode'] or '?'):<18}{context}{price}")
    return 0


# ---------------------------------------------------------------------------------
# diff  (reads raw fact, NOT the resolver, so losers are visible)
# ---------------------------------------------------------------------------------


def cmd_diff(args):
    conn = Q.connect(args.db, read_only=True)
    row = Q.resolve_model(conn, args.model)
    if row is None:
        print(f"no such model: {args.model}", file=sys.stderr)
        return 1

    sql = (
        "SELECT f.field, f.raw_field, f.value_type, f.v_bool, f.v_num, f.v_text, f.v_json, "
        "f.is_derived, f.observed_at, s.name AS source, s.priority "
        "FROM fact f JOIN source s ON s.id = f.source_id WHERE f.model_id = ?"
    )
    params = [row["id"]]
    if args.field:
        sql += " AND f.field IN (%s)" % ",".join("?" * len(args.field))
        params.extend(args.field)
    if args.source_a:
        sql += " AND s.name = ?"
        params.append(args.source_a)
    sql += " ORDER BY f.field, s.priority DESC, f.is_derived"

    by_field = {}
    for r in conn.execute(sql, params):
        by_field.setdefault(r["field"], []).append(r)

    disagreements = []
    payload = {}

    for field in sorted(by_field):
        entries = by_field[field]
        rendered = []
        for e in entries:
            val = (
                e["v_bool"] if e["value_type"] == "bool"
                else e["v_num"] if e["value_type"] == "num"
                else e["v_text"] if e["value_type"] == "text"
                else e["v_json"]
            )
            rendered.append((e["source"], e["priority"], val, e["observed_at"], e["raw_field"], e["is_derived"]))
        payload[field] = [
            {"source": s, "priority": p, "value": v, "observed_at": o, "raw_field": rf,
             "is_derived": bool(d)}
            for s, p, v, o, rf, d in rendered
        ]

        distinct = {repr(v) for _s, _p, v, _o, _rf, _d in rendered}
        if len(distinct) > 1:
            disagreements.append(field)

    if args.json:
        _emit({"model": row["key"], "fields": payload, "disagreements": disagreements}, True, [])
        return 1 if disagreements else 0

    print(f"model: {row['key']}")
    for field in sorted(by_field):
        entries = by_field[field]
        distinct = {repr(e[2]) for e in entries}
        mark = "  <-- DISAGREES" if len(distinct) > 1 else ""
        print(f"{field}:{mark}")
        for e in entries:
            val = (
                e["v_bool"] if e["value_type"] == "bool"
                else e["v_num"] if e["value_type"] == "num"
                else e["v_text"] if e["value_type"] == "text"
                else e["v_json"]
            )
            flag = "  (derived)" if e["is_derived"] else ""
            print(f"    {e['source']:<20} = {str(val):<24} prio={e['priority']:<4} "
                  f"{e['observed_at']}  [{e['raw_field']}]{flag}")
    if not by_field:
        print("  (no facts)")
    if disagreements:
        print(f"\n{len(disagreements)} field(s) disagree across sources")
    return 1 if disagreements else 0


# ---------------------------------------------------------------------------------
# verify / sources
# ---------------------------------------------------------------------------------


def cmd_verify(args):
    # The snapshot is deliberately not committed (its content lives in raw_record), so a
    # missing file is the expected first-run experience and deserves a plain message.
    if not os.path.exists(args.source):
        print(
            f"verify needs the source snapshot to check against: {args.source} not found.\n"
            f"Fetch it first, e.g.  python -m modelmeta update --from-url {UPSTREAM_URL}\n"
            "or point at a local copy with --source.",
            file=sys.stderr,
        )
        return 2
    results = run_verify(args.db, args.source, check_determinism=not args.no_determinism)
    width = max(len(r["name"]) for r in results)
    failed = 0
    for r in results:
        status = "ok  " if r["ok"] else "FAIL"
        if not r["ok"]:
            failed += 1
        print(f"[{status}] {r['name']:<{width}}  {r['detail']}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


def cmd_sources(args):
    conn = Q.connect(args.db, read_only=True)
    rows = conn.execute(
        "SELECT s.id, s.name, s.priority, s.kind, "
        "  (SELECT COUNT(*) FROM fact f WHERE f.source_id = s.id) AS facts, "
        "  (SELECT COUNT(*) FROM price p WHERE p.source_id = s.id) AS prices "
        "FROM source s ORDER BY s.priority DESC"
    ).fetchall()
    lines = ["name                 prio  kind           facts   prices"]
    for r in rows:
        lines.append(
            f"{r['name']:<20} {r['priority']:<5} {r['kind']:<14} {r['facts']:<7} {r['prices']}"
        )
    lines.append("")
    lines.append("only 'litellm' has a loader today; the higher-priority sources are")
    lines.append("structural -- `diff` cannot show cross-source disagreement until a")
    lines.append("second loader is written.")
    _emit(None, False, lines)
    return 0


# ---------------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m modelmeta",
        description=f"modelmeta {SCHEMA_VERSION} ({GENERATOR})",
    )
    # --db lives on each read subcommand rather than on the top-level parser, so that
    # `python -m modelmeta verify --db x.db` works. A top-level flag would only be
    # accepted before the subcommand, which nobody types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=Q.DEFAULT_DB, help="database path (default: modelmeta.db)")

    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser("update", help="build the database from a snapshot")
    u.add_argument("--from-file", help="read a local JSON snapshot")
    u.add_argument("--from-url", help=f"fetch a snapshot (default upstream: {UPSTREAM_URL})")
    u.add_argument("--out", default=Q.DEFAULT_DB, help="output database path")
    u.add_argument("--no-raw", action="store_true", help="skip raw_record (smaller, loses round-trip proof)")
    u.add_argument("--observed-at", help="fixed timestamp, for reproducible builds")
    u.add_argument("--strict", action="store_true", help="exit non-zero on unclassified/collision issues")
    u.set_defaults(func=cmd_update)

    g = sub.add_parser("get", help="show one model", parents=[common])
    g.add_argument("model")
    g.add_argument("--field", action="append", help="restrict to a field (repeatable)")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("search", help="find models by capability", parents=[common])
    for flag in sorted(CAPABILITY_FLAGS):
        s.add_argument(f"--{flag.replace('_', '-')}", action="store_true", help=CAPABILITY_FLAGS[flag])
    s.add_argument("--mode")
    s.add_argument("--provider")
    s.add_argument("--min-context", type=int)
    s.add_argument("--max-input-price", type=float)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("diff", help="show per-source values, including losers", parents=[common])
    d.add_argument("model")
    d.add_argument("--field", action="append")
    d.add_argument("--source-a")
    d.add_argument("--source-b")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_diff)

    v = sub.add_parser("verify", help="completeness and consistency checks", parents=[common])
    v.add_argument("--source", default="model_metadata.json", help="snapshot to check against")
    v.add_argument("--no-determinism", action="store_true", help="skip the rebuild check")
    v.set_defaults(func=cmd_verify)

    src = sub.add_parser("sources", help="list sources and their precedence", parents=[common])
    src.set_defaults(func=cmd_sources)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
