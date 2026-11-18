import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import company_profile_service as service


def requirement(title, description="", category="Eligibility Requirements", **extra):
    return {
        "requirement_key": title,
        "category": category,
        "requirement_title": title,
        "description": description,
        "requirement_status": "mandatory",
        "source_page": 2,
        "evidence_text": description or title,
        **extra,
    }


class CompanyProfilePhase4Tests(unittest.TestCase):
    def test_submission_instructions_are_excluded_and_do_not_count(self):
        match = service._base_match(requirement(
            "Two-envelope submission", "The bidder company must use the submission portal before bid opening.",
            "Eligibility Requirements",
        ))
        cards, excluded = service.classify_and_group_tender_requirements([match])
        self.assertEqual(cards, [])
        self.assertEqual(excluded["informational"], 1)

    def test_registration_pan_and_gst_match_saved_general_information(self):
        profile = {
            "registration_number": "U12345DL2020PTC012345",
            "pan_number": "ABCDE1234F",
            "gst_number": "07ABCDE1234F1Z5",
            "supporting_documents": [],
        }
        results = service.match_tender_requirements(profile, {"requirements": [
            requirement("Legal entity registration", "Provide the registration number."),
            requirement("PAN and GST details", "Bidder PAN and GSTIN must be stated."),
        ]})
        self.assertEqual([item["match_status"] for item in results], ["satisfied", "satisfied"])
        cards, _ = service.classify_and_group_tender_requirements(results)
        self.assertEqual(len(cards), 2)
        self.assertEqual({card["tender_category"] for card in cards}, {"Eligibility & Company Documents"})

    def test_missing_registration_requirements_return_reusable_fields_for_action(self):
        result = service.match_tender_requirements({}, {"requirements": [requirement(
            "Legal entity registration type and number", "Provide the entity type and registration number.",
        )]})[0]
        self.assertEqual(result["match_status"], "missing_information")
        self.assertIn("registration_type", result["matching_profile_fields"])
        self.assertIn("registration_number", result["matching_profile_fields"])

    def test_turnover_uses_matching_saved_financial_period_and_threshold(self):
        profile = {
            "turnover_previous_year": "2024-25",
            "turnover_previous_amount": "INR 2.5 crore",
        }
        result = service.match_tender_requirements(profile, {"requirements": [requirement(
            "Average annual turnover", "Minimum turnover of INR 2 crore during FY 2024-25.",
            "Financial Requirements",
        )]})[0]
        self.assertEqual(result["match_status"], "satisfied")

    def test_past_supply_projects_are_matched_by_type_count_and_period(self):
        profile = {"past_projects": [
            {"project_name": "Printer supply", "work_description": "Supply and installation of printers", "completion_date": "2024-06-01"},
            {"project_name": "Printer deployment", "work_description": "Printer supply and installation", "completion_date": "2023-03-01"},
        ]}
        result = service.match_tender_requirements(profile, {"requirements": [requirement(
            "Past supply experience", "At least 2 printer supply projects completed during FY 2023-24 to 2024-25.",
            "Experience Requirements",
        )]})[0]
        self.assertEqual(result["match_status"], "satisfied")

    def test_duplicate_incorporation_cards_group_without_losing_traceability(self):
        requirements = [
            requirement("Certificate of incorporation", "Upload certificate of incorporation.", source_page=2),
            requirement("Company registration proof", "Provide proof of registration.", source_page=4),
        ]
        matches = [service._finish_match(service._base_match(item), "document_needed", "Evidence missing.", document_needed=True, input_type="document") for item in requirements]
        cards, _ = service.classify_and_group_tender_requirements(matches)
        self.assertEqual(len(cards), 1)
        self.assertEqual(set(cards[0]["original_requirement_ids"]), {item["requirement_id"] for item in matches})
        self.assertEqual(cards[0]["source_pages"], [2, 4])
        self.assertEqual(cards[0]["tender_category"], "Eligibility & Company Documents")

    def test_declaration_is_actionable_but_inert_contract_and_specs_are_excluded(self):
        matches = service.match_tender_requirements({}, {"requirements": [
            requirement("Non-blacklisting declaration", "Bidder shall submit a signed declaration confirming not blacklisted."),
            requirement("Warranty clause", "Products carry a three-year warranty.", "Contractual Terms"),
            requirement("Printer resolution", "Printer must support 1200 dpi.", "Technical Requirements"),
            requirement("Printer supply specification", "Bidder must supply a printer with 1200 dpi resolution.", "Product and Equipment Specifications"),
            requirement("Performance security", "Bidder shall furnish performance security.", "Contractual Terms"),
        ]})
        cards, _ = service.classify_and_group_tender_requirements(matches)
        titles = {card["requirement_title"] for card in cards}
        self.assertIn("Non-blacklisting declaration", titles)
        self.assertIn("Performance security", titles)
        self.assertNotIn("Warranty clause", titles)
        self.assertNotIn("Printer resolution", titles)
        self.assertNotIn("Printer supply specification", titles)
        self.assertTrue(all(card["tender_category"] in {
            "Eligibility & Company Documents", "Financial Requirements",
            "Experience & Past Performance", "Certifications & Authorizations",
            "Tender Declarations & Commitments",
        } for card in cards))

    def test_authorised_signatory_spellings_group_and_missing_fields_win(self):
        profile = {"contact_name": "Asha Rao", "contact_role": "Director"}
        matches = service.match_tender_requirements(profile, {"requirements": [
            requirement("Authorised signatory details", "Provide name and designation."),
            requirement("Authorized signatory details", "Provide name, designation and email."),
        ]})
        cards, _ = service.classify_and_group_tender_requirements(matches)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["match_status"], "missing_information")
        self.assertIn("contact_email", cards[0]["matching_profile_fields"])
        self.assertEqual(len(cards[0]["original_requirement_ids"]), 2)

    def test_financial_qualification_groups_related_evidence_but_keeps_distinct_thresholds(self):
        requirements = [
            requirement("Audited statements", "Audited financial statements for last three financial years.", "Financial Requirements"),
            requirement("Minimum annual turnover", "Minimum average turnover of ₹10,00,000 for last three financial years.", "Financial Requirements", requirement_value="₹10,00,000"),
            requirement("Turnover statements", "Certified turnover statements for last three financial years.", "Financial Requirements"),
            requirement("Higher turnover condition", "Minimum turnover of ₹20,00,000 for last three financial years.", "Financial Requirements", requirement_value="₹20,00,000"),
        ]
        matches = [service._finish_match(service._base_match(item), "document_needed", "Financial evidence is missing.", document_needed=True, input_type="document") for item in requirements]
        cards, _ = service.classify_and_group_tender_requirements(matches)
        self.assertEqual(len(cards), 1)
        combined = cards[0]
        self.assertEqual(combined["requirement_title"], "Financial qualification")
        self.assertEqual(len(combined["financial_subconditions"]), 2)
        self.assertEqual({item["requirement_id"] for item in combined["original_requirements"]}, set(combined["original_requirement_ids"]))
        self.assertEqual(combined["financial_subconditions"][1]["period"], "Last 3 financial years")

    def test_turnover_compares_threshold_across_all_required_years_and_requires_evidence(self):
        profile = {
            "financial_year": "2025-26", "annual_turnover": "INR 12,00,000",
            "turnover_current_year": "2024-25", "turnover_current_amount": "INR 11,00,000",
            "turnover_previous_year": "2023-24", "turnover_previous_amount": "INR 10,00,000",
            "supporting_documents": [{"id": "audit", "name": "Audited financial statements.pdf"}],
        }
        item = requirement("Annual turnover", "Minimum average annual turnover of ₹10,00,000 for last 3 financial years; audited statements required.", "Financial Requirements", requirement_value="₹10,00,000")
        result = service.match_tender_requirements(profile, {"requirements": [item]})[0]
        self.assertEqual(result["match_status"], "satisfied")
        profile["turnover_previous_year"] = "2022-23"
        self.assertEqual(service.match_tender_requirements(profile, {"requirements": [item]})[0]["match_status"], "missing_information")

    def test_existing_legacy_answers_remain_reachable_and_group_progress_counts_once(self):
        profile = {"contact_name": "Asha Rao", "contact_role": "Director"}
        matches = service.match_tender_requirements(profile, {"requirements": [
            requirement("Authorised signatory details", "Provide name and designation."),
            requirement("Authorized signatory details", "Provide name, designation and email."),
        ]})
        cards, excluded = service.classify_and_group_tender_requirements(matches)
        old_id = cards[0]["original_requirement_ids"][0]
        second_old_id = cards[0]["original_requirement_ids"][1]
        tender = {"_id": "tender-1", "filename": "test-tender.pdf"}
        saved = [
            {"requirement_id": old_id, "status": "completed", "response_data": {"value": "Asha Rao"}, "documents": [], "updated_at": datetime.now(timezone.utc)},
            {"requirement_id": second_old_id, "status": "completed", "response_data": {"value": "asha@example.com"}, "documents": [], "updated_at": datetime.now(timezone.utc)},
        ]

        class Rows:
            def find(self, query):
                return saved

        with patch.object(service, "_load_actionable_tender_data", return_value=(tender, {"status": "completed"}, cards, excluded)), \
             patch.object(service, "_tender_response_collection", return_value=Rows()):
            workspace = service.get_tender_responses({"_id": "user-1"}, "tender-1")
        self.assertEqual(workspace["progress"], {"total": 1, "completed": 0, "incomplete": 1})
        self.assertIn(workspace["cards"][0]["response_data"]["value"], {"Asha Rao", "asha@example.com"})
        self.assertEqual(len(workspace["cards"][0]["preserved_responses"]), 1)

    def test_uploaded_documents_must_cover_each_grouped_requirement(self):
        requirements = [
            service._finish_match(service._base_match(requirement("PAN certificate", "Upload PAN certificate.")), "document_needed", "Missing PAN evidence.", document_needed=True, input_type="document"),
            service._finish_match(service._base_match(requirement("GST certificate", "Upload GST certificate.")), "document_needed", "Missing GST evidence.", document_needed=True, input_type="document"),
        ]
        self.assertFalse(service._group_documents_complete(requirements, [{"name": "PAN Card.pdf"}]))
        self.assertTrue(service._group_documents_complete(requirements, [{"name": "PAN Card.pdf"}, {"name": "GST Certificate.pdf"}]))

    def test_extraction_uncertainty_is_warning_while_explicit_conflict_requires_review(self):
        uncertain = requirement("PAN certificate", "Provide PAN certificate.", validation_status="needs_review", confidence=0.3, validation_reasons=["The source marked this finding as uncertain."], source_page=6)
        conflicted = requirement("Performance security", "Provide 3% security.", validation_status="needs_review", confidence=0.4, validation_reasons=["Conflicting values were extracted for this requirement."], source_page=7)
        results = service.match_tender_requirements({"pan_number": "ABCDE1234F"}, {"requirements": [uncertain, conflicted]})
        self.assertNotEqual(results[0]["match_status"], "review_required")
        self.assertEqual(results[0]["evidence_warning"]["source_page"], 6)
        self.assertEqual(results[1]["match_status"], "review_required")
        self.assertEqual(results[1]["review_context"]["source_page"], 7)

    def test_pan_certificate_requires_document_even_when_identifier_is_saved(self):
        result = service.match_tender_requirements({"pan_number": "ABCDE1234F", "supporting_documents": []}, {"requirements": [
            requirement("PAN certificate", "Provide PAN certificate.")
        ]})[0]
        self.assertEqual(result["match_status"], "document_needed")
        self.assertIn("PAN certificate", result["reason"])

    def test_duplicate_financial_subconditions_collapse_but_methods_and_periods_remain_distinct(self):
        source = [
            requirement("Audited accounts", "Audited statements for last 3 financial years.", "Financial Requirements"),
            requirement("Audited statements", "Audited statements for last three financial years.", "Financial Requirements", validation_status="needs_review", confidence=0.4),
            requirement("Turnover", "Average turnover INR 10 lakh for FY 2023-24.", "Financial Requirements"),
            requirement("Turnover", "Turnover INR 10 lakh for FY 2024-25.", "Financial Requirements"),
        ]
        matches = [service._finish_match(service._base_match(item), "missing_information", "Missing.") for item in source]
        cards, _ = service.classify_and_group_tender_requirements(matches)
        finance = next(card for card in cards if card["requirement_title"] == "Financial qualification")
        self.assertEqual(len(finance["financial_subconditions"]), 3)
        self.assertEqual(len(finance["financial_subconditions"][0]["requirement_ids"]), 2)
        self.assertEqual({condition["method"] for condition in finance["financial_subconditions"]} & {"average", ""}, {"average", ""})

    def test_performance_security_reason_names_commitment_and_past_supply_explains_projects(self):
        commitment = service.match_tender_requirements({}, {"requirements": [requirement(
            "Performance security", "Bidder shall furnish 3% of PO value within 10 days of PO issue.", "Contractual Terms",
        )]})[0]
        self.assertIn("3%", commitment["reason"])
        self.assertIn("10 days", commitment["reason"])
        experience = service.match_tender_requirements({"past_projects": [{"project_name": "Printer supply", "work_description": "Printer supply", "completion_date": "2024-05-01"}]}, {"requirements": [requirement(
            "Past supply experience", "At least 2 printer supply orders during 2023-24 to 2024-25.", "Experience Requirements",
        )]})[0]
        assessment = experience["experience_assessment"][0]
        self.assertEqual(assessment["required_count"], 2)
        self.assertEqual(assessment["matched_count"], 1)
        self.assertEqual(assessment["projects_considered"][0]["matched"], True)

    def test_past_supply_and_performance_security_are_classified_in_correct_categories(self):
        past_supply = service._base_match(requirement(
            "Past Supply Experience", "At least three similar supply orders are required.", "Eligibility Requirements",
        ))
        performance_security = service._base_match(requirement(
            "Performance Security", "Post performance security after project award.", "Experience Requirements",
        ))
        self.assertEqual(service._user_facing_category(past_supply), "Experience & Past Performance")
        self.assertEqual(service._user_facing_category(performance_security), "Tender Declarations & Commitments")


if __name__ == "__main__":
    unittest.main()
