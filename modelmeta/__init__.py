"""modelmeta -- normalise LiteLLM model metadata into a queryable SQLite registry."""

__version__ = "1.0.0"

SCHEMA_VERSION = 1
GENERATOR = f"modelmeta/{__version__}"

# Seeded with fixed ids so a rebuild is byte-identical.
SOURCES = [
    # (id, name, priority, kind)
    (1, "litellm", 40, "aggregator"),
    (2, "openrouter", 60, "aggregator"),
    (3, "provider_official", 80, "authoritative"),
    (4, "local_override", 100, "override"),
]
SOURCE_ID = {name: sid for sid, name, _p, _k in SOURCES}

# Records that are documentation rather than models: their values are literal
# placeholder strings, e.g. sample_spec.deprecation_date ==
# "date when the model becomes deprecated in the format YYYY-MM-DD".
# They still get model rows (flagged) so "4460 in -> 4460 accounted for" is provable.
DOC_ENTRIES = {"sample_spec", "fallback_generalizations"}
