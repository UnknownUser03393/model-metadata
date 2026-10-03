"""Field-name grammar and value rules. This is where the source's irregularities are
absorbed, so it carries the highest bug density in the project.

Three irregularities drive the design:

  * Pricing field names are not uniform. Most are ``<qualifier>_cost_per_<unit>`` but
    cache pricing uses ``<qualifier>_token_cost``, and markers stack in either order
    (``..._token_cost_above_1hr_above_200k_tokens``) and may carry decimals and capital
    K (``output_cost_per_image_0.5K``).

  * ``max_input_tokens`` / ``max_output_tokens`` / ``max_tokens`` overlap in ways that
    make ``max_tokens`` genuinely ambiguous (see ``normalize_limits``).

  * reasoning effort is encoded two mutually exclusive ways: discrete booleans
    (``supports_xhigh_reasoning_effort``) or a list (``reasoning_effort_levels``).
"""

import json
import re

# --------------------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------------------


def classify(value):
    """Map a JSON value onto the typed columns of ``fact``.

    Returns (value_type, v_bool, v_num, v_text, v_json).

    ``bool`` MUST be tested before ``int``: in Python ``bool`` is a subclass of ``int``,
    so the reverse order would file ``supports_vision`` as a number and silently empty
    out every ``v_bool`` query.
    """
    if isinstance(value, bool):
        return "bool", (1 if value else 0), None, None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "num", None, float(value), None, None
    if isinstance(value, str):
        return "text", None, None, value, None
    return "json", None, None, None, json.dumps(value, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------------------
# mode
# --------------------------------------------------------------------------------------

MODES = frozenset(
    {
        "chat",
        "completion",
        "embedding",
        "rerank",
        "image_generation",
        "image_edit",
        "audio_transcription",
        "audio_speech",
        "realtime",
        "video_generation",
        "search",
        "ocr",
        "evaluation",
        "moderation",
        "guardrail",
        "vector_store",
        "responses",
    }
)

# Modes where `max_tokens` denotes input capacity rather than output length.
NON_GENERATIVE_MODES = frozenset({"embedding", "rerank", "ocr", "search", "vector_store"})


def coerce_mode(raw):
    """Validate against the vocabulary in Python rather than with a SQL CHECK.

    A CHECK would make every upstream mode addition (guardrail and vector_store are
    recent) a hard ingest failure; the schema has to outlive the vocabulary.

    Returns (mode_or_None, issue_detail_or_None). A missing mode is legitimate and
    produces no issue; an unrecognised one does.
    """
    if raw is None:
        return None, None
    if isinstance(raw, str) and raw in MODES:
        return raw, None
    return None, f"mode={raw!r} not in vocabulary -> NULL (mode_raw retained)"


# --------------------------------------------------------------------------------------
# Token limits
# --------------------------------------------------------------------------------------

LIMIT_RAW_FIELDS = ("max_input_tokens", "max_output_tokens", "max_tokens")


def normalize_limits(record, mode):
    """Derive canonical capacity facts from the three overlapping limit fields.

    Returns (derived, issues) where derived is a list of
    (field, raw_field, value_type, v_bool, v_num, v_text, v_json) for is_derived=1 rows.

    ``max_tokens`` is ambiguous: for a chat model it means max output, but for an
    embedding/rerank/ocr model it is the input capacity. It is only used as the output
    limit for generative modes.
    """
    derived = []
    issues = []

    max_in = record.get("max_input_tokens")
    max_out = record.get("max_output_tokens")
    max_tok = record.get("max_tokens")

    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    max_in, max_out, max_tok = num(max_in), num(max_out), num(max_tok)

    if max_in is not None:
        derived.append(("context_window", "max_input_tokens", "num", None, float(max_in), None, None))
    elif max_tok is not None and mode in NON_GENERATIVE_MODES:
        # For these modes max_tokens IS the context window.
        derived.append(("context_window", "max_tokens", "num", None, float(max_tok), None, None))

    if max_out is not None:
        # The raw max_output_tokens row already carries field == 'max_output_tokens',
        # so no derived row is needed; only report a disagreement.
        if max_tok is not None and max_tok != max_out:
            issues.append(
                (
                    "collision",
                    "max_tokens",
                    f"max_tokens={max_tok:g} != max_output_tokens={max_out:g}; kept max_output_tokens",
                )
            )
    elif max_tok is not None and mode not in NON_GENERATIVE_MODES:
        derived.append(("max_output_tokens", "max_tokens", "num", None, float(max_tok), None, None))

    return derived, issues


# --------------------------------------------------------------------------------------
# reasoning effort
# --------------------------------------------------------------------------------------

# Only these five exist upstream. There is no supports_medium_reasoning_effort or
# supports_high_reasoning_effort -- do not add speculative entries here.
EFFORT_BOOL_FIELDS = {
    "supports_none_reasoning_effort": "none",
    "supports_minimal_reasoning_effort": "minimal",
    "supports_low_reasoning_effort": "low",
    "supports_max_reasoning_effort": "max",
    "supports_xhigh_reasoning_effort": "xhigh",
}
EFFORT_LIST_FIELD = "reasoning_effort_levels"
EFFORT_DEFAULT_FIELD = "default_reasoning_effort"
EFFORT_FIELDS = frozenset(EFFORT_BOOL_FIELDS) | {EFFORT_LIST_FIELD, EFFORT_DEFAULT_FIELD}


def normalize_efforts(record):
    """Merge both encodings into (level, raw_field, encoding, is_default) tuples.

    A boolean only contributes when it is exactly ``True`` -- ``False`` means the level
    is unsupported, which is represented by the level's absence, not by a row.
    """
    default = record.get(EFFORT_DEFAULT_FIELD)
    out = []

    for raw_field, level in sorted(EFFORT_BOOL_FIELDS.items()):
        if record.get(raw_field) is True:
            out.append((level, raw_field, "boolean", 1 if level == default else 0))

    levels = record.get(EFFORT_LIST_FIELD)
    if isinstance(levels, list):
        for level in levels:
            if isinstance(level, str):
                out.append((level, EFFORT_LIST_FIELD, "list", 1 if level == default else 0))

    return out


# --------------------------------------------------------------------------------------
# Pricing field names
# --------------------------------------------------------------------------------------

# Contains "cost" or "price" -> expected to land in price/price_schedule. Counted by
# the accounting check in cli verify.
def is_price_named(field):
    return "cost" in field or "price" in field


# Cannot be flattened into price rows: a list of disjoint token-range bands, and a
# time-of-day schedule. Stored as JSON.
PRICE_SCHEDULE_FIELDS = frozenset({"tiered_pricing", "off_peak_pricing"})

TIERS = ("batches", "priority", "flex", "ultrafast", "balanced")

UNIT_TOKENS = {
    "token": "per_token",
    "tokens": "per_token",
    "second": "per_second",
    "page": "per_page",
    "image": "per_image",
    "pixel": "per_pixel",
    "character": "per_character",
    "query": "per_query",
    "request": "per_request",
    "session": "per_session",
    "credit": "per_credit",
    "unit": "per_unit",
    "call": "per_call",
    "calls": "per_call",
}

MODALITY_TOKENS = ("audio", "image", "video", "text")

# Ordered most-specific-first. 'input'/'output' must come after the compound directions
# so that cache_read_input resolves to cache_read, while input_dbu resolves to input.
DIRECTIONS = (
    "cache_read",
    "cache_creation",
    "code_interpreter",
    "file_search",
    "vector_store",
    "computer_use",
    "google_maps_grounding",
    "search_context",
    "web_search",
    "annotation",
    "guardrail",
    "citation",
    "reasoning",
    "input",
    "output",
    "dbu",
    "search",
    "ocr",
)

_ABOVE_TOKENS_RE = re.compile(r"_above_(\d+)k_tokens$")
_ABOVE_SECONDS_RE = re.compile(r"_above_(\d+)s_interval$")
_ABOVE_HR_RE = re.compile(r"_above_(\d+hr)$")
# The trailing group is unconstrained because tier markers attach directly to it:
# cache_read_input_token_cost_batches, cache_creation_input_token_cost_priority, ...
_TOKEN_COST_RE = re.compile(r"^(?P<left>.+?)_token_cost(?P<rest>.*)$")


def _strip_suffix_markers(s):
    """Repeatedly peel band / cache-ttl / tier markers off the end of ``s``.

    A loop rather than three ordered regexes because the markers stack in either order:
    ``..._token_cost_above_1hr_above_200k_tokens`` and ``..._token_cost_above_200k_tokens_batches``
    are both real.
    """
    info = {"band_start": None, "cache_ttl": None, "tier": None, "extra": []}
    while True:
        m = _ABOVE_TOKENS_RE.search(s)
        if m:
            info["band_start"] = int(m.group(1)) * 1000
            s = s[: m.start()].rstrip("_")
            continue
        m = _ABOVE_SECONDS_RE.search(s)
        if m:
            info["extra"].append(f"above_{m.group(1)}s_interval")
            s = s[: m.start()].rstrip("_")
            continue
        m = _ABOVE_HR_RE.search(s)
        if m:
            info["cache_ttl"] = m.group(1)
            s = s[: m.start()].rstrip("_")
            continue
        tier = next((t for t in TIERS if s.endswith("_" + t)), None)
        if tier:
            info["tier"] = tier
            s = s[: -(len(tier) + 1)].rstrip("_")
            continue
        break
    return s, info


def _parse_unit_expr(s):
    """Parse the unit expression into (unit, modality, qualifiers).

    'audio' and 'image' are both modality words and unit words. A bare modality with no
    explicit unit promotes to that unit: ``cost_per_image`` is per_image, but
    ``cost_per_image_token`` is per_token with modality image.
    """
    qualifiers = []
    if s == "gb_per_day":
        return "per_gb_per_day", None, qualifiers
    if s.startswith("1k_"):
        qualifiers.append("scaled_1k")
        s = s[3:]

    unit = None
    modality = None
    for token in (t for t in s.split("_") if t):
        low = token.lower()
        if modality is None and unit is None and low in MODALITY_TOKENS:
            modality = low
            continue
        if unit is None and low in UNIT_TOKENS:
            unit = UNIT_TOKENS[low]
            continue
        if low == "per":
            continue
        qualifiers.append(token)

    if unit is None and modality is not None:
        unit, modality = "per_" + modality, None

    return unit, modality, qualifiers


def _parse_direction(left):
    """-> (direction_or_None, qualifier_tokens)"""
    if not left:
        return "unspecified", []
    for d in DIRECTIONS:
        if left == d:
            return d, []
        if left.startswith(d + "_"):
            return d, [left[len(d) + 1 :]]
        if left.endswith("_" + d):
            return d, [left[: -(len(d) + 1)]]
    return None, [left]


def parse_price_name(raw_field):
    """Parse a pricing field name into its dimensions.

    Returns a dict with keys direction/unit/modality/tier/cache_ttl/band_start/qualifier,
    or ``None`` when no unit can be determined. ``None`` means "not a price row" -- the
    caller stores it in ``fact`` and records an ``unclassified`` issue. It never means
    the value was dropped.
    """
    s = raw_field
    forced_unit = None

    m = _TOKEN_COST_RE.match(s)
    if m:
        # cache_read_input_token_cost / cache_creation_input_audio_token_cost.
        # These end in `_token_cost` rather than containing `cost_per_`.
        left, rest, forced_unit = m.group("left"), m.group("rest"), "per_token"
    else:
        idx = s.find("cost_per_")
        if idx < 0:
            return None
        left = s[:idx].rstrip("_")
        rest = s[idx + len("cost_per_") :]

    direction, left_quals = _parse_direction(left)
    if direction is None:
        return None

    rest, info = _strip_suffix_markers(rest)
    unit, modality, unit_quals = _parse_unit_expr(rest)
    if unit is None:
        unit, modality, unit_quals = forced_unit, None, unit_quals
    if unit is None:
        return None

    qualifiers = list(left_quals) + list(info["extra"]) + list(unit_quals)

    # A modality may also appear in the qualifier tokens (e.g. cache_read_input_audio).
    if modality is None:
        for q in qualifiers:
            for token in q.split("_"):
                if token in MODALITY_TOKENS:
                    modality = token
                    break
            if modality:
                break

    return {
        "direction": direction,
        "unit": unit,
        "modality": modality,
        "tier": info["tier"],
        "cache_ttl": info["cache_ttl"],
        "band_start": info["band_start"],
        "qualifier": "_".join(q for q in qualifiers if q) or None,
    }


# --------------------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------------------


def normalize_field_name(field):
    """Canonical fact field name for a capability/other field.

    Identity for now. Capability names are NOT rewritten to friendlier aliases:
    mapping supports_function_calling + supports_tool_choice onto a single
    'supports_tools' would invent OR-semantics the source never stated. The CLI expands
    its flags to the real underlying fields instead, so no judgement is fabricated here.
    """
    return field


def provider_of(record):
    p = record.get("litellm_provider")
    return p if isinstance(p, str) else None


# Field routing, defined ONCE and consumed by both ingest and the verify accounting.
# Duplicating this logic is what makes completeness checks disagree with the loader for
# reasons nobody can debug.
ROUTE_PRICE = "price"
ROUTE_SCHEDULE = "schedule"
ROUTE_EFFORT = "effort"
ROUTE_FACT = "fact"


def route_field(field, value):
    """Where a source field lands. Returns one of ROUTE_*.

    A price-named field the grammar cannot classify routes to ROUTE_FACT: it stays
    queryable as a number and is additionally logged as 'unclassified'.
    """
    if field in PRICE_SCHEDULE_FIELDS:
        return ROUTE_SCHEDULE
    if field in EFFORT_FIELDS:
        return ROUTE_EFFORT
    if is_price_named(field):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # Non-numeric but price-named: search_context_cost_per_query is a list of
            # per-provider costs and cannot be flattened into price rows.
            return ROUTE_SCHEDULE
        if parse_price_name(field) is None:
            return ROUTE_FACT
        return ROUTE_PRICE
    return ROUTE_FACT
