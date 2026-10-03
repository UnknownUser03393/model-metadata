import unittest

from modelmeta import normalize as N


class TestClassify(unittest.TestCase):
    def test_bool_is_not_filed_as_a_number(self):
        # bool subclasses int in Python; testing int first would put supports_vision
        # into v_num and silently empty every v_bool query.
        kind, vb, vn, vt, vj = N.classify(True)
        self.assertEqual(kind, "bool")
        self.assertEqual(vb, 1)
        self.assertIsNone(vn)

        kind, vb, _vn, _vt, _vj = N.classify(False)
        self.assertEqual(kind, "bool")
        self.assertEqual(vb, 0)

    def test_numbers_strings_and_containers(self):
        self.assertEqual(N.classify(1.5)[0], "num")
        self.assertEqual(N.classify(0)[0], "num")
        self.assertEqual(N.classify("x")[0], "text")
        self.assertEqual(N.classify([1, 2])[0], "json")
        self.assertEqual(N.classify({"a": 1})[0], "json")
        self.assertEqual(N.classify(None)[0], "json")

    def test_json_is_key_sorted_for_determinism(self):
        self.assertEqual(N.classify({"b": 1, "a": 2})[4], '{"a":2,"b":1}')


class TestMode(unittest.TestCase):
    def test_known_mode_passes(self):
        self.assertEqual(N.coerce_mode("chat"), ("chat", None))

    def test_missing_mode_is_not_an_issue(self):
        self.assertEqual(N.coerce_mode(None), (None, None))

    def test_dirty_mode_is_nulled_and_reported(self):
        mode, issue = N.coerce_mode("one of: chat, embedding")
        self.assertIsNone(mode)
        self.assertIn("not in vocabulary", issue)


class TestLimits(unittest.TestCase):
    def test_context_window_from_max_input_tokens(self):
        derived, issues = N.normalize_limits({"max_input_tokens": 128000, "max_tokens": 4096}, "chat")
        self.assertIn(("context_window", "max_input_tokens", "num", None, 128000.0, None, None), derived)
        self.assertEqual(issues, [])

    def test_max_tokens_used_as_output_for_generative_modes(self):
        derived, _ = N.normalize_limits({"max_tokens": 4096}, "chat")
        self.assertTrue(any(d[0] == "max_output_tokens" and d[1] == "max_tokens" for d in derived))

    def test_max_tokens_is_context_for_non_generative_modes(self):
        # An embedding model's max_tokens is its input capacity, not an output length.
        derived, _ = N.normalize_limits({"max_tokens": 8192}, "embedding")
        self.assertEqual([d[0] for d in derived], ["context_window"])

    def test_disagreement_is_reported_not_silently_dropped(self):
        derived, issues = N.normalize_limits(
            {"max_output_tokens": 4096, "max_tokens": 8192}, "chat"
        )
        self.assertEqual(issues[0][0], "collision")
        self.assertFalse(any(d[0] == "max_output_tokens" for d in derived))

    def test_absent_limits_derive_nothing(self):
        self.assertEqual(N.normalize_limits({}, "chat"), ([], []))


class TestEffort(unittest.TestCase):
    def test_boolean_encoding_only_true_contributes(self):
        got = N.normalize_efforts({"supports_xhigh_reasoning_effort": True,
                                   "supports_low_reasoning_effort": False})
        self.assertEqual([g[0] for g in got], ["xhigh"])

    def test_list_encoding(self):
        got = N.normalize_efforts({"reasoning_effort_levels": ["low", "high"]})
        self.assertEqual(sorted(g[0] for g in got), ["high", "low"])
        self.assertTrue(all(g[2] == "list" for g in got))

    def test_default_is_flagged(self):
        got = N.normalize_efforts({"reasoning_effort_levels": ["low", "high"],
                                   "default_reasoning_effort": "high"})
        flags = {g[0]: g[3] for g in got}
        self.assertEqual(flags, {"low": 0, "high": 1})

    def test_empty_list_yields_nothing(self):
        self.assertEqual(N.normalize_efforts({"reasoning_effort_levels": []}), [])


class TestPriceGrammar(unittest.TestCase):
    CASES = {
        # plain
        "input_cost_per_token": ("input", "per_token", None, None, None, None),
        "output_cost_per_token": ("output", "per_token", None, None, None, None),
        # `<x>_token_cost` form, not `cost_per_`
        "cache_read_input_token_cost": ("cache_read", "per_token", None, None, None, "input"),
        "cache_creation_input_audio_token_cost": (
            "cache_creation", "per_token", "audio", None, None, "input_audio",
        ),
        # tier markers attaching directly to `_token_cost`
        "cache_read_input_token_cost_batches": ("cache_read", "per_token", None, "batches", None, "input"),
        "input_cost_per_token_priority": ("input", "per_token", None, "priority", None, None),
        "cache_read_input_token_cost_balanced": ("cache_read", "per_token", None, "balanced", None, "input"),
        # band markers
        "input_cost_per_token_above_200k_tokens": ("input", "per_token", None, None, 200000, None),
        "output_cost_per_token_above_272k_tokens_ultrafast": (
            "output", "per_token", None, "ultrafast", 272000, None,
        ),
        # two markers stacked, in this order
        "cache_creation_input_token_cost_above_1hr_above_200k_tokens": (
            "cache_creation", "per_token", None, None, 200000, "input",
        ),
        # unit scaling must survive in the qualifier
        "computer_use_input_cost_per_1k_tokens": ("computer_use", "per_token", None, None, None, "input_scaled_1k"),
        "file_search_cost_per_1k_calls": ("file_search", "per_call", None, None, None, "scaled_1k"),
        # modality is both a modality and a unit
        "input_cost_per_audio_token": ("input", "per_token", "audio", None, None, None),
        "input_cost_per_image": ("input", "per_image", None, None, None, None),
        "output_cost_per_image_0.5K": ("output", "per_image", None, None, None, "0.5K"),
        "input_cost_per_audio_per_second": ("input", "per_second", "audio", None, None, None),
        "output_cost_per_second_1080p": ("output", "per_second", None, None, None, "1080p"),
        # odd units
        "file_search_cost_per_gb_per_day": ("file_search", "per_gb_per_day", None, None, None, None),
        "guardrail_cost_per_unit": ("guardrail", "per_unit", None, None, None, None),
        "code_interpreter_cost_per_session": ("code_interpreter", "per_session", None, None, None, None),
        "ocr_cost_per_page": ("ocr", "per_page", None, None, None, None),
        "citation_cost_per_token": ("citation", "per_token", None, None, None, None),
        "output_cost_per_reasoning_token": ("output", "per_token", None, None, None, "reasoning"),
        # input survives ahead of the dbu qualifier
        "input_dbu_cost_per_token": ("input", "per_token", None, None, None, "dbu"),
        # no direction prefix at all
        "cost_per_second": ("unspecified", "per_second", None, None, None, None),
    }

    def test_known_shapes(self):
        for field, expected in self.CASES.items():
            with self.subTest(field=field):
                spec = N.parse_price_name(field)
                self.assertIsNotNone(spec, f"{field} failed to parse")
                got = (
                    spec["direction"], spec["unit"], spec["modality"],
                    spec["tier"], spec["band_start"], spec["qualifier"],
                )
                self.assertEqual(got, expected)

    def test_cache_ttl_is_extracted(self):
        spec = N.parse_price_name("cache_creation_input_token_cost_above_1hr")
        self.assertEqual(spec["cache_ttl"], "1hr")

    def test_non_price_fields_return_none(self):
        for field in ("supports_vision", "mode", "max_input_tokens", "rpm", "tiered_pricing"):
            with self.subTest(field=field):
                self.assertIsNone(N.parse_price_name(field))

    def test_unparseable_price_name_returns_none_rather_than_guessing(self):
        self.assertIsNone(N.parse_price_name("some_unknown_cost_field"))


class TestRouting(unittest.TestCase):
    def test_flat_price_routes_to_price(self):
        self.assertEqual(N.route_field("input_cost_per_token", 1e-06), N.ROUTE_PRICE)

    def test_non_numeric_price_routes_to_schedule(self):
        # search_context_cost_per_query is a list of per-provider costs upstream.
        self.assertEqual(
            N.route_field("search_context_cost_per_query", [{"cost": 0.01}]), N.ROUTE_SCHEDULE
        )

    def test_unclassifiable_price_routes_to_fact_not_dropped(self):
        self.assertEqual(N.route_field("some_unknown_cost_field", 1.5), N.ROUTE_FACT)

    def test_effort_fields_route_to_effort(self):
        self.assertEqual(N.route_field("supports_xhigh_reasoning_effort", True), N.ROUTE_EFFORT)
        self.assertEqual(N.route_field("reasoning_effort_levels", ["low"]), N.ROUTE_EFFORT)

    def test_plain_fields_route_to_fact(self):
        self.assertEqual(N.route_field("supports_vision", True), N.ROUTE_FACT)
        self.assertEqual(N.route_field("rpm", 1000), N.ROUTE_FACT)


if __name__ == "__main__":
    unittest.main()
