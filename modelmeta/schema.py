"""The capability taxonomy, as data.

This registry exists because this dataset has **two structurally different kinds of
tri-state** in the same `capabilities` object, and flattening them is the single easiest
way to get a wrong answer out of it:

  * Modality-declared capabilities (``vision``, ``audio_input``, ...) are only ever
    ``true`` or ``false`` upstream. Here ``false`` is a real observation: the offering's
    declared modality list simply does not include it.

  * Parameter-inferred capabilities (``tool_calling``, ``json_mode``, ...) are only ever
    ``true`` or ``null`` -- ``false`` never occurs, because the value is inferred from the
    presence of a supported parameter, and absence of evidence is not evidence of absence.
    A consumer that reads ``null`` as "false" is wrong about a third of the catalogue.

Two capabilities (``parallel_tool_calls``, ``streaming``) are null for every single
offering: the column exists but was never established.

Keeping this in Python *and* in the ``capability_def`` table is deliberate duplication, so
that a query can explain its own semantics without consulting the source code. A test
asserts the two stay identical.
"""

SCHEMA_VERSION = 2
GENERATOR = "modelmeta/2.0.0"

# (name, domain, evidence_aspect, false_is_meaningful)
CAPABILITIES = [
    # Declared modality lists. false is meaningful: the list was explicit.
    ("vision", "modality", "modalities", 1),
    ("audio_input", "modality", "modalities", 1),
    ("video_input", "modality", "modalities", 1),
    ("image_output", "modality", "modalities", 1),
    ("audio_output", "modality", "modalities", 1),
    ("embeddings", "modality", "modalities", 1),
    # Inferred from supported_parameters. false never occurs: absent => null, not false.
    ("tool_calling", "parameter", "parameter_capabilities", 0),
    ("structured_outputs", "parameter", "parameter_capabilities", 0),
    ("json_mode", "parameter", "parameter_capabilities", 0),
    ("temperature", "parameter", "parameter_capabilities", 0),
    ("reasoning", "parameter", "reasoning", 0),
    ("prompt_cache", "parameter", "prompt_cache", 0),
    # Present in the schema, never established for any offering.
    ("parallel_tool_calls", "operational", "parameter_capabilities", 0),
    ("streaming", "operational", "parameter_capabilities", 0),
]

# Capabilities for which upstream may legitimately report false.
FALSE_IS_MEANINGFUL = frozenset(name for name, _d, _e, f in CAPABILITIES if f == 1)
FALSE_IS_IMPOSSIBLE = frozenset(name for name, _d, _e, f in CAPABILITIES if f == 0)
CAPABILITY_NAMES = frozenset(name for name, _d, _e, _f in CAPABILITIES)
EVIDENCE_ASPECTS = frozenset(aspect for _n, _d, aspect, _f in CAPABILITIES)

# Availability vocabulary. Validated in Python rather than by a SQL CHECK, so an upstream
# addition is a logged issue instead of a hard build failure.
AVAILABILITY_STATUSES = frozenset(
    {
        "listed",
        "documented_available",
        "existing_users_only",
        "retired",
        "plan_restricted",
    }
)

PRICE_KINDS = {
    "input": "input_per_million_tokens",
    "output": "output_per_million_tokens",
    "cache_read": "cache_read_per_million_tokens",
    "cache_write": "cache_write_per_million_tokens",
}

# Top-level document keys this loader understands. Anything else is reported, not ignored.
KNOWN_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "snapshot_at",
        "as_of_date",
        "scope",
        "normalization_rules",
        "statistics",
        "sources",
        "provider_namespace_counts",
        "models",
        "endpoint_discovery_checks",
    }
)
KNOWN_MODEL_KEYS = frozenset(
    {
        "catalog_id",
        "canonical_slug",
        "name",
        "author",
        "selection",
        "model_identity",
        "offerings",
        "official_offerings",
        "conflicts",
    }
)
KNOWN_OFFERING_KEYS = frozenset(
    {
        "provider",
        "model_id",
        "source_id",
        "availability",
        "limits",
        "modalities",
        "capabilities",
        "capability_evidence",
        "reasoning",
        "pricing",
        "supported_parameters",
        "default_parameters",
        "supported_voices",
        "tokenizer",
        # official-only
        "notes",
        "knowledge_cutoff",
        "additional_source_ids",
        "api_protocols",
    }
)


def capability_rows():
    """Rows for the capability_def table, in a stable order."""
    return [
        (name, domain, aspect, false_ok)
        for name, domain, aspect, false_ok in CAPABILITIES
    ]
