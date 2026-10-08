"""Verify provenance, cache recovery, page references, and local retrieval."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autocst import manual


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "reference.pdf"
        self.pdf.write_bytes(b"fake stable PDF source")
        self.cache = self.root / "cache"
        self.extractor = patch.object(manual, "_extract_pages", return_value=iter([
            "CST Studio Suite 2025. Overview.",
            "Python automation via cst.interface. A solver can start a simulation.",
            "VBA macros and geometry modeling use History commands.",
        ]))
        self.mock_extract = self.extractor.start()
        self.addCleanup(self.extractor.stop)

    def test_index_preserves_pdf_and_proves_source_hash(self):
        before = self.pdf.read_bytes()
        info = manual.build_index(self.pdf, self.cache)
        self.assertEqual(info["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(info["pages"], 3)
        self.assertFalse(info["cached"])
        self.assertEqual(self.pdf.read_bytes(), before)
        cached = manual.build_index(self.pdf, self.cache)
        self.assertTrue(cached["cached"])
        self.mock_extract.assert_called_once()
        json.dumps(info)

    def test_source_replacement_creates_new_hash_namespace(self):
        first = manual.build_index(self.pdf, self.cache)
        self.pdf.write_bytes(b"replacement document")
        self.mock_extract.return_value = iter(["A different manual."])
        second = manual.build_index(self.pdf, self.cache)
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertNotEqual(first["index_path"], second["index_path"])
        self.assertTrue(Path(first["index_path"]).exists())
        self.assertEqual(second["pages"], 1)

    def test_modified_cache_rebuilds_instead_of_serving_wrong_pages(self):
        info = manual.build_index(self.pdf, self.cache)
        Path(info["index_path"]).write_text('{"page": 999, "text": "false evidence"}\n', encoding="utf-8")
        self.mock_extract.return_value = iter(["Recovered authoritative content."])
        result = manual.read_pages(self.pdf, self.cache, 1, 1)
        self.assertEqual(result["pages"][0]["text"], "Recovered authoritative content.")
        self.assertEqual(self.mock_extract.call_count, 2)

    def test_page_ranges_are_inclusive_physical_pages(self):
        result = manual.read_pages(self.pdf, self.cache, 2, 3)
        self.assertEqual([page["page"] for page in result["pages"]], [2, 3])
        for page in result["pages"]:
            self.assertEqual(page["source_sha256"], result["source_sha256"])
        with self.assertRaises(ValueError):
            manual.read_pages(self.pdf, self.cache, 0, 1)
        with self.assertRaises(ValueError):
            manual.read_pages(self.pdf, self.cache, 3, 2)
        with self.assertRaises(ValueError):
            manual.read_pages(self.pdf, self.cache, 1, 4)

    def test_search_finds_api_name_and_chinese_intent(self):
        api = manual.search_manual("cst.interface", self.pdf, self.cache)
        self.assertEqual(api["results"][0]["page"], 2)
        self.assertIn("cst.interface", api["results"][0]["snippet"])
        query = manual.search_manual("建模", self.pdf, self.cache, limit=1)
        self.assertEqual(query["results"][0]["page"], 3)
        self.assertEqual(query["results"][0]["source_sha256"], api["source_sha256"])
        json.dumps(query, ensure_ascii=False)

    def test_search_does_not_match_com_inside_component(self):
        self.mock_extract.return_value = iter(["component geometry", "Use the COM interface."])
        result = manual.search_manual("COM", self.pdf, self.cache)
        self.assertEqual([page["page"] for page in result["results"]], [2])

    def test_snippet_shows_actual_match_not_partial_word(self):
        self.mock_extract.return_value = iter(["component geometry " * 100 + "Use the COM interface."])
        result = manual.search_manual("COM", self.pdf, self.cache)
        self.assertIn("COM interface", result["results"][0]["snippet"])

    def test_source_change_during_extraction_rejects_index(self):
        def changing(_):
            yield "original content"
            self.pdf.write_bytes(b"source changed")
        self.mock_extract.side_effect = changing
        with self.assertRaisesRegex(manual.ManualError, "changed during indexing"):
            manual.build_index(self.pdf, self.cache)
        self.assertFalse(list(self.cache.rglob("metadata.json")))
        self.assertFalse(list(self.cache.rglob("pages.jsonl")))

    def test_bad_queries_and_missing_source_fail_clearly(self):
        for query in ("", "   ", "..."):
            with self.assertRaises(ValueError):
                manual.search_manual(query, self.pdf, self.cache)
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(ValueError):
                manual.search_manual("Python", self.pdf, self.cache, limit=limit)
        with self.assertRaises(manual.ManualError):
            manual.build_index(self.root / "missing.pdf", self.cache)

    def test_extraction_failure_leaves_no_completed_index(self):
        def broken(_):
            yield "first page"
            raise manual.ManualError("failed halfway")
        self.mock_extract.side_effect = broken
        with self.assertRaises(manual.ManualError):
            manual.build_index(self.pdf, self.cache)
        self.assertFalse(list(self.cache.rglob("metadata.json")))
        self.assertFalse(list(self.cache.rglob("pages.jsonl")))
        self.assertFalse(list(self.cache.rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
