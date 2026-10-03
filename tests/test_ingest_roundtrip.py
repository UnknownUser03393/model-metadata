import json
import os
import sqlite3
import tempfile
import unittest

from modelmeta import query as Q
from modelmeta.fetch import decode, read_source
from modelmeta.ingest import build
from modelmeta.verify import verify

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "mini.json")
FIXTURE_UTF16 = os.path.join(HERE, "fixtures", "mini_utf16.json")
OBSERVED_AT = "2026-10-03T00:00:00Z"


def build_fixture(source_path=FIXTURE, include_raw=True):
    records, sha, origin = read_source(path=source_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    build(records, tmp.name, OBSERVED_AT, sha, origin=source_path, include_raw=include_raw)
    return tmp.name, records


class TestDecode(unittest.TestCase):
    def test_all_three_encodings_agree(self):
        payload = '{"a": "模型"}'
        utf8 = payload.encode("utf-8")
        self.assertEqual(decode(utf8), payload)
        self.assertEqual(decode(payload.encode("utf-16")), payload)          # BOM
        self.assertEqual(decode(payload.encode("utf-16-le")), payload)       # no BOM
        self.assertEqual(decode(b"\xef\xbb\xbf" + utf8), payload)            # UTF-8 BOM

    def test_bom_less_utf16_does_not_fall_back_to_a_wrong_codec(self):
        # A non-ASCII model name decoded as cp1252 would come back mojibake rather than
        # raising, so the sniff has to be explicit.
        self.assertEqual(decode('{"k":"模型"}'.encode("utf-16-le")), '{"k":"模型"}')


class TestBuildFromFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.records = build_fixture()

    def setUp(self):
        self._conns = []

    def tearDown(self):
        # Windows refuses to unlink a file while a sqlite connection holds it open.
        for c in self._conns:
            c.close()
        self._conns = []

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db)

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        self._conns.append(c)
        return c

    def qconn(self):
        c = Q.connect(self.db, read_only=True)
        self._conns.append(c)
        return c

    def test_every_entry_gets_a_model_row(self):
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM model").fetchone()[0], len(self.records))
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM model WHERE is_doc_entry = 1").fetchone()[0], 2
        )

    def test_round_trip_is_exact(self):
        c = self.conn()
        rows = c.execute(
            "SELECT m.key, r.v_json FROM raw_record r JOIN model m ON m.id = r.model_id"
        ).fetchall()
        self.assertEqual(len(rows), len(self.records))
        for r in rows:
            self.assertEqual(json.loads(r["v_json"]), self.records[r["key"]])

    def test_utf16_and_utf8_snapshots_produce_the_same_data(self):
        db16, _recs = build_fixture(FIXTURE_UTF16)
        try:
            a = self.conn()
            b = sqlite3.connect(db16)
            # Compare as plain tuples: sqlite3.Row does not compare equal to tuple.
            q = "SELECT key, mode, provider FROM model ORDER BY key"
            self.assertEqual([tuple(r) for r in a.execute(q)], [tuple(r) for r in b.execute(q)])
            q2 = (
                "SELECT raw_field, direction, unit, band_start, price FROM price "
                "ORDER BY raw_field, band_start"
            )
            self.assertEqual([tuple(r) for r in a.execute(q2)], [tuple(r) for r in b.execute(q2)])
            b.close()
        finally:
            os.unlink(db16)

    def test_tiered_pricing_is_decomposed_into_bands(self):
        c = self.conn()
        mid = c.execute("SELECT id FROM model WHERE key='test/tiered-code'").fetchone()["id"]
        rows = c.execute(
            "SELECT price, band_start, band_end FROM price WHERE model_id=? AND "
            "direction='input' ORDER BY band_start",
            (mid,),
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(5e-08, 0, 256000), (2.5e-07, 256000, 1000000)])

    def test_price_at_resolves_the_right_band(self):
        c = self.qconn()
        mid = c.execute("SELECT id FROM model WHERE key='test/tiered-code'").fetchone()["id"]
        self.assertEqual(c.execute("SELECT price_at(?, 'input', 1000)", (mid,)).fetchone()[0], 5e-08)
        self.assertEqual(c.execute("SELECT price_at(?, 'input', 300000)", (mid,)).fetchone()[0], 2.5e-07)

    def test_price_at_returns_nothing_for_a_model_with_no_flat_price(self):
        # A model priced only via a schedule must still resolve, and a model with no
        # token price at all must return NULL rather than a bogus number.
        c = self.qconn()
        mid = c.execute("SELECT id FROM model WHERE key='test/no-limits'").fetchone()["id"]
        self.assertIsNone(c.execute("SELECT price_at(?, 'input', 1000)", (mid,)).fetchone()[0])

    def test_tri_state_vision(self):
        c = self.conn()
        states = {}
        for row in c.execute(
            "SELECT m.key, f.v_bool FROM model m LEFT JOIN resolved_fact f "
            "ON f.model_id = m.id AND f.field='supports_vision'"
        ):
            states[row["key"]] = row["v_bool"]
        self.assertEqual(states["test/basic-chat"], 1)
        self.assertEqual(states["test/tiered-code"], 0)
        # Absent and explicitly-null are both unknown, never false.
        self.assertIsNone(states["test/no-limits"])
        self.assertIsNone(states["test/weird-mode"])

    def test_search_excludes_models_that_are_silent_about_a_capability(self):
        c = self.qconn()
        keys = [
            r["key"]
            for r in c.execute(
                "SELECT m.key FROM model m WHERE m.is_doc_entry = 0 AND EXISTS "
                "(SELECT 1 FROM resolved_fact f WHERE f.model_id = m.id "
                "AND f.field='supports_vision' AND f.v_bool = 1)"
            )
        ]
        self.assertIn("test/basic-chat", keys)
        self.assertNotIn("test/tiered-code", keys)
        self.assertNotIn("test/no-limits", keys)
        self.assertNotIn("test/weird-mode", keys)

    def test_reasoning_efforts_merged_from_both_encodings(self):
        c = self.conn()
        b = c.execute("SELECT id FROM model WHERE key='test/basic-chat'").fetchone()["id"]
        t = c.execute("SELECT id FROM model WHERE key='test/tiered-code'").fetchone()["id"]
        self.assertEqual(
            sorted(r[0] for r in c.execute(
                "SELECT level FROM reasoning_effort WHERE model_id=?", (b,))),
            ["xhigh"],
        )
        self.assertEqual(
            sorted(r[0] for r in c.execute(
                "SELECT level FROM reasoning_effort WHERE model_id=?", (t,))),
            ["high", "low"],
        )

    def test_unparseable_price_field_is_kept_and_reported(self):
        c = self.conn()
        n = c.execute(
            "SELECT COUNT(*) FROM fact WHERE raw_field='some_unknown_cost_field' AND is_derived=0"
        ).fetchone()[0]
        self.assertEqual(n, 1)
        issues = c.execute(
            "SELECT COUNT(*) FROM ingest_issue WHERE severity='unclassified' "
            "AND raw_field='some_unknown_cost_field'"
        ).fetchone()[0]
        self.assertEqual(issues, 1)

    def test_doc_entries_do_not_leak_into_search(self):
        c = self.qconn()
        for row in c.execute("SELECT key FROM model WHERE is_doc_entry = 1"):
            self.assertIn(row["key"], ("sample_spec", "fallback_generalizations"))

    def test_verify_passes_on_the_fixture(self):
        results = verify(self.db, FIXTURE, check_determinism=False)
        failed = [r for r in results if not r["ok"]]
        self.assertEqual(failed, [], f"failed checks: {failed}")

    def test_rebuild_is_byte_identical(self):
        results = verify(self.db, FIXTURE, check_determinism=True)
        det = [r for r in results if "byte-identical" in r["name"]]
        self.assertTrue(det and det[0]["ok"], det)

    def test_no_raw_omits_raw_record_but_keeps_everything_else(self):
        db, _ = build_fixture(include_raw=False)
        try:
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM model").fetchone()[0], len(self.records))
            c.close()
        finally:
            os.unlink(db)


if __name__ == "__main__":
    unittest.main()
