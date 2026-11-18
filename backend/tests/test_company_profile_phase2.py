import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from bson import ObjectId

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import company_profile_service as service
from services import tender_service


class MemoryProfiles:
    def __init__(self, row):
        self.row = row

    def create_index(self, *args, **kwargs):
        return None

    def find_one(self, query):
        if self.row.get("owner_user_id") == query.get("owner_user_id"):
            return dict(self.row)
        return None

    def update_one(self, query, update, upsert=False):
        for key, value in update.get("$set", {}).items():
            self.row[key] = value
        for key, value in update.get("$setOnInsert", {}).items():
            self.row.setdefault(key, value)
        return SimpleNamespace(matched_count=1)


class CompanyProfilePhase2Tests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()

    def test_array_entries_are_validated_and_sanitized(self):
        entries = service._validate_entries(
            {"past_projects": [{
                "project_name": "  Bridge work  ",
                "client_department": "Public Works",
                "work_description": "Completed roadway construction",
                "contract_value": "INR 25 lakh",
                "completion_date": "2025-03-31",
                "supporting_document_id": "document-1",
            }]},
            "past_projects",
            service.PAST_PROJECT_FIELDS,
        )
        self.assertEqual(entries[0]["project_name"], "Bridge work")
        self.assertEqual(entries[0]["supporting_document_id"], "document-1")

    def test_omitted_array_entries_preserve_existing_profile_lists(self):
        owner_id = "owner-1"
        existing_projects = [{"project_name": "Existing work", "client_department": "Existing client"}]
        existing_certifications = [{"certification_name": "ISO 9001", "certificate_number": "ISO-123"}]
        profiles = MemoryProfiles({
            "owner_user_id": owner_id,
            "legal_name": "Existing Company",
            "past_projects": existing_projects,
            "certification_entries": existing_certifications,
        })
        database = SimpleNamespace(company_profiles=profiles)
        user = {"_id": owner_id, "company_name": "Existing Company"}

        with patch.object(service, "get_database", return_value=database):
            saved = service.save_company_profile(user, {"legal_name": "Updated Company"})

        self.assertEqual(saved["legal_name"], "Updated Company")
        self.assertEqual(profiles.row["past_projects"], existing_projects)
        self.assertEqual(profiles.row["certification_entries"], existing_certifications)

    def test_public_entries_do_not_expose_unexpected_types(self):
        public = service._public_entries([
            {"certification_name": "ISO", "issue_date": "2025-01-01", "unused": "ignored"},
            {"certification_name": 123},
            None,
        ], service.CERTIFICATION_FIELDS)
        self.assertEqual(public[0]["certification_name"], "ISO")
        self.assertEqual(public[1]["certification_name"], "")

    def test_deterministic_matching_satisfied_missing_document_and_review(self):
        profile = {
            "pan_number": "ABCDE1234F",
            "gst_number": "27ABCDE1234F1Z5",
            "years_experience": "8",
            "annual_turnover": "12 crore",
            "supporting_documents": [{"id": "gst-doc", "name": "GST certificate.pdf"}],
            "certification_entries": [{
                "certification_name": "ISO 9001",
                "issuing_authority": "Bureau",
                "certificate_number": "ISO-123",
                "supporting_document_id": "iso-doc",
            }],
            "past_projects": [{"project_name": "Highway", "work_description": "Road construction"}],
        }
        requirements = [
            {"category": "Eligibility Requirements", "requirement_title": "PAN",
             "description": "Submit PAN", "requirement_value": "PAN", "requirement_status": "mandatory",
             "source_page": 2, "source_evidence": [{"source_page": 2, "evidence_text": "PAN required"}],
             "validation_status": "verified"},
            {"category": "Eligibility Requirements", "requirement_title": "Employee count",
             "description": "The bidder must disclose employee count.",
             "requirement_value": "", "requirement_status": "mandatory",
             "validation_status": "verified"},
            {"category": "Required Documents and Certificates", "requirement_title": "GST certificate",
             "description": "Submit a GST certificate.",
             "requirement_value": "", "requirement_status": "mandatory",
             "source_page": 3, "validation_status": "verified"},
            {"category": "Required Documents and Certificates", "requirement_title": "PAN card",
             "description": "Submit a PAN card copy.",
             "requirement_value": "", "requirement_status": "mandatory",
             "source_page": 4, "validation_status": "verified"},
            {"category": "Technical Requirements", "requirement_title": "Complex eligibility",
             "description": "Eligibility applies only if the bidder meets condition A or condition B.",
             "requirement_value": "", "requirement_status": "conditional",
             "validation_status": "verified"},
        ]
        results = service.match_tender_requirements(profile, {"requirements": requirements})
        statuses = [item["match_status"] for item in results]
        self.assertIn("satisfied", statuses)
        self.assertIn("missing_information", statuses)
        self.assertIn("satisfied", statuses)
        self.assertIn("document_needed", statuses)
        self.assertIn("review_required", statuses)

        preserved = results[0]
        self.assertEqual(preserved["source_page"], 2)
        self.assertEqual(preserved["description"], "Submit PAN")
        self.assertEqual(preserved["source_evidence"][0]["evidence_text"], "PAN required")

    def test_certification_experience_turnover_and_project_matching(self):
        profile = {
            "years_experience": "8",
            "annual_turnover": "12 crore",
            "financial_year": "2025-26",
            "past_projects": [{"project_name": "One"}, {"project_name": "Two"}],
            "certification_entries": [{"certification_name": "ISO 9001", "supporting_document_id": "iso-doc"}],
            "supporting_documents": [{"id": "iso-doc", "name": "ISO 9001.pdf"}],
        }
        requirements = [
            {"category": "Required Documents and Certificates", "requirement_title": "ISO 9001 certification",
             "description": "ISO 9001 certification is required.", "requirement_value": "ISO 9001",
             "requirement_status": "mandatory", "validation_status": "verified"},
            {"category": "Experience Requirements", "requirement_title": "Experience",
             "description": "Bidder must have 5 years of experience.", "requirement_value": "5 years",
             "requirement_status": "mandatory", "validation_status": "verified"},
            {"category": "Financial Requirements", "requirement_title": "Annual turnover",
             "description": "Minimum annual turnover of INR 10 crore.", "requirement_value": "10 crore",
             "requirement_status": "mandatory", "validation_status": "verified"},
            {"category": "Experience Requirements", "requirement_title": "Past work",
             "description": "Submit evidence of at least 3 similar projects.", "requirement_value": "3 projects",
             "requirement_status": "mandatory", "validation_status": "verified"},
        ]
        results = service.match_tender_requirements(profile, {"requirements": requirements})
        self.assertEqual([item["match_status"] for item in results],
                         ["satisfied", "satisfied", "satisfied", "missing_information"])

    def test_financial_year_mismatch_is_incomplete_not_satisfaction(self):
        profile = {"annual_turnover": "20 crore", "financial_year": "2024-25"}
        requirement = {"category": "Financial Requirements", "requirement_title": "Annual turnover",
                       "description": "Minimum turnover for FY 2025-26 is 10 crore.",
                       "requirement_value": "10 crore", "requirement_status": "mandatory",
                       "validation_status": "verified"}
        result = service.match_tender_requirements(profile, {"requirements": [requirement]})[0]
        self.assertEqual(result["match_status"], "missing_information")

    def test_weak_text_match_is_missing_information_not_generic_review(self):
        profile = {"services": "civil construction"}
        requirement = {"category": "Technical Requirements", "requirement_title": "Software development",
                       "description": "The bidder must provide software development services.",
                       "requirement_value": "", "requirement_status": "mandatory",
                       "validation_status": "verified"}
        result = service.match_tender_requirements(profile, {"requirements": [requirement]})[0]
        self.assertEqual(result["match_status"], "missing_information")

    def test_tender_ownership_is_enforced_before_matching(self):
        tender_id = str(ObjectId())
        fake_collection = SimpleNamespace(find_one=lambda query: None)
        user = {"_id": ObjectId()}

        with patch.object(tender_service, "_collection", return_value=fake_collection):
            with self.assertRaises(tender_service.TenderError) as caught:
                tender_service._find_owned_tender(user, tender_id)

        self.assertEqual(caught.exception.status_code, 404)

    def test_tender_response_completion_requires_valid_input(self):
        self.assertTrue(service._response_is_complete(
            "missing_information", "text", {"value": "Signed declaration"}, [],
        ))
        self.assertFalse(service._response_is_complete(
            "missing_information", "text", {"value": ""}, [],
        ))
        self.assertTrue(service._response_is_complete(
            "document_needed", "document", {}, [{"id": "doc"}],
        ))
        self.assertFalse(service._response_is_complete(
            "document_needed", "document", {}, [],
        ))
        self.assertFalse(service._response_is_complete(
            "review_required", "confirmation", {"confirmed": True}, [],
        ))
        self.assertFalse(service._response_is_complete(
            "review_required", "confirmation", {"confirmed": False}, [],
        ))

    def test_tender_response_document_signature_validation(self):
        self.assertTrue(service._content_matches_type("pdf", b"%PDF-1.7 test"))
        self.assertFalse(service._content_matches_type("pdf", b"MZ executable"))
        self.assertTrue(service._content_matches_type("png", b"\x89PNG\r\n\x1a\n"))

    def test_phase4_classification_excludes_technical_and_informational(self):
        matches = [
            {"requirement_id": "tech", "category": "Technical Requirements",
             "requirement_title": "Print resolution", "description": "Minimum 1200 dpi resolution",
             "requirement_value": "1200 dpi", "requirement_status": "mandatory",
             "match_status": "missing_information"},
            {"requirement_id": "info", "category": "Tender Overview",
             "requirement_title": "Submission deadline", "description": "Submit through the portal",
             "requirement_value": "", "requirement_status": "informational",
             "match_status": "missing_information"},
            {"requirement_id": "elig", "category": "Eligibility Requirements",
             "requirement_title": "PAN registration", "description": "Provide PAN registration",
             "requirement_value": "", "requirement_status": "mandatory",
             "match_status": "missing_information"},
        ]
        cards, excluded = service.classify_and_group_tender_requirements(matches)
        self.assertEqual(excluded["technical_compliance"], 1)
        self.assertEqual(excluded["informational"], 1)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["tender_category"], "Eligibility & Company Documents")

    def test_phase4_duplicate_grouping_preserves_different_thresholds_and_evidence(self):
        matches = [
            {"requirement_id": "a", "category": "Financial Requirements",
             "requirement_title": "Annual turnover", "description": "Annual turnover statement",
             "requirement_value": "10 crore", "requirement_status": "mandatory",
             "applicability_condition": "FY 2025-26",
             "match_status": "missing_information",
             "source_evidence": [{"source_page": 4, "evidence_text": "10 crore"}]},
            {"requirement_id": "b", "category": "Financial Requirements",
             "requirement_title": "Audited turnover", "description": "Audited turnover statement",
             "requirement_value": "10 crore", "requirement_status": "mandatory",
             "applicability_condition": "FY 2025-26",
             "match_status": "missing_information",
             "source_evidence": [{"source_page": 5, "evidence_text": "Audited turnover 10 crore"}]},
            {"requirement_id": "c", "category": "Financial Requirements",
             "requirement_title": "Annual turnover", "description": "Annual turnover statement",
             "requirement_value": "20 crore", "requirement_status": "mandatory",
             "applicability_condition": "FY 2025-26",
             "match_status": "missing_information",
             "source_evidence": [{"source_page": 6, "evidence_text": "20 crore"}]},
        ]
        cards, _ = service.classify_and_group_tender_requirements(matches)
        self.assertEqual(len(cards), 1)
        financial = cards[0]
        self.assertEqual(set(financial["original_requirement_ids"]), {"a", "b", "c"})
        self.assertEqual(len(financial["source_evidence"]), 3)
        self.assertEqual(len(financial["financial_subconditions"]), 2)

    def test_tender_response_upsert_is_requirement_unique_and_updateable(self):
        class MemoryResponseCollection:
            def __init__(self):
                self.rows = []
                self.create_index = lambda *args, **kwargs: None

            def find_one(self, query):
                return next((dict(row) for row in self.rows
                             if all(row.get(key) == value for key, value in query.items())), None)

            def find(self, query):
                return [dict(row) for row in self.rows
                        if all(row.get(key) == value for key, value in query.items())]

            def update_one(self, query, update, upsert=False):
                row = next((row for row in self.rows
                            if all(row.get(key) == value for key, value in query.items())), None)
                if row is None:
                    row = dict(query)
                    self.rows.append(row)
                for key, value in update.get("$set", {}).items():
                    row[key] = value
                for key, value in update.get("$setOnInsert", {}).items():
                    row.setdefault(key, value)
                return SimpleNamespace(matched_count=1)

        tender = {"_id": ObjectId(), "filename": "Tender.pdf"}
        user = {"_id": ObjectId(), "company_name": "Test Company"}
        match = {
            "requirement_id": "requirement-1",
            "match_status": "missing_information",
            "suggested_input_type": "text",
        }
        collection = MemoryResponseCollection()
        with patch.object(service, "_load_actionable_tender_data",
                          return_value=(tender, {"status": "completed"}, [match], {})), \
             patch.object(service, "_tender_response_collection", return_value=collection):
            service.save_tender_response(user, str(tender["_id"]), {
                "requirement_id": "requirement-1",
                "response_data": {"value": "First response"},
            })
            service.save_tender_response(user, str(tender["_id"]), {
                "requirement_id": "requirement-1",
                "response_data": {"value": "Updated response"},
            })

        self.assertEqual(len(collection.rows), 1)
        self.assertEqual(collection.rows[0]["response_data"]["value"], "Updated response")


if __name__ == "__main__":
    unittest.main()
