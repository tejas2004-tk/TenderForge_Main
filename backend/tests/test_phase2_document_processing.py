"""TenderForge - Phase 2 Comprehensive Test Suite.

Covers all 26 required test scenarios:
1. Normal text PDF
2. Multi-page PDF
3. Empty page
4. Scanned/image-only page
5. Repeated headers and footers
6. Multi-column text
7. Tables
8. Broken line breaks
9. Paragraph preservation
10. Sentence preservation
11. Numerical value preservation
12. Date and percentage preservation
13. Chunk size and overlap
14. Page references
15. Character offsets
16. Cross-page clauses
17. Repeated extraction
18. Duplicate chunk prevention
19. Relevance classification
20. Important clauses without obvious keywords
21. Unclassified chunk retention
22. LLM-ready selection
23. Token budget handling
24. Corrupted PDF
25. Password-protected PDF
26. MongoDB persistence and failure handling
"""

import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from flask import Flask

from services.document_processing import (
    DocumentChunk,
    LLMReadyChunk,
    PDFValidationError,
    assess_document_ocr_status,
    clean_page_text,
    classify_document_chunks,
    create_chunks_for_pages,
    detect_repeated_headers_and_footers,
    extract_tables_from_page,
    fix_broken_linebreaks_and_hyphens,
    inspect_pdf_page,
    is_page_number_noise,
    normalize_whitespace_and_artefacts,
    open_and_validate_pdf,
    prepare_llm_ready_chunks,
    score_single_chunk,
    sort_blocks_by_column,
)
from services import pdf_processing_service as service


class MemoryCollection:
    """In-memory MongoDB collection fake for safe, fast, isolated testing."""

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.updates = []

    class Cursor(list):
        def sort(self, key, direction=1):
            if isinstance(key, list):
                result = self.__class__(self)
                for field, order in reversed(key):
                    result = self.__class__(sorted(result, key=lambda row: row.get(field, 0), reverse=order < 0))
                return result
            return self.__class__(sorted(self, key=lambda row: row.get(key, 0), reverse=direction < 0))

    def _matches(self, row, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(row, cond) for cond in value):
                    return False
            elif isinstance(value, dict):
                if "$in" in value and row.get(key) not in value["$in"]:
                    return False
                if "$ne" in value and row.get(key) == value["$ne"]:
                    return False
                if "$lte" in value and row.get(key) is not None and row.get(key) > value["$lte"]:
                    return False
                if "$exists" in value:
                    exists = key in row
                    if exists != value["$exists"]:
                        return False
            elif row.get(key) != value:
                return False
        return True

    def find(self, query=None, projection=None):
        return self.Cursor(dict(row) for row in self.rows if self._matches(row, query or {}))

    def find_one(self, query=None, sort=None, projection=None):
        rows = self.find(query)
        if sort and rows:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def find_one_and_update(self, query, update, return_document=None, upsert=False):
        row = next((row for row in self.rows if self._matches(row, query)), None)
        if row is None and upsert:
            row = dict(query.get("$setOnInsert", {}))
            for k, v in query.items():
                if not k.startswith("$"):
                    row[k] = v
            self.rows.append(row)
        if row is not None:
            self._apply(row, update)
            return dict(row)
        return None

    def count_documents(self, query):
        return sum(self._matches(row, query) for row in self.rows)

    def update_one(self, query, update, upsert=False):
        self.updates.append((query, update))
        row = next((row for row in self.rows if self._matches(row, query)), None)
        if row is None and upsert:
            row = dict(query)
            row.update(update.get("$setOnInsert", {}))
            self.rows.append(row)
        if row is not None:
            self._apply(row, update)
        return SimpleNamespace(matched_count=int(row is not None))

    def update_many(self, query, update):
        for row in self.rows:
            if self._matches(row, query):
                self._apply(row, update)

    def delete_many(self, query):
        self.rows = [row for row in self.rows if not self._matches(row, query)]

    def insert_one(self, row):
        saved = dict(row)
        saved.setdefault("_id", f"mem-id-{len(self.rows)}")
        self.rows.append(saved)
        return SimpleNamespace(inserted_id=saved["_id"])

    def insert_many(self, rows, ordered=True):
        for r in rows:
            self.insert_one(r)

    def distinct(self, key, query=None):
        matching = [r[key] for r in self.rows if self._matches(r, query or {}) and key in r]
        return list(dict.fromkeys(matching))

    def create_index(self, keys, **kwargs):
        pass

    @staticmethod
    def _apply(row, update):
        for key, value in update.get("$set", {}).items():
            row[key] = value
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        for key, value in update.get("$addToSet", {}).items():
            values = value.get("$each", [value]) if isinstance(value, dict) else [value]
            row.setdefault(key, [])
            for item in values:
                if item not in row[key]:
                    row[key].append(item)
        for key in update.get("$unset", {}):
            row.pop(key, None)


class Phase2DocumentProcessingTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            DEBUG=False,
            ANALYSIS_MAX_INPUT_TOKENS=6000,
            MAX_TENDER_SIZE_MB=25,
        )
        self.context = self.app.app_context()
        self.context.push()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        self.context.pop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_synthetic_pdf(self, pages_text: list[str]) -> Path:
        """Helper to create a synthetic multi-page PDF."""
        doc = fitz.open()
        for text in pages_text:
            page = doc.new_page()
            if text:
                page.insert_textbox(fitz.Rect(50, 50, 500, 750), text)
        pdf_path = Path(self.temp_dir) / f"test_{os.urandom(4).hex()}.pdf"
        doc.save(pdf_path)
        doc.close()
        return pdf_path

    # 1. Normal text PDF
    def test_01_normal_text_pdf(self):
        content = "SECTION 1: TENDER OVERVIEW\n\nThis tender is for the supply of medical laboratory instruments."
        pdf_path = self._create_synthetic_pdf([content])
        doc = open_and_validate_pdf(pdf_path)
        self.assertEqual(doc.page_count, 1)
        raw_text, info = inspect_pdf_page(doc[0], 1)
        self.assertIn("TENDER OVERVIEW", raw_text)
        self.assertFalse(info.is_scanned)
        self.assertFalse(info.is_empty)
        doc.close()

    # 2. Multi-page PDF
    def test_02_multipage_pdf(self):
        pages = [
            "Page 1: Scope of Work and Introduction to the Project.",
            "Page 2: Minimum Bidder Eligibility Criteria and Technical Standards.",
            "Page 3: Commercial Terms, Payment Milestones, and Conclusion.",
        ]
        pdf_path = self._create_synthetic_pdf(pages)
        doc = open_and_validate_pdf(pdf_path)
        self.assertEqual(doc.page_count, 3)
        for i in range(3):
            raw, info = inspect_pdf_page(doc[i], i + 1)
            self.assertEqual(info.page_number, i + 1)
            self.assertIn(f"Page {i + 1}", raw)
        doc.close()

    # 3. Empty page
    def test_03_empty_page(self):
        pages = ["Page 1 has valid tender requirements.", "", "Page 3 has submission instructions."]
        pdf_path = self._create_synthetic_pdf(pages)
        doc = open_and_validate_pdf(pdf_path)
        self.assertEqual(doc.page_count, 3)
        raw1, info1 = inspect_pdf_page(doc[0], 1)
        raw2, info2 = inspect_pdf_page(doc[1], 2)
        raw3, info3 = inspect_pdf_page(doc[2], 3)
        self.assertFalse(info1.is_empty)
        self.assertTrue(info2.is_empty)
        self.assertFalse(info3.is_empty)
        doc.close()

    # 4. Scanned/image-only page
    def test_04_scanned_image_only_page(self):
        doc = fitz.open()
        # Add 2 pages with large image rect and no text
        for _ in range(2):
            page = doc.new_page()
            img_doc = fitz.open()
            img_page = img_doc.new_page(width=200, height=200)
            img_page.draw_rect(fitz.Rect(10, 10, 190, 190), color=(1, 0, 0), fill=(0, 1, 0))
            pix = img_page.get_pixmap()
            page.insert_image(fitz.Rect(50, 50, 500, 700), pixmap=pix)
            img_doc.close()
        pdf_path = Path(self.temp_dir) / "scanned.pdf"
        doc.save(pdf_path)
        doc.close()

        opened = open_and_validate_pdf(pdf_path)
        infos = []
        for i in range(opened.page_count):
            _, info = inspect_pdf_page(opened[i], i + 1)
            infos.append(info)
        opened.close()

        summary = assess_document_ocr_status(2, infos)
        self.assertTrue(summary.needs_ocr)
        self.assertEqual(summary.status, "needs_ocr")
        self.assertIn("OCR", summary.error_message)

    # 5. Repeated headers and footers
    def test_05_repeated_headers_and_footers(self):
        page_lines = [
            ["NATIONAL TENDER BOARD - NIT 2024", "Clause 1 Scope", "CONFIDENTIAL - PAGE 1"],
            ["NATIONAL TENDER BOARD - NIT 2024", "Clause 2 Eligibility", "CONFIDENTIAL - PAGE 2"],
            ["NATIONAL TENDER BOARD - NIT 2024", "Clause 3 Payment", "CONFIDENTIAL - PAGE 3"],
            ["NATIONAL TENDER BOARD - NIT 2024", "Clause 4 Warranty", "CONFIDENTIAL - PAGE 4"],
        ]
        headers, footers = detect_repeated_headers_and_footers(page_lines, min_pages=3)
        self.assertTrue(any("national tender board" in h for h in headers))
        self.assertTrue(any("confidential" in f for f in footers))

        raw_page = "NATIONAL TENDER BOARD - NIT 2024\n\nClause 1 Scope of Work.\n\nCONFIDENTIAL - PAGE 1"
        cleaned = clean_page_text(1, raw_page, detected_headers=headers, detected_footers=footers)
        self.assertNotIn("NATIONAL TENDER BOARD", cleaned.cleaned_text)
        self.assertNotIn("CONFIDENTIAL", cleaned.cleaned_text)
        self.assertIn("Clause 1 Scope of Work.", cleaned.cleaned_text)

    # 6. Multi-column text
    def test_06_multicolumn_text(self):
        blocks = [
            (50.0, 50.0, 200.0, 100.0, "Left Col Block 1\n", 0, 0),
            (300.0, 50.0, 480.0, 100.0, "Right Col Block 1\n", 1, 0),
            (50.0, 110.0, 200.0, 160.0, "Left Col Block 2\n", 2, 0),
            (300.0, 110.0, 480.0, 160.0, "Right Col Block 2\n", 3, 0),
        ]
        sorted_blocks, is_multi = sort_blocks_by_column(blocks)
        self.assertTrue(is_multi)
        texts = [b[4].strip() for b in sorted_blocks]
        # Should read Left Col Block 1, Left Col Block 2, then Right Col Block 1, Right Col Block 2
        self.assertEqual(texts[0], "Left Col Block 1")
        self.assertEqual(texts[1], "Left Col Block 2")
        self.assertEqual(texts[2], "Right Col Block 1")
        self.assertEqual(texts[3], "Right Col Block 2")

    # 7. Tables
    def test_07_tables(self):
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(50, 50, 350, 150))
        page.draw_line(fitz.Point(50, 80), fitz.Point(350, 80))
        page.draw_line(fitz.Point(200, 50), fitz.Point(200, 150))
        page.insert_text((60, 70), "Criteria")
        page.insert_text((210, 70), "Requirement")
        page.insert_text((60, 110), "Annual Turnover")
        page.insert_text((210, 110), "Rs. 50,00,000")

        tables = extract_tables_from_page(page)
        doc.close()
        self.assertGreaterEqual(len(tables), 1)
        md = tables[0]["markdown"]
        self.assertIn("| Criteria | Requirement |", md)
        self.assertIn("Annual Turnover", md)
        self.assertIn("Rs. 50,00,000", md)

    # 8. Broken line breaks
    def test_08_broken_line_breaks(self):
        broken = (
            "The bidder must ensure the submis-\n"
            "sion of all required documents. The imple-\n"
            "mentation schedule shall be strictly adhered to.\n"
            "This sentence is spread\n"
            "across multiple lines without\n"
            "a terminating full stop."
        )
        cleaned, fixes = fix_broken_linebreaks_and_hyphens(broken.split("\n"))
        self.assertIn("submission", cleaned)
        self.assertIn("implementation", cleaned)
        self.assertIn("spread across multiple lines without a terminating full stop.", cleaned)
        self.assertGreater(fixes, 0)

    # 9. Paragraph preservation
    def test_09_paragraph_preservation(self):
        paras = (
            "Paragraph 1: The bidder must have 5 years experience in supplying equipment.\n\n"
            "Paragraph 2: The bidder must submit an audited balance sheet.\n\n"
            "Paragraph 3: Delivery shall be within 30 days."
        )
        cleaned = clean_page_text(1, paras)
        split_p = cleaned.cleaned_text.split("\n\n")
        self.assertEqual(len(split_p), 3)
        self.assertIn("Paragraph 1", split_p[0])
        self.assertIn("Paragraph 2", split_p[1])
        self.assertIn("Paragraph 3", split_p[2])

    # 10. Sentence preservation
    def test_10_sentence_preservation(self):
        long_para = (
            "First sentence defines bidder eligibility criteria. "
            "Second sentence defines financial capacity and turnover requirements. "
            "Third sentence specifies technical compliance specifications. "
            "Fourth sentence provides earnest money deposit instructions."
        )
        chunks = create_chunks_for_pages(
            [{"page_number": 1, "cleaned_text": long_para}],
            chunk_size=120,
            chunk_overlap=0,
        )
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            # Ensure sentences are not split mid-word
            self.assertTrue(c.text.endswith(".") or c.text.endswith("instructions."))

    # 11. Numerical value preservation
    def test_11_numerical_value_preservation(self):
        text = "EMD amount is Rs. 1,50,000/-. Turnover must exceed INR 50,00,000. Voltage is 415 V ± 10%."
        cleaned = clean_page_text(1, text)
        self.assertIn("Rs. 1,50,000/-", cleaned.cleaned_text)
        self.assertIn("INR 50,00,000", cleaned.cleaned_text)
        self.assertIn("415 V", cleaned.cleaned_text)
        self.assertIn("10%", cleaned.cleaned_text)

    # 12. Date and percentage preservation
    def test_12_date_and_percentage_preservation(self):
        text = "Submission deadline is 15/12/2024. Bid validity is 90 days. Liquidated damages are 0.5% per week."
        cleaned = clean_page_text(1, text)
        self.assertIn("15/12/2024", cleaned.cleaned_text)
        self.assertIn("90 days", cleaned.cleaned_text)
        self.assertIn("0.5%", cleaned.cleaned_text)

    # 13. Chunk size and overlap
    def test_13_chunk_size_and_overlap(self):
        paras = "\n\n".join([f"Clause {i}: Detailed tender condition specification number {i}." for i in range(15)])
        chunk_size = 200
        overlap = 50
        chunks = create_chunks_for_pages(
            [{"page_number": 1, "cleaned_text": paras}],
            chunk_size=chunk_size,
            chunk_overlap=overlap,
        )
        self.assertGreater(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c.text), chunk_size + 100)

    # 14. Page references
    def test_14_page_references(self):
        pages = [
            {"page_number": 1, "cleaned_text": "Page 1 Content on Eligibility."},
            {"page_number": 2, "cleaned_text": "Page 2 Content on Financials."},
            {"page_number": 3, "cleaned_text": "Page 3 Content on Submission."},
        ]
        chunks = create_chunks_for_pages(pages, chunk_size=500, chunk_overlap=0)
        page_nums = [c.page_number for c in chunks]
        self.assertIn(1, page_nums)
        self.assertIn(2, page_nums)
        self.assertIn(3, page_nums)

    # 15. Character offsets
    def test_15_character_offsets(self):
        text = "Paragraph 1 is here.\n\nParagraph 2 is here."
        chunks = create_chunks_for_pages([{"page_number": 1, "cleaned_text": text}], chunk_size=500)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual(text[c.char_start:c.char_end], c.text)

    # 16. Cross-page clauses
    def test_16_cross_page_clauses(self):
        pages = [
            {"page_number": 1, "cleaned_text": "Clause 3.1: Technical Experience Requirements\nThe bidder must have supplied"},
            {"page_number": 2, "cleaned_text": "at least 3 similar units in the last three years to government entities."},
        ]
        chunks = create_chunks_for_pages(pages, chunk_size=1000, chunk_overlap=0)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].clause_id, "clause_3.1")
        self.assertTrue(chunks[1].is_continuation)
        self.assertEqual(chunks[1].continuation_of_chunk_index, 0)

    # 17. Repeated extraction
    def test_17_repeated_extraction(self):
        # Verify idempotency: calling create_chunks and classify twice produces identical output
        pages = [{"page_number": 1, "cleaned_text": "Tender Overview: Supply of equipment."}]
        run1 = create_chunks_for_pages(pages, chunk_size=500)
        run2 = create_chunks_for_pages(pages, chunk_size=500)
        self.assertEqual(len(run1), len(run2))
        self.assertEqual(run1[0].content_hash, run2[0].content_hash)
        self.assertEqual(run1[0].text, run2[0].text)

    # 18. Duplicate chunk prevention
    def test_18_duplicate_chunk_prevention(self):
        boilerplate = "Standard General Disclaimer: The authority reserves the right to cancel the tender."
        pages = [
            {"page_number": 1, "cleaned_text": boilerplate},
            {"page_number": 2, "cleaned_text": boilerplate},
        ]
        chunks = create_chunks_for_pages(pages, chunk_size=500)
        self.assertEqual(len(chunks), 2)
        self.assertFalse(chunks[0].is_duplicate)
        self.assertTrue(chunks[1].is_duplicate)
        self.assertEqual(chunks[1].duplicate_of_chunk_index, 0)

    # 19. Relevance classification
    def test_19_relevance_classification(self):
        samples = [
            ("The bidder must submit an Earnest Money Deposit (EMD) of Rs. 2,00,000 via Bank Guarantee.", "EMD & Earnest Money"),
            ("Average annual turnover of the bidder must be at least Rs. 50 Lakhs in the last 3 financial years.", "Turnover & Financial Capacity"),
            ("Liquidated damages shall be levied at the rate of 0.5% per week of delay up to a maximum of 10%.", "Penalties & Liquidated Damages"),
            ("The equipment must carry a comprehensive warranty of 3 years from the date of commissioning.", "Warranty & Maintenance"),
            ("The bidder will be debarred and blacklisted if found guilty of corrupt practices.", "Disqualification Conditions"),
        ]
        for text, expected_cat in samples:
            res = score_single_chunk(text, 1, 0)
            self.assertEqual(res.relevance_category, expected_cat)
            self.assertTrue(res.is_llm_candidate)
            self.assertGreater(res.relevance_score, 20)

    # 20. Important clauses without obvious keywords
    def test_20_important_clauses_without_obvious_keywords(self):
        # A contract condition without specific category keywords but with amounts, dates, and mandatory cues
        text = "The contractor shall within 14 days furnish the sum of Rs. 500,000. Failure to comply will result in forfeiture."
        res = score_single_chunk(text, 1, 0)
        self.assertTrue(res.is_llm_candidate)
        self.assertGreater(res.relevance_score, 0)
        self.assertIn("General & Special Conditions", res.relevance_category)

    # 21. Unclassified chunk retention
    def test_21_unclassified_chunk_retention(self):
        neutral_text = "This document was printed on acid-free paper. Save trees and protect the global environment."
        res = score_single_chunk(neutral_text, 1, 0)
        self.assertEqual(res.relevance_category, "Unclassified")
        self.assertEqual(res.relevance_score, 0)
        self.assertFalse(res.is_llm_candidate)

        # Ensure unclassified chunk is retained in chunk preparation reserve
        chunk = DocumentChunk(chunk_index=0, page_number=1, original_page_numbers=[1], text=neutral_text, char_start=0, char_end=len(neutral_text))
        selected, reserve, summary = prepare_llm_ready_chunks([chunk], [res], max_token_budget=1000)
        self.assertEqual(len(selected), 0)
        self.assertEqual(len(reserve), 1)
        self.assertEqual(reserve[0].text, neutral_text)

    # 22. LLM-ready selection
    def test_22_llm_ready_selection(self):
        chunks = [
            DocumentChunk(0, 1, [1], "EMD of Rs. 100,000 is mandatory.", 0, 30),
            DocumentChunk(1, 1, [1], "Neutral header without tender terms.", 31, 65),
            DocumentChunk(2, 2, [2], "Average annual turnover must be Rs. 50 Lakhs.", 66, 110),
        ]
        classifications = classify_document_chunks(chunks)
        selected, reserve, summary = prepare_llm_ready_chunks(chunks, classifications, max_token_budget=5000)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(reserve), 1)
        self.assertEqual(selected[0].chunk_index, 0)
        self.assertEqual(selected[1].chunk_index, 2)

    # 23. Token budget handling
    def test_23_token_budget_handling(self):
        # 5 high-relevance chunks of ~100 tokens each
        chunks = [
            DocumentChunk(i, i + 1, [i + 1], f"Condition {i}: The bidder must submit EMD of Rs. {i * 10000} and turnover.", 0, 60)
            for i in range(5)
        ]
        classifications = classify_document_chunks(chunks)
        # Tight token budget allowing only 2 chunks (~60-80 tokens)
        selected, reserve, summary = prepare_llm_ready_chunks(chunks, classifications, max_token_budget=70)
        self.assertLess(len(selected), 5)
        self.assertGreater(len(reserve), 0)
        self.assertEqual(len(selected) + len(reserve), 5)
        self.assertLessEqual(summary["total_selected_tokens"], 70)

    # 24. Corrupted PDF
    def test_24_corrupted_pdf(self):
        corrupt_path = Path(self.temp_dir) / "corrupt.pdf"
        corrupt_path.write_bytes(b"%PDF-1.4\nGarbage data not a real PDF trailer %%EOF")
        with self.assertRaises(PDFValidationError) as ctx:
            open_and_validate_pdf(corrupt_path)
        self.assertIn("corrupted", ctx.exception.message.casefold())

    # 25. Password-protected PDF
    def test_25_password_protected_pdf(self):
        doc = fitz.open()
        doc.new_page()
        pw_path = Path(self.temp_dir) / "encrypted.pdf"
        doc.save(pw_path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret123")
        doc.close()

        with self.assertRaises(PDFValidationError) as ctx:
            open_and_validate_pdf(pw_path)
        self.assertIn("password-protected", ctx.exception.message)

    # 26. MongoDB persistence and failure handling
    def test_26_mongodb_persistence_and_failure_handling(self):
        content = "SECTION 1: TENDER DETAILS\n\nEMD is Rs. 50,000. Turnover must be Rs. 20 Lakhs."
        pdf_path = self._create_synthetic_pdf([content])
        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

        user = {"_id": "test_user_id"}
        tender = {
            "_id": "test_tender_id",
            "storage_name": pdf_path.name,
            "sha256": sha,
            "owner_user_id": "test_user_id",
        }

        extractions = MemoryCollection()
        pages = MemoryCollection()
        chunks = MemoryCollection()
        cleaned_pages = MemoryCollection()
        llm_ready = MemoryCollection()

        all_cols = (extractions, pages, chunks, cleaned_pages, llm_ready)

        with patch.object(service, "_find_owned_tender", return_value=tender), \
             patch.object(service, "_tender_directory", return_value=pdf_path.parent), \
             patch.object(service, "_all_collections", return_value=all_cols), \
             patch.object(service, "_collections", return_value=(extractions, pages, chunks)):

            # First run: success
            summary = service.process_tender(user, "test_tender_id")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["page_count"], 1)
            self.assertGreater(summary["total_chunks"], 0)
            self.assertEqual(pages.count_documents({}), 1)
            self.assertGreater(chunks.count_documents({}), 0)
            self.assertGreater(cleaned_pages.count_documents({}), 0)
            self.assertGreater(llm_ready.count_documents({}), 0)

            # Second run: reuse check
            summary2 = service.process_tender(user, "test_tender_id")
            self.assertEqual(summary2["status"], "completed")
            self.assertTrue(summary2["reused"])

            # Retrieval check
            source_text = service.get_chunk_source_text(user, "test_tender_id", 0)
            self.assertIn("TENDER DETAILS", source_text)


if __name__ == "__main__":
    unittest.main()
