#!/usr/bin/env python3
"""Behavioral tests using fictional documents in disposable directories."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import insurance_evidence as evidence

TEXT = (
    "保险责任\n经基本医疗保险结算后，赔付比例为60%。\n"
    "本合同不承担既往症导致的医疗费用。\n"
    "保证续保期间为6年，每年仍需缴纳保险费。\n"
    "免赔额为10000元，医疗费用小于等于5000元的部分按约定比例给付。"
)
QUOTE = "经基本医疗保险结算后，赔付比例为60%。"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_parent = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="insurance-evidence-test-", dir=self.temp_parent)
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, self.temp_parent)
        self.db = self.root / "证据.sqlite"
        self.manifest = self.root / "sources.json"
        self.current = self.root / "当期.txt"
        self.current.write_text(TEXT, encoding="utf-8")
        (self.root / "历史.txt").write_text(TEXT.replace("60%", "100%"), encoding="utf-8")
        (self.root / "另一单.txt").write_text(
            "本合同保险期间为终身，给付项目为身故保险金。投保人须按约定交纳保险费，"
            "保险责任和责任免除以本合同及特别约定为准。", encoding="utf-8")
        self.sources = [
            {"id": "D01", "path": "当期.txt", "kind": "policy", "scope": "current", "policy_ids": ["P001"]},
            {"id": "D02", "path": "历史.txt", "kind": "policy", "scope": "historical", "policy_ids": ["P001"]},
            {"id": "D03", "path": "另一单.txt", "kind": "policy", "scope": "current", "policy_ids": ["P002"]},
        ]
        self.save_manifest()
        evidence.index_sources(self.db, self.manifest)
        self.context = evidence.database(self.db, write=True)
        self.conn = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.assertEqual(self.root.parent, self.temp_parent)
        self.temp.cleanup()

    def save_manifest(self):
        self.manifest.write_text(evidence.dump({"sources": self.sources}), encoding="utf-8")

    def fact(self, fact_id="F001", source="D01", value="60", period="2026"):
        return {
            "id": fact_id, "policy_id": "P001", "field": "medical_ratio",
            "value": value, "unit": "%", "condition": "经医保结算", "period": period,
            "citation": {"source_id": source, "page": 1,
                         "quote": QUOTE if source == "D01" else QUOTE.replace("60%", "100%")},
        }

    def test_chinese_substring_search(self):
        result = evidence.search(self.conn, "保证续保")
        self.assertEqual(result["status"], "matches")
        self.assertTrue(result["retrieval_only"])
        self.assertEqual(result["results"][0]["source_id"], "D01")
        self.assertEqual(result["results"][0]["page"], 1)

    def test_spaced_chinese_tokens(self):
        self.assertIn("住院", evidence.tokens("住 \n院前后门急诊"))
        self.assertIn("门诊", evidence.tokens("门 诊"))

    def test_history_excluded_unless_requested(self):
        current = evidence.search(self.conn, "赔付比例")
        all_sources = evidence.search(self.conn, "赔付比例", scope="all")
        self.assertEqual({r["source_id"] for r in current["results"]}, {"D01"})
        self.assertEqual({r["source_id"] for r in all_sources["results"]}, {"D01", "D02"})

    def test_policy_and_source_scope(self):
        self.assertEqual(evidence.search(self.conn, "赔付比例", policy="P002")["results"], [])
        self.assertEqual(evidence.search(self.conn, "保险期间", source="D03")["results"][0]["source_id"], "D03")

    def test_empty_and_fts_syntax_queries(self):
        self.assertEqual(evidence.search(self.conn, " \n -- ")["status"], "no_match")
        self.assertEqual(evidence.search(self.conn, '" OR SQLINJECTIONUNIQUE "')["status"], "no_match")

    def test_reindex_and_duplicate_content_are_idempotent(self):
        self.sources.append({**self.sources[0], "id": "D04"})
        self.save_manifest()
        result = evidence.index_sources(self.db, self.manifest)
        self.assertEqual(result["counts"]["sources"], 4)
        self.assertEqual(result["counts"]["documents"], 3)
        again = evidence.index_sources(self.db, self.manifest)
        self.assertEqual(again["counts"], result["counts"])

    def test_same_id_new_bytes_rejected_without_partial_changes(self):
        self.current.write_text(TEXT + "\n新内容", encoding="utf-8")
        with self.assertRaisesRegex(evidence.EvidenceError, "source_content_changed_use_new_id"):
            evidence.index_sources(self.db, self.manifest)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 3)

    def test_source_metadata_changes_have_history(self):
        self.sources[0]["scope"] = "historical"
        self.save_manifest()
        evidence.index_sources(self.db, self.manifest)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 1)
        self.assertEqual(evidence.search(self.conn, "赔付比例")["results"], [])

    def test_quote_layout_normalization_and_semantic_boundary(self):
        quote = QUOTE.replace("60%", "６０％").replace("结算后", "结 算\n后")
        result = evidence.verify_quote(self.conn, "D01", 1, quote)
        self.assertTrue(result["valid"])
        self.assertEqual(result["semantic_support"], "not_checked")
        self.assertEqual(result["original_excerpt"], QUOTE)

    def test_changed_number_rejected(self):
        self.assertFalse(evidence.verify_quote(self.conn, "D01", 1, QUOTE.replace("60%", "100%"))["valid"])

    def test_changed_negation_rejected(self):
        self.assertFalse(evidence.verify_quote(
            self.conn, "D01", 1, "本合同承担既往症导致的医疗费用。")["valid"])

    def test_comparison_and_decimal_not_discarded(self):
        self.assertNotEqual(evidence.canonical("金额<=5000"), evidence.canonical("金额>=5000"))
        self.assertNotEqual(evidence.canonical("金额1.00元"), evidence.canonical("金额100元"))
        self.assertFalse(evidence.verify_quote(
            self.conn, "D01", 1, "医疗费用大于等于5000元的部分按约定比例给付。")["valid"])

    def test_wrong_document_page_and_spliced_text(self):
        self.assertFalse(evidence.verify_quote(self.conn, "D03", 1, QUOTE)["valid"])
        with self.assertRaisesRegex(evidence.EvidenceError, "page_missing"):
            evidence.verify_quote(self.conn, "D01", 2, QUOTE)
        self.assertFalse(evidence.verify_quote(self.conn, "D01", 1, "经基本医疗保险赔付比例为60%。")["valid"])

    def test_short_quotes_do_not_create_evidence(self):
        self.assertEqual(evidence.verify_quote(self.conn, "D01", 1, "60%")["reason"], "quote_too_short")

    def test_changed_source_blocks_retrieval_verify_and_check(self):
        self.current.write_text(TEXT + "\nchanged", encoding="utf-8")
        result = evidence.search(self.conn, "保证续保")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["blocked_sources"]["D01"], "source_changed")
        with self.assertRaisesRegex(evidence.EvidenceError, "source_changed"):
            evidence.verify_quote(self.conn, "D01", 1, QUOTE)
        self.assertFalse(evidence.check(self.conn)["ok"])

    def test_import_is_atomic_and_facts_start_pending(self):
        invalid = self.fact("F002")
        invalid["citation"]["quote"] = "本合同免除所有的缴费义务"
        with self.assertRaisesRegex(evidence.EvidenceError, "invalid_quote"):
            evidence.add_facts(self.conn, [self.fact(), invalid])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
        evidence.add_facts(self.conn, [self.fact()])
        self.assertEqual(evidence.fact_records(self.conn)[0]["status"], "needs_review")

    def test_fact_source_policy_mismatch_rejected(self):
        wrong = self.fact()
        wrong["policy_id"] = "P002"
        with self.assertRaisesRegex(evidence.EvidenceError, "policy_source_mismatch"):
            evidence.add_facts(self.conn, [wrong])

    def test_import_cannot_overwrite_original_or_review(self):
        original = self.fact()
        evidence.add_facts(self.conn, [original])
        evidence.review(self.conn, "F001", 0, "confirm", "assistant", "已核读虚构原页")
        self.assertEqual(evidence.add_facts(self.conn, [original])["unchanged"], 1)
        changed = copy.deepcopy(original)
        changed["value"] = "61"
        with self.assertRaisesRegex(evidence.EvidenceError, "fact_exists_use_review"):
            evidence.add_facts(self.conn, [changed])
        self.assertEqual(evidence.audit(self.conn, "F001")["revision"], 1)

    def test_revision_conflict_prevents_lost_update(self):
        evidence.add_facts(self.conn, [self.fact()])
        evidence.review(self.conn, "F001", 0, "confirm", "assistant", "已核读")
        with self.assertRaisesRegex(evidence.EvidenceError, "revision_conflict"):
            evidence.review(self.conn, "F001", 0, "ignore", "assistant", "过期视图")
        self.assertEqual(len(evidence.audit(self.conn, "F001")["events"]), 1)

    def test_invalid_correction_does_not_create_event(self):
        evidence.add_facts(self.conn, [self.fact()])
        replacement = self.fact(value="70")
        replacement["citation"]["quote"] = QUOTE.replace("60%", "70%")
        with self.assertRaisesRegex(evidence.EvidenceError, "invalid_quote"):
            evidence.review(self.conn, "F001", 0, "correct", "assistant", "无依据更正", replacement)
        self.assertEqual(evidence.audit(self.conn, "F001")["events"], [])

    def test_correct_reopen_and_ignore_preserve_original(self):
        original = self.fact()
        evidence.add_facts(self.conn, [original])
        replacement = self.fact(source="D02", value="100")
        evidence.review(self.conn, "F001", 0, "correct", "assistant", "改引虚构补充资料", replacement)
        evidence.review(self.conn, "F001", 1, "reopen", "assistant", "需再次复核期间")
        evidence.review(self.conn, "F001", 2, "ignore", "assistant", "不适用")
        trail = evidence.audit(self.conn, "F001")
        self.assertEqual(trail["original"]["value"], "60")
        self.assertEqual(trail["current"]["value"], "100")
        self.assertEqual([e["revision"] for e in trail["events"]], [1, 2, 3])
        self.assertEqual(trail["status"], "ignored")

    def test_different_periods_not_reported_as_conflict(self):
        evidence.add_facts(self.conn, [self.fact(), self.fact("F002", "D02", "100", "2025")])
        self.assertEqual(evidence.conflicts(self.conn, scope="all")["groups"], [])
        evidence.add_facts(self.conn, [self.fact("F003", "D02", "100", "2026")])
        self.assertEqual(len(evidence.conflicts(self.conn, scope="all")["groups"]), 1)
        evidence.review(self.conn, "F003", 0, "ignore", "assistant", "错误期间")
        self.assertEqual(evidence.conflicts(self.conn, scope="all")["groups"], [])

    def test_valid_corpus_checks_and_missing_fts_detected(self):
        evidence.add_facts(self.conn, [self.fact()])
        self.assertTrue(evidence.check(self.conn)["ok"])
        with self.conn:
            self.conn.execute("DELETE FROM page_fts WHERE rowid=(SELECT min(id) FROM pages)")
        result = evidence.check(self.conn)
        self.assertFalse(result["ok"])
        self.assertIn("index_mismatch", [p["reason"] for p in result["problems"]])

    def test_missing_read_database_not_created(self):
        missing = self.root / "does-not-exist.sqlite"
        with self.assertRaisesRegex(evidence.EvidenceError, "database_missing"):
            with evidence.database(missing):
                pass
        self.assertFalse(missing.exists())

    def test_cli_search_returns_utf8_json(self):
        completed = subprocess.run(
            [sys.executable, str(Path(evidence.__file__).resolve()), "search",
             "--db", str(self.db), "--query", "保证续保"],
            capture_output=True, text=True, encoding="utf-8", check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["results"][0]["source_id"], "D01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
