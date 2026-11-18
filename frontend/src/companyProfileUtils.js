export function formatEvidenceWarning(warning) {
  const parts = [];
  if (warning.message) parts.push(warning.message);
  if (warning.source_page) parts.push(`Source page ${warning.source_page}`);
  if (warning.evidence_text) parts.push(`Evidence: ${warning.evidence_text}`);
  return parts.join(" · ");
}

export function getGeneralProfileChecklist(profile = {}, changesSaved = true) {
  const missing = [];
  if (!String(profile.legal_name || "").trim()) missing.push("Legal company name");

  const hasIdentifier = ["registration_number", "cin_llpin", "pan_number", "gst_number"]
    .some((field) => String(profile[field] || "").trim());
  if (!hasIdentifier) missing.push("At least one company identifier (registration number, CIN/LLPIN, PAN, or GST)");

  const identityDocument = (Array.isArray(profile.supporting_documents) ? profile.supporting_documents : []).some((document) =>
    /registration|incorporat|certificate of incorporation|\bpan\b|\bgst\b|\bcin\b|\bllp(?:in)?\b/i.test(document?.name || ""));
  if (!identityDocument) missing.push("A company identity document (registration, incorporation, PAN, or GST proof)");
  const completed = 3 - missing.length;
  if (!changesSaved) missing.push("Save your General Information changes");

  return { complete: missing.length === 0, missing, completed };
}

export function getTenderCategoryView(cards, category) {
  const categoryCards = (Array.isArray(cards) ? cards : []).filter((card) => card.tender_category === category);
  return {
    cards: categoryCards,
    total: categoryCards.length,
    completed: categoryCards.filter((card) => card.current_status === "completed").length,
  };
}
