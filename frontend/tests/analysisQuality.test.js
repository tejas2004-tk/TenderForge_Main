import assert from "node:assert/strict";
import test from "node:test";
import { deriveDisplayRequirements, displayValueSupported } from "../src/analysisQuality.js";

test("exact duplicates merge safely and preserve evidence without mutating originals", () => {
  const original = [
    {
      _id: "a",
      category: "Eligibility Requirements",
      requirement_title: "Annual turnover",
      description: "The bidder must demonstrate annual turnover.",
      requirement_value: "10 crore",
      requirement_status: "mandatory",
      source_page: 4,
      evidence_text: "The bidder must have annual turnover of 10 crore.",
    },
    {
      _id: "b",
      category: "Eligibility Requirements",
      requirement_title: "Annual turnover",
      description: "The bidder must demonstrate annual turnover.",
      requirement_value: "10 crore",
      requirement_status: "mandatory",
      source_page: 4,
      source_evidence: [{ source_page: 4, evidence_text: "Annual turnover of 10 crore is required." }],
    },
  ];
  const snapshot = JSON.stringify(original);
  const derived = deriveDisplayRequirements(original);

  assert.equal(derived.length, 1);
  assert.equal(derived[0].display_merged_count, 2);
  assert.deepEqual(derived[0].display_source_ids, ["a", "b"]);
  assert.equal(derived[0].source_evidence.length, 2);
  assert.equal(JSON.stringify(original), snapshot);
  assert.deepEqual(deriveDisplayRequirements(derived), derived);
});

test("different thresholds, deadlines, and conditions are never merged", () => {
  const derived = deriveDisplayRequirements([
    {
      requirement_title: "Past experience",
      description: "The bidder must have experience.",
      requirement_value: "5 years",
      requirement_status: "mandatory",
      applicability_condition: "",
      source_evidence: [{ source_page: 2, evidence_text: "Five years of experience is required." }],
    },
    {
      requirement_title: "Past experience",
      description: "The bidder must have experience.",
      requirement_value: "10 years",
      requirement_status: "mandatory",
      applicability_condition: "",
      source_evidence: [{ source_page: 3, evidence_text: "Ten years of experience is required." }],
    },
    {
      requirement_title: "Past experience",
      description: "The bidder must have experience.",
      requirement_value: "5 years",
      requirement_status: "mandatory",
      applicability_condition: "For national bidders",
      source_evidence: [{ source_page: 4, evidence_text: "Five years of experience is required for national bidders." }],
    },
  ]);
  assert.equal(derived.length, 3);
});

test("unsupported extracted values are flagged but findings are retained", () => {
  const derived = deriveDisplayRequirements([{
    requirement_title: "EMD amount",
    description: "The bidder must submit earnest money.",
    requirement_value: "INR 5 lakh",
    requirement_status: "mandatory",
    source_evidence: [{ source_page: 7, evidence_text: "Earnest money of INR 2 lakh shall be submitted." }],
  }]);
  assert.equal(derived.length, 1);
  assert.equal(derived[0].validation_status, "needs_review");
  assert.ok(derived[0].validation_reasons.some((reason) => reason.includes("extracted value")));
  assert.equal(displayValueSupported("INR 5 lakh", [{ evidence_text: "INR 2 lakh" }]), false);
});

test("missing descriptions receive a readable fallback from existing text", () => {
  const derived = deriveDisplayRequirements([{
    requirement_title: "GST certificate",
    description: "",
    requirement_value: "Valid GST certificate",
    requirement_status: "mandatory",
    source_evidence: [{ source_page: 8, evidence_text: "A valid GST certificate shall be submitted." }],
  }]);
  assert.match(derived[0].display_description, /GST certificate|Valid GST certificate/);
  assert.equal(derived[0].description, "");
});

test("uncertain status and empty evidence remain visible and require review", () => {
  const derived = deriveDisplayRequirements([{
    requirement_title: "Submission document",
    description: "Submit the signed form.",
    requirement_status: "unknown",
    requirement_value: "",
  }]);
  assert.equal(derived.length, 1);
  assert.equal(derived[0].validation_status, "needs_review");
  assert.equal(derived[0].source_evidence.length, 0);
});

test("long requirement text and categories are retained safely", () => {
  const longText = "The bidder shall provide ".repeat(40) + "documented evidence.";
  const derived = deriveDisplayRequirements([{
    category: "technical requirements",
    requirement_title: longText,
    description: "",
    requirement_status: "mandatory",
  }]);
  assert.equal(derived[0].requirement_title, longText);
  assert.equal(derived[0].category, "Technical Requirements");
});
