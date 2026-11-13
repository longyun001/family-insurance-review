"""PDF backend and evidence-preservation tests using synthetic local documents."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import insurance_evidence as evidence


TEXT = (
    "本合同不承担既往症导致的医疗费用，费用补偿按约定范围给付。"
    "保险期间和保证续保期间分别列示，每年仍需缴纳保险费。"
)
QUOTE = "本合同不承担既往症导致的医疗费用"


class FakeTools:
    def __init__(self):
        self.show_warnings = True
        self.show_errors = True
        self.buffer = ""

    def mupdf_display_warnings(self, on=None):
        if on is not None:
            self.show_warnings = on
        return self.show_warnings

    def mupdf_display_errors(self, on=None):
        if on is not None:
            self.show_errors = on
        return self.show_errors

    def mupdf_warnings(self, reset=True):
        value = self.buffer
        if reset:
            self.buffer = ""
        return value


def fake_pymupdf(texts, *, warning="", encrypted=False, error=None):
    tools = FakeTools()

    def get_text(text):
        if error:
            raise error
        tools.buffer = warning
        return text

    class Document:
        is_encrypted = encrypted
        closed = False

        def authenticate(self, password):
            return False

        def __iter__(self):
            return iter(SimpleNamespace(get_text=lambda t=text: get_text(t)) for text in texts)

        def close(self):
            self.closed = True

    doc = Document()
    module = SimpleNamespace(TOOLS=tools, __version__="fixture", open=lambda _: doc)
    return module, doc


def write_pdf(path):
    """Three-page text fixture, independent of either extraction library."""
    labels = [f"Physical page {n}: the payment amount is {n}0000 CNY under this contract."
              for n in range(1, 4)]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(3))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count 3 >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(labels):
        stream = f"BT /F1 12 Tf 30 100 Td ({text}) Tj ET".encode("ascii")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * index} 0 R >>".encode("ascii"))
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    path.write_bytes(data)
    return labels


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="insurance-pdf-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "fictional.pdf"
        self.pdf.write_bytes(b"fictional PDF; backend mocked unless write_pdf is called")
        self.db = self.root / "evidence.sqlite"
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"sources": [{
            "id": "D01", "path": str(self.pdf), "kind": "policy", "scope": "current",
            "policy_ids": ["P001"],
        }]}), encoding="utf-8")

    def index_mocked(self, pages, backend="pymupdf"):
        diagnostics = evidence.extraction_diagnostics(pages, backend, [])
        with patch.object(evidence, "_extract_with_" + backend,
                          return_value=(pages, backend + "-fixture", diagnostics)):
            return evidence.index_sources(self.db, self.manifest, backend)

    def test_auto_prefers_pymupdf(self):
        module, _ = fake_pymupdf([TEXT])
        with patch.dict(sys.modules, {"pymupdf": module}), patch.object(evidence, "_extract_with_pypdf") as fallback:
            pages, backend, diagnostic = evidence.extract(self.pdf)
        self.assertEqual(pages, [TEXT])
        self.assertEqual(backend, "pymupdf-fixture")
        self.assertTrue(diagnostic["raw_text_preserved"])
        fallback.assert_not_called()

    def test_auto_falls_back_if_pymupdf_missing(self):
        with patch.dict(sys.modules, {"pymupdf": None}), patch.object(
                evidence, "_extract_with_pypdf", return_value=([TEXT], "pypdf-fixture", {})):
            _, backend, diagnostic = evidence.extract(self.pdf)
        self.assertEqual(backend, "pypdf-fixture")
        self.assertIn("missing_pymupdf", diagnostic["fallback"]["reason"])

    def test_auto_falls_back_if_primary_extraction_fails(self):
        module, doc = fake_pymupdf([TEXT], error=ValueError("damaged fixture"))
        with patch.dict(sys.modules, {"pymupdf": module}), patch.object(
                evidence, "_extract_with_pypdf", return_value=([TEXT], "pypdf-fixture", {})):
            _, backend, diagnostic = evidence.extract(self.pdf)
        self.assertEqual(backend, "pypdf-fixture")
        self.assertIn("pymupdf_extraction_failed", diagnostic["fallback"]["reason"])
        self.assertTrue(doc.closed)

    def test_explicit_backend_does_not_silently_fallback(self):
        with patch.dict(sys.modules, {"pymupdf": None}), patch.object(evidence, "_extract_with_pypdf") as fallback:
            with self.assertRaisesRegex(evidence.EvidenceError, "missing_pymupdf"):
                evidence.extract(self.pdf, "pymupdf")
        fallback.assert_not_called()

    def test_explicit_pypdf_bypasses_pymupdf(self):
        with patch.object(evidence, "_extract_with_pymupdf") as primary, patch.object(
                evidence, "_extract_with_pypdf", return_value=([TEXT], "pypdf-fixture", {})):
            self.assertEqual(evidence.extract(self.pdf, "pypdf")[1], "pypdf-fixture")
        primary.assert_not_called()

    def test_encrypted_pdf_stops_without_fallback(self):
        module, doc = fake_pymupdf([TEXT], encrypted=True)
        with patch.dict(sys.modules, {"pymupdf": module}), patch.object(evidence, "_extract_with_pypdf") as fallback:
            with self.assertRaisesRegex(evidence.EvidenceError, "encrypted_pdf_requires_unlocked_copy"):
                evidence.extract(self.pdf)
        fallback.assert_not_called()
        self.assertTrue(doc.closed)
        self.assertTrue(module.TOOLS.show_errors)
        self.assertTrue(module.TOOLS.show_warnings)

    def test_primary_empty_pdf_can_fallback(self):
        module, doc = fake_pymupdf([])
        with patch.dict(sys.modules, {"pymupdf": module}), patch.object(
                evidence, "_extract_with_pypdf", return_value=([TEXT], "pypdf-fixture", {})):
            self.assertEqual(evidence.extract(self.pdf)[2]["fallback"]["reason"], "empty_pdf")
        self.assertTrue(doc.closed)

    def test_backend_preserves_all_unknown_characters(self):
        raw = TEXT + "金额2\ufffd0元；保\ue001险；\uffff；\x00；\U000f0001"
        module, doc = fake_pymupdf([raw])
        with patch.dict(sys.modules, {"pymupdf": module}):
            pages, _, diagnostic = evidence.extract(self.pdf)
        self.assertEqual(pages, [raw])
        self.assertEqual(diagnostic["anomaly_pages"][0]["counts"], {
            "replacement_characters": 1, "private_use_characters": 2,
            "noncharacters": 1, "control_characters": 1,
        })
        self.assertTrue(doc.closed)

    def test_font_warnings_are_captured_and_display_settings_restored(self):
        module, _ = fake_pymupdf([TEXT], warning="font encoding could not be resolved\nxref repaired")
        with patch.dict(sys.modules, {"pymupdf": module}):
            _, _, diagnostic = evidence.extract(self.pdf)
        self.assertEqual(diagnostic["warning_count"], 2)
        self.assertTrue(diagnostic["font_encoding_warning"])
        self.assertEqual(diagnostic["xref_recovery_warnings"], 1)
        self.assertTrue(module.TOOLS.show_warnings)
        self.assertTrue(module.TOOLS.show_errors)

    def test_failure_closes_document_and_restores_display_settings(self):
        module, doc = fake_pymupdf([TEXT], error=RuntimeError("fixture"))
        with patch.dict(sys.modules, {"pymupdf": module}):
            with self.assertRaisesRegex(evidence.EvidenceError, "pymupdf_extraction_failed:RuntimeError"):
                evidence.extract(self.pdf, "pymupdf")
        self.assertTrue(doc.closed)
        self.assertTrue(module.TOOLS.show_warnings)
        self.assertTrue(module.TOOLS.show_errors)

    def test_unknown_numeric_characters_never_become_clean_quotes(self):
        for char in ("\ufffd", "\uffff", "\ue001", "\U000f0001", "\x00"):
            with self.subTest(char=repr(char)):
                db = self.root / ("quote-" + str(ord(char)) + ".sqlite")
                raw = TEXT + f"本合同赔付金额为2{char}0元。"
                with patch.object(evidence, "_extract_with_pymupdf", return_value=(
                        [raw], "pymupdf-fixture", evidence.extraction_diagnostics([raw], "pymupdf", []))):
                    evidence.index_sources(db, self.manifest)
                with evidence.database(db) as conn:
                    self.assertFalse(evidence.verify_quote(conn, "D01", 1, "本合同赔付金额为20元。")["valid"])
                    original = evidence.verify_quote(conn, "D01", 1, f"本合同赔付金额为2{char}0元。")
                    self.assertTrue(original["valid"])
                    self.assertTrue(original["requires_visual_review"])
                    self.assertEqual(evidence.page_data(conn, "D01", 1)["text"], raw)

    def test_clean_text_and_layout_whitespace_remain_usable(self):
        self.index_mocked([TEXT + "\n金额小于等于5000元时按60.5%给付。"])
        with evidence.database(self.db) as conn:
            result = evidence.verify_quote(conn, "D01", 1, "本 合同\n不承担既往症导致的医疗费用")
            self.assertTrue(result["valid"])
            self.assertFalse(result["requires_visual_review"])
            self.assertFalse(evidence.verify_quote(conn, "D01", 1, "本合同承担既往症导致的医疗费用")["valid"])
            self.assertFalse(evidence.verify_quote(conn, "D01", 1, "金额大于等于5000元时按60.5%给付。")["valid"])
            self.assertFalse(evidence.verify_quote(conn, "D01", 1, "金额小于等于5000元时按605%给付。")["valid"])

    def test_one_unknown_character_marks_long_page_for_visual_review(self):
        for char in ("\ufffd", "\uffff", "\ue001", "\U000f0001", "\x00", "\ufdd0"):
            with self.subTest(char=repr(char)):
                self.assertEqual(evidence.quality(TEXT * 20 + char), "needs_visual")
        self.assertEqual(evidence.quality(TEXT + "\n\t "), "text")
        self.assertEqual(evidence.quality(""), "no_text")
        self.assertEqual(evidence.quality("短页"), "low_text")

    def test_legacy_raw_pages_get_stricter_read_time_quality_without_write(self):
        self.index_mocked([TEXT + "\ufffd"])
        with evidence.database(self.db, write=True) as conn, conn:
            conn.execute("UPDATE pages SET quality='text'")
        before = self.db.read_bytes()
        with evidence.database(self.db) as conn:
            self.assertEqual(evidence.page_data(conn, "D01", 1)["quality"], "needs_visual")
            self.assertEqual(evidence.search(conn, "保证续保")["results"][0]["quality"], "needs_visual")
            self.assertEqual(evidence.check(conn)["pages_needing_visual_review"], 1)
        self.assertEqual(before, self.db.read_bytes())

    def test_font_warning_is_not_relaxed_by_clean_text(self):
        diagnostics = evidence.extraction_diagnostics([TEXT], "pymupdf", ["font encoding warning"])
        with patch.object(evidence, "_extract_with_pymupdf", return_value=([TEXT], "pymupdf-fixture", diagnostics)):
            evidence.index_sources(self.db, self.manifest)
        with evidence.database(self.db) as conn:
            self.assertEqual(evidence.page_data(conn, "D01", 1)["quality"], "needs_visual")

    def test_existing_index_facts_and_reviews_survive_backend_change(self):
        self.index_mocked([TEXT], "pypdf")
        with evidence.database(self.db, write=True) as conn:
            evidence.add_facts(conn, [{"id": "F001", "policy_id": "P001", "field": "exclusion",
                "period": "fixture", "value": "既往症",
                "citation": {"source_id": "D01", "page": 1, "quote": QUOTE}}])
            evidence.review(conn, "F001", 0, "confirm", "assistant", "核读虚构测试材料")
            before = evidence.audit(conn, "F001")
        with patch.object(evidence, "_extract_with_pymupdf") as primary:
            result = evidence.index_sources(self.db, self.manifest, "pymupdf")
        primary.assert_not_called()
        self.assertEqual(result["reused_documents"], 1)
        self.assertEqual(result["extractors"], {"pypdf-fixture": 1})
        self.assertTrue(result["existing_extractions_preserved"])
        with evidence.database(self.db) as conn:
            self.assertEqual(evidence.audit(conn, "F001"), before)
            self.assertEqual(evidence.page_data(conn, "D01", 1)["extractor"], "pypdf-fixture")
            self.assertTrue(evidence.check(conn)["ok"])

    def test_txt_documents_need_neither_pdf_package(self):
        text = self.root / "fictional.txt"
        text.write_text(TEXT, encoding="utf-8")
        with patch.dict(sys.modules, {"pymupdf": None, "pypdf": None}):
            self.assertEqual(evidence.extract(text)[0], [TEXT])

    def test_invalid_backend_fails_before_creating_database(self):
        with self.assertRaisesRegex(evidence.EvidenceError, "invalid_pdf_backend"):
            evidence.index_sources(self.db, self.manifest, "unknown")
        self.assertFalse(self.db.exists())

    def test_real_pdf_keeps_physical_page_order_with_each_available_backend(self):
        labels = write_pdf(self.pdf)
        backends = [b for b in ("pypdf", "pymupdf") if importlib.util.find_spec(b)]
        if not backends:
            self.skipTest("No PDF backend installed")
        for backend in backends:
            with self.subTest(backend=backend):
                pages, extractor, diagnostic = evidence.extract(self.pdf, backend)
                self.assertEqual(len(pages), 3)
                self.assertTrue(extractor.startswith(backend + "-"))
                for page, label in zip(pages, labels):
                    self.assertIn(label, page)
                self.assertTrue(diagnostic["raw_text_preserved"])

    def test_cli_pdf_index_outputs_json_and_reports_backend(self):
        write_pdf(self.pdf)
        backend = next((b for b in ("pymupdf", "pypdf") if importlib.util.find_spec(b)), None)
        if backend is None:
            self.skipTest("No PDF backend installed")
        command = [sys.executable, "-B", str(Path(evidence.__file__).resolve()), "index",
                   "--db", str(self.db), "--manifest", str(self.manifest), "--pdf-backend", backend]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["requested_pdf_backend"], backend)
        self.assertEqual(result["counts"]["pages"], 3)
        self.assertTrue(all(k.startswith(backend + "-") for k in result["extractors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
