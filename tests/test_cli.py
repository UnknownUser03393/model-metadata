import contextlib
import io
import json
import os
import tempfile
import unittest

from modelmeta import cli, query as Q
from modelmeta.fetch import read_snapshot
from modelmeta.ingest import build

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "snapshot_mini.json")


def run(*argv):
    """Invoke the CLI and capture stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, buf.getvalue()


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db.close()
        doc, _ = read_snapshot(FIXTURE)
        build(doc, cls.db.name, origin=FIXTURE)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db.name)

    def setUp(self):
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            c.close()

    def conn(self):
        c = Q.connect(self.db.name, read_only=True)
        self._conns.append(c)
        return c

    # -- get ------------------------------------------------------------------------

    def test_get_shows_both_offerings_and_the_conflict(self):
        code, out = run("get", "deepseek/deepseek-v4.1-flash", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertIn("[catalog] openrouter", out)
        self.assertIn("[official] deepseek", out)
        self.assertIn("393216", out)
        self.assertIn("943718", out)
        self.assertIn("retain both", out)
        # The two offerings disagree on max_output, and both numbers must be visible.
        self.assertIn("off_peak", out)

    def test_get_renders_tri_state_without_collapsing_it(self):
        _code, out = run("get", "deepseek/deepseek-v4.1-flash", "--db", self.db.name)
        self.assertIn("parallel_tool_calls=unknown", out)
        self.assertIn("vision=true", out)
        self.assertIn("false cannot occur", out)

    def test_get_reports_zero_context_rather_than_hiding_it(self):
        _code, out = run("get", "respan/span-01-lite", "--db", self.db.name)
        self.assertIn("context=0", out)
        self.assertIn("snapshot does not say what that means", out)

    def test_get_json_round_trips(self):
        code, out = run("get", "deepseek/deepseek-v4.1-flash", "--db", self.db.name, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["offerings"]), 2)
        self.assertEqual(len(payload["conflicts"]), 1)
        catalog = [o for o in payload["offerings"] if o["authority"] == "catalog"][0]
        self.assertIsNone(catalog["capabilities"]["parallel_tool_calls"])
        self.assertIs(catalog["capabilities"]["vision"], 1)

    def test_get_missing_model_explains_the_lookup_rule(self):
        code, _out = run("get", "does/not-exist", "--db", self.db.name)
        self.assertEqual(code, 1)

    # -- search: the two kinds of tri-state behave differently ----------------------

    def test_positive_filter_excludes_silent_offerings(self):
        # gpt-audio-mini says vision=true; the fixture's other models say false.
        code, out = run("search", "--vision", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertNotIn("0 offering(s)", out)

    def test_negated_parameter_capability_returns_empty_not_everything(self):
        """The point of the whole taxonomy.

        `tool_calling` is inferred from supported_parameters, so upstream never reports
        false for it. A search for --no-tools must therefore match nothing. A loader that
        had coerced null to false would return every offering here instead.
        """
        code, out = run("search", "--no-tools", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertIn("0 offering(s)", out)

    def test_negated_modality_capability_does_return_matches(self):
        # vision is a declared modality, so false is a real observation.
        code, out = run("search", "--no-vision", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertNotIn("0 offering(s)", out)

    def test_text_only_filter(self):
        code, out = run("search", "--text-only", "--db", self.db.name, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload)
        for row in payload:
            self.assertEqual(row["context_tokens"] is None, False)

    def test_search_json_carries_the_tri_state(self):
        _code, out = run("search", "--vision", "--db", self.db.name, "--json")
        payload = json.loads(out)
        self.assertTrue(any(o["capabilities"]["vision"] == 1 for o in payload))

    # -- diff ----------------------------------------------------------------------

    def test_diff_lists_differing_fields(self):
        code, out = run("diff", "deepseek/deepseek-v4.1-flash", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertIn("max_output_tokens", out)
        self.assertIn("differs", out)

    # -- sources / rules -----------------------------------------------------------

    def test_sources_explains_the_null_probe_status(self):
        code, out = run("sources", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertIn("never HTTP-checked", out)

    def test_rules_quotes_the_snapshot_contract(self):
        code, out = run("rules", "--db", self.db.name)
        self.assertEqual(code, 0)
        self.assertIn("string similarity", out)


class TestUpdateGuard(unittest.TestCase):
    def test_update_rejects_a_wrong_format_snapshot_without_writing(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"gpt-4o": {"mode": "chat"}}, bad)
        bad.close()
        out_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        out_db.close()
        os.unlink(out_db.name)
        try:
            code, _ = run("update", "--from-file", bad.name, "--out", out_db.name)
            self.assertEqual(code, 2)
            self.assertFalse(os.path.exists(out_db.name), "must not write a partial database")
        finally:
            os.unlink(bad.name)
            if os.path.exists(out_db.name):
                os.unlink(out_db.name)


if __name__ == "__main__":
    unittest.main()
