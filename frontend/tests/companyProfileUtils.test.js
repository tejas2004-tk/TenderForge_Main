import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { formatEvidenceWarning, getGeneralProfileChecklist, getTenderCategoryView } from "../src/companyProfileUtils.js";

test("extraction warning objects render as text instead of crashing the tender card", () => {
  const warning = {
    confidence: 0.3,
    message: "The source marked this finding as uncertain.",
    source_page: 6,
    evidence_text: "PAN certificate is requested.",
  };

  assert.throws(
    () => renderToStaticMarkup(React.createElement("li", null, warning)),
    /Objects are not valid as a React child/,
  );
  const markup = renderToStaticMarkup(
    React.createElement("li", null, formatEvidenceWarning(warning)),
  );
  assert.match(markup, /The source marked this finding as uncertain/);
  assert.match(markup, /Source page 6/);
  assert.match(markup, /PAN certificate is requested/);
});

test("incomplete or unsaved General Information keeps Tender Specific locked", () => {
  const emptyChecklist = getGeneralProfileChecklist({ legal_name: "", supporting_documents: [] }, true);
  assert.equal(emptyChecklist.complete, false);
  assert.equal(emptyChecklist.completed, 0);
  assert.equal(emptyChecklist.missing.length, 3);

  const unsavedChecklist = getGeneralProfileChecklist({
    legal_name: "Acme Ltd",
    registration_number: "REG-123",
    supporting_documents: [{ name: "Certificate of Incorporation.pdf" }],
  }, false);
  assert.equal(unsavedChecklist.complete, false);
  assert.ok(unsavedChecklist.missing.includes("Save your General Information changes"));

  const savedChecklist = getGeneralProfileChecklist({
    legal_name: "Acme Ltd",
    registration_number: "REG-123",
    supporting_documents: [{ name: "Certificate of Incorporation.pdf" }],
  }, true);
  assert.equal(savedChecklist.complete, true);
  assert.equal(savedChecklist.completed, 3);
});

test("category views filter cards and count completion without mutating shared tender data", () => {
  const cards = [
    { requirement_id: "a", tender_category: "Eligibility & Company Documents", current_status: "completed" },
    { requirement_id: "b", tender_category: "Financial Requirements", current_status: "missing" },
  ];
  const before = JSON.stringify(cards);
  const eligibility = getTenderCategoryView(cards, "Eligibility & Company Documents");
  const financial = getTenderCategoryView(cards, "Financial Requirements");
  assert.deepEqual(eligibility.cards.map((card) => card.requirement_id), ["a"]);
  assert.deepEqual([eligibility.completed, eligibility.total], [1, 1]);
  assert.deepEqual(financial.cards.map((card) => card.requirement_id), ["b"]);
  assert.deepEqual([financial.completed, financial.total], [0, 1]);
  assert.equal(JSON.stringify(cards), before);
});
