import json
import os
import sqlite3
import tempfile
import unittest

from modelmeta import schema as S
from modelmeta.fetch import SnapshotError, load_document, read_snapshot
from modelmeta.ingest import build, content_hash
from modelmeta.verify import expected_counts, verify

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "snapshot_mini.json")
FIXTURE_UTF16 = os.path.join(HERE, "fixtures", "snapshot_mini_utf16.json")


def build_fixture(path=FIXTURE):
    doc, _sha = read_snapshot(path)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    build(doc, tmp.name, origin=path)
    return tmp.name, doc


class TestDocumentGuard(unittest.TestCase):
    """The guard that makes a wrong-format snapshot a loud failure.

    The loader cannot silently ingest a flat `model -> record` map as this document shape,
    because it would turn ten metadata keys into ten models and drop every real one.
    """

    def test_rejects_a_flat_model_map(self):
        flat = json.dumps({"gpt-4o": {"mode": "chat"}, "claude": {"mode": "chat"}}).encode()
        with self.assertRaises(SnapshotError) as ctx:
            load_document(flat)
        self.assertIn("flat model map", str(ctx.exception))

    def test_rejects_models_that_are_not_a_list(self):
        with self.assertRaises(SnapshotError):
            load_document(json.dumps({"models": {"a": {}}}).encode())

    def test_rejects_a_model_without_catalog_id(self):
        with self.assertRaises(SnapshotError):
            load_document(json.dumps({"models": [{"name": "x"}]}).encode())

    def test_accepts_the_real_shape(self):
        doc, _ = read_snapshot(FIXTURE)
        self.assertIsInstance(doc["models"], list)
        self.assertEqual(len(doc["models"]), 6)

    def test_all_four_encodings_agree(self):
        payload = '{"models": [{"catalog_id": "模型/x"}], "a": 1}'
        text = json.dumps(json.loads(payload), ensure_ascii=False)
        for label, raw in {
            "utf8": text.encode("utf-8"),
            "utf8-bom": ("﻿" + text).encode("utf-8"),
            "utf16-bom": text.encode("utf-16"),
            "utf16le": text.encode("utf-16-le"),
        }.items():
            with self.subTest(encoding=label):
                self.assertEqual(load_document(raw), json.loads(text))


class TestMirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.doc = build_fixture()

    def setUp(self):
        self._conns = []

    def tearDown(self):
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

    def scalar(self, sql, params=()):
        return self.conn().execute(sql, params).fetchone()[0]

    # -- completeness ---------------------------------------------------------------

    def test_every_key_produced_the_expected_rows(self):
        ex = expected_counts(self.doc)
        for table in sorted(ex):
            with self.subTest(table=table):
                self.assertEqual(self.scalar(f"SELECT COUNT(*) FROM {table}"), ex[table])

    def test_verify_passes_on_the_fixture(self):
        results = verify(self.db, FIXTURE, check_determinism=False)
        failed = [r for r in results if not r["ok"]]
        self.assertEqual(failed, [], f"failed: {failed}")

    # -- the tri-state, in all three of its shapes ----------------------------------

    def test_parameter_capabilities_never_report_false(self):
        # Upstream infers these from supported_parameters, so `false` never occurs.
        # A zero here would mean the loader invented one.
        for name in sorted(S.FALSE_IS_IMPOSSIBLE):
            with self.subTest(capability=name):
                self.assertEqual(
                    self.scalar("SELECT COUNT(*) FROM capability WHERE name=? AND value=0", (name,)),
                    0,
                )

    def test_modality_capabilities_never_report_null(self):
        # These come from an explicit declared list, so a value always exists.
        for name in sorted(S.FALSE_IS_MEANINGFUL):
            with self.subTest(capability=name):
                self.assertEqual(
                    self.scalar("SELECT COUNT(*) FROM capability WHERE name=? AND value IS NULL", (name,)),
                    0,
                )

    def test_never_established_capabilities_are_declared_but_null(self):
        for name in ("parallel_tool_calls", "streaming"):
            with self.subTest(capability=name):
                self.assertGreater(
                    self.scalar("SELECT COUNT(*) FROM capability WHERE name=?", (name,)), 0
                )
                self.assertEqual(
                    self.scalar(
                        "SELECT COUNT(*) FROM capability WHERE name=? AND value IS NOT NULL",
                        (name,),
                    ),
                    0,
                )

    def test_model_capability_view_is_tri_state(self):
        rows = dict(
            (r["capability"], r["value"])
            for r in self.conn().execute(
                "SELECT capability, value FROM model_capability WHERE catalog_id=?",
                ("deepseek/deepseek-v4.1-flash",),
            )
        )
        self.assertEqual(rows["vision"], 1)          # both offerings say yes
        self.assertEqual(rows["audio_input"], 0)     # declared false, and false is meaningful
        self.assertIsNone(rows["parallel_tool_calls"])  # nobody established it
        self.assertEqual(rows["tool_calling"], 1)

    # -- suspicious values preserved -----------------------------------------------

    def test_zero_token_limit_is_stored_as_zero(self):
        value = self.scalar(
            "SELECT context_tokens FROM offering o JOIN model m ON m.id=o.model_id "
            "WHERE m.catalog_id='respan/span-01-lite'"
        )
        self.assertEqual(value, 0)

    def test_columns_the_snapshot_leaves_empty_stay_empty(self):
        for tbl, col in (
            ("offering", "max_input_tokens"),
            ("model", "release_date"),
            ("model", "open_weights"),
            ("model", "license"),
            ("model", "parameter_count"),
        ):
            with self.subTest(column=f"{tbl}.{col}"):
                self.assertEqual(
                    self.scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {col} IS NOT NULL"), 0
                )

    def test_official_stub_is_kept_as_a_stub(self):
        row = self.conn().execute(
            "SELECT o.* FROM offering o JOIN model m ON m.id=o.model_id "
            "WHERE m.catalog_id='google/gemini-2.5-flash' AND o.authority='official'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["provider_model_id"])
        self.assertIsNotNone(row["notes"])
        self.assertIsNone(row["context_tokens"])
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM capability WHERE offering_id=?", (row["id"],)), 0
        )

    def test_pricing_can_be_absent_without_losing_the_offering(self):
        # z-ai/glm-5.3-flash's *official* offering declares no pricing at all. The
        # offering row must still exist -- dropping it would lose the fact that the
        # provider documents the model.
        row = self.conn().execute(
            "SELECT o.* FROM offering o JOIN model m ON m.id=o.model_id "
            "WHERE m.catalog_id='z-ai/glm-5.3-flash' "
            "AND NOT EXISTS (SELECT 1 FROM price p WHERE p.offering_id=o.id)"
        ).fetchone()
        self.assertIsNotNone(row, "the price-less offering must still exist")
        self.assertEqual(row["authority"], "official")

    def test_a_model_can_be_discoverable_only_through_official_documentation(self):
        """Four models in the real snapshot have no aggregator listing at all.

        They are not absent -- they are reachable only via a provider's own documentation,
        which is a distinct and worth-preserving state.
        """
        row = self.conn().execute(
            "SELECT COUNT(*) AS n FROM offering o JOIN model m ON m.id=o.model_id "
            "WHERE m.catalog_id='moonshotai/kimi-k2.7-code-highspeed' "
            "AND o.authority='catalog'"
        ).fetchone()
        self.assertEqual(row["n"], 0)
        official = self.conn().execute(
            "SELECT COUNT(*) AS n FROM offering o JOIN model m ON m.id=o.model_id "
            "WHERE m.catalog_id='moonshotai/kimi-k2.7-code-highspeed' "
            "AND o.authority='official'"
        ).fetchone()
        self.assertEqual(official["n"], 1)

    def test_every_model_has_at_least_one_offering(self):
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM model m WHERE NOT EXISTS "
                "(SELECT 1 FROM offering o WHERE o.model_id = m.id)"
            ),
            0,
        )

    # -- effort sets ----------------------------------------------------------------

    def test_reordered_effort_lists_collapse_to_one_set(self):
        # deepseek/deepseek-v4.1-flash is both offerings: ['max','high','low'] and
        # ['low','high','max'].
        rows = self.conn().execute(
            "SELECT o.id, GROUP_CONCAT(re.effort) AS g FROM offering o "
            "JOIN model m ON m.id=o.model_id LEFT JOIN reasoning_effort re "
            "ON re.offering_id=o.id WHERE m.catalog_id='deepseek/deepseek-v4.1-flash' "
            "GROUP BY o.id ORDER BY o.id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["g"], rows[1]["g"])
        self.assertEqual(sorted(rows[0]["g"].split(",")), ["high", "low", "max"])

    # -- conflicts ------------------------------------------------------------------

    def test_conflict_keeps_both_values(self):
        row = self.conn().execute(
            "SELECT c.* FROM conflict c JOIN model m ON m.id=c.model_id "
            "WHERE m.catalog_id='deepseek/deepseek-v4.1-flash'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["field"], "limits.max_output_tokens")
        self.assertEqual(json.loads(row["official_value_json"]), 393216)
        self.assertEqual(json.loads(row["aggregator_value_json"]), 943718)
        self.assertTrue(row["resolution"])

    # -- taxonomy stays in lockstep -------------------------------------------------

    def test_registry_matches_capability_def(self):
        db_rows = {
            (r["name"], r["domain"], r["evidence_aspect"], r["false_is_meaningful"])
            for r in self.conn().execute(
                "SELECT name, domain, evidence_aspect, false_is_meaningful FROM capability_def"
            )
        }
        self.assertEqual(db_rows, set(S.capability_rows()))

    def test_taxonomy_is_a_partition(self):
        self.assertEqual(len(S.FALSE_IS_MEANINGFUL & S.FALSE_IS_IMPOSSIBLE), 0)
        self.assertEqual(
            S.FALSE_IS_MEANINGFUL | S.FALSE_IS_IMPOSSIBLE, set(S.CAPABILITY_NAMES)
        )

    # -- determinism ----------------------------------------------------------------

    def test_rebuild_is_byte_identical(self):
        results = verify(self.db, FIXTURE, check_determinism=True)
        det = [r for r in results if "byte-identical" in r["name"]]
        self.assertTrue(det and det[0]["ok"], det)

    def test_utf16_and_utf8_snapshots_produce_byte_identical_databases(self):
        """The previous iteration's actual defect, kept as a regression test.

        It recorded a hash of the raw input file, so the same data arriving as UTF-16+CRLF
        and as UTF-8+LF produced different databases and the committed one could not be
        reproduced from the documented path.
        """
        doc16, _sha = read_snapshot(FIXTURE_UTF16)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            # Same origin as the UTF-8 build: origin is recorded in meta and is therefore
            # part of the file's bytes, and this test isolates the encoding variable.
            build(doc16, tmp.name, origin=FIXTURE)
            self.assertEqual(content_hash(doc16), content_hash(self.doc))
            with open(tmp.name, "rb") as a, open(self.db, "rb") as b:
                self.assertEqual(a.read(), b.read())
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
