"""Command line interface: `python -m modelmeta <command>`.

The unit of truth in this dataset is an *offering*, not a model: the same model served by
two providers carries different limits and prices, and the snapshot records both rather
than picking one. So `search` returns offerings, and `get` shows every offering of a model
side by side.

Tri-state is rendered as `true` / `false` / `unknown` and never collapsed. `--vision`
matches only offerings that say true; an offering that is silent about vision is not
matched. That is the difference between "no" and "nobody said", and it changes the answer
for a large part of this catalogue.
"""

import argparse
import json
import os
import sys

from . import schema as S
from . import query as Q
from .fetch import SnapshotError
from .ingest import build
from .verify import verify as run_verify

try:  # keep unencodable characters from killing output on a legacy console codepage
    sys.stdout.reconfigure(errors="replace")
except Exception:  # pragma: no cover
    pass

# Friendlier spellings for the capability flags that have a natural short name.
FLAG_ALIASES = {
    "tools": "tool_calling",
    "structured": "structured_outputs",
    "caching": "prompt_cache",
    "json-mode": "json_mode",
}
FLAG_NAMES = {}
for _name in sorted(S.CAPABILITY_NAMES):
    FLAG_NAMES[_name.replace("_", "-")] = _name
for _alias, _target in FLAG_ALIASES.items():
    FLAG_NAMES[_alias] = _target


def tri(value):
    return "unknown" if value is None else ("true" if value else "false")


def money(value):
    return "unknown" if value is None else f"${value:g}"


def _emit(payload, as_json, lines):
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        for line in lines:
            print(line)


# --------------------------------------------------------------------------- update


def cmd_update(args):
    from .fetch import read_snapshot

    try:
        doc, file_sha = read_snapshot(args.from_file)
    except SnapshotError as exc:
        print(f"snapshot rejected: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"no such snapshot: {args.from_file}", file=sys.stderr)
        return 2

    stats, issues = build(doc, args.out, origin=args.origin or args.from_file)

    by_severity = {}
    for severity, _scope, _detail in issues:
        by_severity[severity] = by_severity.get(severity, 0) + 1

    print(f"snapshot      : {args.from_file}")
    print(f"file sha256   : {file_sha}")
    print(f"schema_version: {doc.get('schema_version')}   snapshot_at: {doc.get('snapshot_at')}")
    print(f"models        : {stats['model_count']}   offerings: {stats['offering_count']}")
    print(f"capabilities  : {stats['capability']}   prices: {stats['price']}   "
          f"conflicts: {stats['conflict']}")
    print(f"issues        : {len(issues)}" + (f"  {by_severity}" if by_severity else ""))
    for severity, scope, detail in issues[:25]:
        print(f"  [{severity}] {scope}: {detail}")
    if len(issues) > 25:
        print(f"  ... {len(issues) - 25} more (see the ingest_issue table)")

    if args.strict and issues:
        print("strict mode: the loader reported issues", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------------------ get


def cmd_get(args):
    conn = Q.connect(args.db, read_only=True)
    model = Q.resolve_model(conn, args.model)
    if model is None:
        conn.close()
        print(f"no such model: {args.model}", file=sys.stderr)
        print("lookup is exact on catalog_id, canonical_slug then name "
              "(no fuzzy matching -- the snapshot forbids merging by string similarity)",
              file=sys.stderr)
        return 1

    offerings = Q.offerings_for(conn, model["id"])
    conflicts = Q.conflicts_for(conn, model["id"])
    agg = Q.model_capabilities(conn, model["id"])

    if args.json:
        payload = {
            "catalog_id": model["catalog_id"],
            "canonical_slug": model["canonical_slug"],
            "name": model["name"],
            "author": model["author"],
            "identity": {
                "hugging_face_id": model["hugging_face_id"],
                "release_date": model["release_date"],
                "catalog_created_at": model["catalog_created_at"],
                "knowledge_cutoff": model["knowledge_cutoff"],
                "open_weights": model["open_weights"],
                "license": model["license"],
                "parameter_count": model["parameter_count"],
            },
            "capabilities": agg,
            "offerings": [],
            "conflicts": [dict(c) for c in conflicts],
        }
        for off in offerings:
            payload["offerings"].append(_offering_payload(conn, off, args.evidence))
        conn.close()
        _emit(payload, True, [])
        return 0

    L = []
    L.append(f"model    : {model['catalog_id']}")
    L.append(f"name     : {model['name']}   author: {model['author']}")
    if model["canonical_slug"]:
        L.append(f"slug     : {model['canonical_slug']}")
    if model["hugging_face_id"]:
        L.append(f"hf       : {model['hugging_face_id']}")
    L.append("")
    L.append("capabilities (aggregated over offerings; unknown = no offering established it):")
    row = "  "
    for i, name in enumerate(sorted(agg)):
        row += f"{name}={tri(agg[name])}  "
        if (i + 1) % 3 == 0:
            L.append(row.rstrip())
            row = "  "
    if row.strip():
        L.append(row.rstrip())

    for off in offerings:
        L.append("")
        head = (f"offering [{off['authority']}] {off['provider']}"
                f"  source={off['source_id']}")
        if off["provider_model_id"]:
            head += f"  model_id={off['provider_model_id']}"
        L.append(head)
        L.append(f"  availability : {off['availability_status']}"
                 f"   inference_tested={tri(off['inference_tested'])}"
                 + (f"   expires={off['expiration_date']}" if off["expiration_date"] else ""))
        ctx = off["context_tokens"]
        ctx_note = "  <- reported as 0; the snapshot does not say what that means" \
            if ctx == 0 else ""
        L.append(f"  limits       : context={ctx}  max_output={off['max_output_tokens']}"
                 f"  max_input={off['max_input_tokens']}{ctx_note}")
        mods = Q.modalities_for(conn, off["id"])
        L.append(f"  modalities   : in={'+'.join(mods['input']) or '? '}"
                 f"   out={'+'.join(mods['output']) or '?'}")
        if off["tokenizer"]:
            L.append(f"  tokenizer    : {off['tokenizer']}")

        caps = Q.capability_rows(conn, off["id"])
        if caps:
            L.append("  capabilities :")
            for c in caps:
                mark = "" if c["false_is_meaningful"] else "  (false cannot occur)"
                L.append(f"    {c['name']:<22} {tri(c['value']):<8}{mark}")

        reasoning = Q.reasoning_for(conn, off["id"])
        if reasoning:
            p = reasoning["profile"]
            L.append(f"  reasoning    : mandatory={tri(p['mandatory'])}"
                     f" default_enabled={tri(p['default_enabled'])}"
                     f" default={p['default_effort']}")
            L.append(f"    efforts    : {', '.join(reasoning['efforts']) or '(none declared)'}")

        prices = Q.prices_for(conn, off["id"])
        if prices:
            L.append("  prices (USD per MILLION tokens):")
            for p in prices:
                band = f"[{p['time_band']}] " if p["time_band"] else ""
                L.append(f"    {band}{p['kind']:<13} {money(p['per_million_tokens'])}")
        if args.evidence:
            ev = Q.evidence_for(conn, off["id"])
            if ev:
                L.append("  evidence     :")
                for aspect in sorted(ev):
                    L.append(f"    {aspect}: {ev[aspect]}")

    if conflicts:
        L.append("")
        L.append("recorded conflicts (both values retained, not resolved):")
        for c in conflicts:
            L.append(f"  {c['field']}")
            L.append(f"    official   = {c['official_value_json']}"
                     f"   [{c['source_id'] or '?'}]")
            L.append(f"    aggregator = {c['aggregator_value_json']}")
            L.append(f"    resolution = {c['resolution']}")

    conn.close()
    _emit(None, False, L)
    return 0


def _offering_payload(conn, off, include_evidence):
    payload = {
        "authority": off["authority"],
        "provider": off["provider"],
        "provider_model_id": off["provider_model_id"],
        "source_id": off["source_id"],
        "availability": off["availability_status"],
        "inference_tested": off["inference_tested"],
        "account_access_verified": off["account_access_verified"],
        "expiration_date": off["expiration_date"],
        "endpoint_count_observed": off["endpoint_count_observed"],
        "limits": {
            "context_tokens": off["context_tokens"],
            "max_output_tokens": off["max_output_tokens"],
            "max_input_tokens": off["max_input_tokens"],
        },
        "modalities": Q.modalities_for(conn, off["id"]),
        "capabilities": {
            r["name"]: r["value"] for r in Q.capability_rows(conn, off["id"])
        },
        "reasoning": None,
        "prices": [dict(p) for p in Q.prices_for(conn, off["id"])],
        "price_raw": Q.price_raw_for(conn, off["id"]),
        "supported_parameters": Q.supported_parameters_for(conn, off["id"]),
        "voices": Q.voices_for(conn, off["id"]),
        "api_protocols": Q.protocols_for(conn, off["id"]),
        "notes": off["notes"],
    }
    reasoning = Q.reasoning_for(conn, off["id"])
    if reasoning:
        payload["reasoning"] = {
            "mandatory": reasoning["profile"]["mandatory"],
            "default_enabled": reasoning["profile"]["default_enabled"],
            "default_effort": reasoning["profile"]["default_effort"],
            "supported_efforts": reasoning["efforts"],
        }
    if include_evidence:
        payload["capability_evidence"] = Q.evidence_for(conn, off["id"])
    return payload


# --------------------------------------------------------------------------- search


def cmd_search(args):
    conn = Q.connect(args.db, read_only=True)

    sql = [
        "SELECT o.*, m.catalog_id, m.name AS model_name, m.author "
        "FROM offering o JOIN model m ON m.id = o.model_id WHERE 1=1"
    ]
    params = []

    for flag, cap in sorted(FLAG_NAMES.items()):
        positive = getattr(args, flag.replace("-", "_"), False)
        negative = getattr(args, f"no_{flag.replace('-', '_')}", False)
        if positive and negative:
            conn.close()
            print(f"--{flag} and --no-{flag} are mutually exclusive", file=sys.stderr)
            return 2
        if positive:
            # value = 1 only. An offering silent about this capability is NOT matched --
            # unknown is not yes.
            sql.append(
                "AND EXISTS (SELECT 1 FROM capability c WHERE c.offering_id = o.id "
                "AND c.name = ? AND c.value = 1)"
            )
            params.append(cap)
        elif negative:
            # value = 0 only. For parameter-inferred capabilities this always matches
            # nothing, because upstream cannot report false for them -- which is the
            # honest answer, not a bug.
            sql.append(
                "AND EXISTS (SELECT 1 FROM capability c WHERE c.offering_id = o.id "
                "AND c.name = ? AND c.value = 0)"
            )
            params.append(cap)

    if args.text_only:
        for direction in ("input", "output"):
            sql.append(
                "AND EXISTS (SELECT 1 FROM modality md WHERE md.offering_id = o.id "
                "AND md.direction = ? AND md.modality = 'text')"
            )
            params.append(direction)

    if args.min_context is not None:
        sql.append("AND o.context_tokens >= ?")
        params.append(args.min_context)
    if args.author:
        sql.append("AND m.author = ?")
        params.append(args.author)
    if args.provider:
        sql.append("AND o.provider = ?")
        params.append(args.provider)
    if args.status:
        sql.append("AND o.availability_status = ?")
        params.append(args.status)
    if args.expiring_before:
        sql.append("AND o.expiration_date IS NOT NULL AND o.expiration_date <= ?")
        params.append(args.expiring_before)
    if args.max_input_price is not None:
        sql.append(
            "AND (SELECT MIN(p.per_million_tokens) FROM price p WHERE p.offering_id = o.id "
            "AND p.kind = 'input' AND p.per_million_tokens IS NOT NULL) <= ?"
        )
        params.append(args.max_input_price)

    sql.append("ORDER BY m.catalog_id, o.authority, o.provider")
    rows = conn.execute(" ".join(sql), params).fetchall()

    out = []
    for r in rows:
        cheapest = conn.execute(
            "SELECT MIN(per_million_tokens) FROM price WHERE offering_id = ? AND kind='input'",
            (r["id"],),
        ).fetchone()[0]
        out.append((r, cheapest))

    if args.json:
        payload = [
            {
                "catalog_id": r["catalog_id"],
                "author": r["author"],
                "authority": r["authority"],
                "provider": r["provider"],
                "availability": r["availability_status"],
                "expiration_date": r["expiration_date"],
                "context_tokens": r["context_tokens"],
                "max_output_tokens": r["max_output_tokens"],
                "cheapest_input_per_million": cheapest,
                "capabilities": {
                    c["name"]: c["value"] for c in Q.capability_rows(conn, r["id"])
                },
            }
            for r, cheapest in out
        ]
        conn.close()
        _emit(payload, True, [])
        return 0

    print(f"{len(out)} offering(s)")
    if not out:
        print("  (empty is a valid answer here: an unknown capability does not match a "
              "positive filter, and no capability can ever be false for the "
              "parameter-inferred ones)")
    for r, cheapest in out:
        ctx = r["context_tokens"]
        ctxs = "ctx=0" if ctx == 0 else (f"ctx={ctx}" if ctx is not None else "ctx=?")
        price = f"in>={money(cheapest)}/M" if cheapest is not None else "in=?"
        expiry = f"  expires={r['expiration_date']}" if r["expiration_date"] else ""
        print(f"  {r['catalog_id']:<44} {r['authority']:<8} {r['provider']:<16}"
              f" {ctxs:<12} {price}{expiry}")
    conn.close()
    return 0


# ----------------------------------------------------------------------------- diff


def cmd_diff(args):
    conn = Q.connect(args.db, read_only=True)
    model = Q.resolve_model(conn, args.model)
    if model is None:
        conn.close()
        print(f"no such model: {args.model}", file=sys.stderr)
        return 1

    offerings = Q.offerings_for(conn, model["id"])
    conflicts = Q.conflicts_for(conn, model["id"])

    fields = ["context_tokens", "max_output_tokens", "max_input_tokens",
              "availability_status", "expiration_date"]
    payload = {"catalog_id": model["catalog_id"], "offerings": {}, "conflicts": []}
    lines = [f"model: {model['catalog_id']}",
             f"  {'field':<22}" + "".join(f"{o['authority'] + '/' + o['provider']:<26}"
                                          for o in offerings)]

    for field in fields:
        values = [o[field] for o in offerings]
        payload["offerings"][field] = [
            {"authority": o["authority"], "provider": o["provider"], "value": o[field]}
            for o in offerings
        ]
        differ = len({str(v) for v in values}) > 1
        lines.append(
            f"  {field:<22}"
            + "".join(f"{str(v):<26}" for v in values)
            + ("   <-- differs" if differ else "")
        )

    cap_names = sorted(S.CAPABILITY_NAMES)
    for name in cap_names:
        values = []
        for off in offerings:
            row = conn.execute(
                "SELECT value FROM capability WHERE offering_id = ? AND name = ?",
                (off["id"], name),
            ).fetchone()
            values.append(row["value"] if row else None)
        if len({str(v) for v in values}) > 1:
            lines.append(
                f"  {name:<22}" + "".join(f"{tri(v):<26}" for v in values)
                + "   <-- differs"
            )

    if conflicts:
        lines.append("")
        lines.append("recorded conflicts:")
        for c in conflicts:
            lines.append(f"  {c['field']}: official={c['official_value_json']} "
                         f"aggregator={c['aggregator_value_json']} "
                         f"({c['resolution']})")
            payload["conflicts"].append(dict(c))

    if args.json:
        conn.close()
        _emit(payload, True, [])
        return 0
    if len(offerings) < 2:
        lines.append("")
        lines.append("  (only one offering; nothing to differ against)")
    conn.close()
    _emit(None, False, lines)
    return 0


# ----------------------------------------------------------------- verify / sources


def cmd_verify(args):
    if not os.path.exists(args.snapshot):
        print(f"verify needs the snapshot to check against: {args.snapshot} not found.",
              file=sys.stderr)
        return 2
    results = run_verify(args.db, args.snapshot, check_determinism=not args.no_determinism)
    width = max(len(r["name"]) for r in results)
    failed = 0
    for r in results:
        if not r["ok"]:
            failed += 1
        print(f"[{'ok  ' if r['ok'] else 'FAIL'}] {r['name']:<{width}}  {r['detail']}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


def cmd_sources(args):
    conn = Q.connect(args.db, read_only=True)
    rows = conn.execute(
        "SELECT s.id, s.kind, s.url, s.live_http_status, "
        "  (SELECT COUNT(*) FROM offering o WHERE o.source_id = s.id) AS offerings "
        "FROM source s ORDER BY s.kind, s.id"
    ).fetchall()
    lines = [f"{'source':<24} {'kind':<32} {'probed':<8} {'offerings':<9} url"]
    for r in rows:
        probed = "-" if r["live_http_status"] is None else str(r["live_http_status"])
        lines.append(
            f"{r['id']:<24} {r['kind']:<32} {probed:<8} {r['offerings']:<9} {r['url'] or ''}"
        )
    lines.append("")
    lines.append("probed '-' means the source was never HTTP-checked -- that is not a")
    lines.append("failure, and live_http_status is deliberately NULL rather than 0 or 200.")
    conn.close()
    _emit(None, False, lines)
    return 0


def cmd_rules(args):
    conn = Q.connect(args.db, read_only=True)
    lines = ["the snapshot's own normalization contract, quoted from the source:"]
    for r in Q.normalization_rules(conn):
        lines.append("")
        lines.append(f"  {r['name']}")
        lines.append(f"    {r['text']}")
    conn.close()
    _emit(None, False, lines)
    return 0


# ---------------------------------------------------------------------------- parser


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m modelmeta",
        description=f"modelmeta {S.SCHEMA_VERSION} ({S.GENERATOR})",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=Q.DEFAULT_DB, help="database path")

    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser("update", help="build the database from a snapshot")
    u.add_argument("--from-file", required=True, help="path to the curated snapshot JSON")
    u.add_argument("--out", default=Q.DEFAULT_DB, help="output database path")
    u.add_argument("--origin", help="recorded origin label (defaults to the file path)")
    u.add_argument("--strict", action="store_true", help="exit non-zero if any issue is reported")
    u.set_defaults(func=cmd_update)

    g = sub.add_parser("get", help="show one model and all of its offerings", parents=[common])
    g.add_argument("model", help="catalog_id, canonical_slug or name (exact match)")
    g.add_argument("--evidence", action="store_true", help="include capability evidence text")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("search", help="find offerings by capability", parents=[common])
    for flag, cap in sorted(FLAG_NAMES.items()):
        s.add_argument(f"--{flag}", action="store_true",
                       help=f"offering says true for {cap}")
        s.add_argument(f"--no-{flag}", action="store_true",
                       help=f"offering says false for {cap} (always empty for "
                            f"parameter-inferred capabilities)")
    s.add_argument("--text-only", action="store_true",
                   help="input and output modalities both include text")
    s.add_argument("--min-context", type=int)
    s.add_argument("--max-input-price", type=float,
                   help="cheapest advertised input price per million tokens, off-peak included")
    s.add_argument("--author")
    s.add_argument("--provider")
    s.add_argument("--status", help="availability status, e.g. listed or retired")
    s.add_argument("--expiring-before", metavar="YYYY-MM-DD")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("diff", help="compare a model's offerings side by side", parents=[common])
    d.add_argument("model")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_diff)

    v = sub.add_parser("verify", help="completeness and consistency checks", parents=[common])
    v.add_argument("--snapshot", default="snapshot/model_metainf_2026-10-03.json")
    v.add_argument("--no-determinism", action="store_true")
    v.set_defaults(func=cmd_verify)

    src = sub.add_parser("sources", help="list data sources", parents=[common])
    src.set_defaults(func=cmd_sources)

    r = sub.add_parser("rules", help="show the snapshot's normalization contract", parents=[common])
    r.set_defaults(func=cmd_rules)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)
