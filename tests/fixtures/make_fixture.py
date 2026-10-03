"""Regenerate the test fixture from the real snapshot.

Run from the repo root:  python tests/fixtures/make_fixture.py

The fixture is a hand-picked excerpt, not a random sample. Each model was chosen because
it exercises one shape that a naive loader would get wrong. If you change the loader,
check these still hold:

  deepseek/deepseek-v4.1-flash   the recorded conflict (official 393216 vs aggregator
                                 943718 max_output_tokens); a tiered official offering
                                 (peak/off_peak); AND the effort-reordering case -- its
                                 two offerings list the same set as ['max','high','low']
                                 and ['low','high','max'].
  respan/span-01-lite            context_tokens == 0 (73 such offerings upstream). Must be
                                 stored as 0, not nulled.
  google/gemini-2.5-flash        an official offering with a null model_id and only four
                                 keys -- a documentation stub, no limits/capabilities/pricing.
  z-ai/glm-5.3-flash             an offering whose pricing is null.
  moonshotai/kimi-k2.7-code-highspeed  a model with no offerings at all.
  openai/gpt-audio-mini          audio_output == true, so at least one modality capability
                                 is true rather than only false.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SOURCE = os.path.join(REPO, "snapshot", "model_metainf_2026-10-03.json")

SELECTED = [
    "deepseek/deepseek-v4.1-flash",
    "respan/span-01-lite",
    "google/gemini-2.5-flash",
    "z-ai/glm-5.3-flash",
    "moonshotai/kimi-k2.7-code-highspeed",
    "openai/gpt-audio-mini",
]


def main():
    doc = json.load(open(SOURCE, encoding="utf-8"))
    by_id = {m["catalog_id"]: m for m in doc["models"]}
    models = [by_id[cid] for cid in SELECTED]

    # Keep only the sources and namespaces the excerpt actually references, so the fixture
    # stays self-contained and its foreign keys resolve.
    referenced = set()
    for m in models:
        for group in ("offerings", "official_offerings"):
            for off in m.get(group) or []:
                referenced.add(off.get("source_id"))
                referenced.update(off.get("additional_source_ids") or [])
                pricing = off.get("pricing")
                if isinstance(pricing, dict) and pricing.get("source_id"):
                    referenced.add(pricing["source_id"])
    sources = {k: v for k, v in doc["sources"].items() if k in referenced}

    namespaces = {
        k: v
        for k, v in doc["provider_namespace_counts"].items()
        if k in {m["author"] for m in models}
        or k in {off["provider"] for m in models for g in ("offerings", "official_offerings")
                 for off in (m.get(g) or [])}
    }

    mini = {
        "schema_version": doc["schema_version"],
        "snapshot_at": doc["snapshot_at"],
        "as_of_date": doc["as_of_date"],
        "scope": doc["scope"],
        "normalization_rules": doc["normalization_rules"],
        # Counts are recomputed for the excerpt, so verify's cross-check still means
        # something rather than being disabled.
        "statistics": {
            "model_records": len(models),
            "aggregator_catalog_records": sum(len(m.get("offerings") or []) for m in models),
            "officially_cross_checked_records": sum(
                1 for m in models if m.get("official_offerings")
            ),
            "inference_calls_performed": 0,
        },
        "sources": sources,
        "provider_namespace_counts": namespaces,
        "models": models,
        "endpoint_discovery_checks": [
            c
            for c in doc.get("endpoint_discovery_checks") or []
            if c.get("model_id") in SELECTED
        ],
    }

    out_utf8 = os.path.join(HERE, "snapshot_mini.json")
    with open(out_utf8, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(mini, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    # The UTF-16LE + CRLF variant exists so the encoding-sniffing path stays covered. The
    # previous dataset in this repo really was UTF-16, and a wrong guess mangles non-ASCII
    # model names instead of raising.
    out_utf16 = os.path.join(HERE, "snapshot_mini_utf16.json")
    text = json.dumps(mini, indent=2, sort_keys=True, ensure_ascii=False)
    with open(out_utf16, "wb") as fh:
        fh.write(text.replace("\n", "\r\n").encode("utf-16"))

    print(f"wrote {out_utf8} and {out_utf16}")
    print(f"models={len(models)} offerings="
          f"{sum(len(m.get('offerings') or []) + len(m.get('official_offerings') or []) for m in models)}")


if __name__ == "__main__":
    main()
