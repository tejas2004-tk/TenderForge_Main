const CATEGORIES = [
  "Tender Overview", "Eligibility Requirements", "Financial Requirements",
  "Technical Requirements", "Product and Equipment Specifications", "Experience Requirements",
  "Required Documents and Certificates", "EMD and Tender Fees", "Important Dates and Deadlines",
  "Contractual Terms", "Procurement Policies", "Other Mandatory Bidder Conditions",
];

const STATUSES = new Set(["mandatory", "conditional", "optional", "informational", "unclear"]);
const STATUS_ALIASES = new Map([
  ["required", "mandatory"], ["must", "mandatory"], ["mandatory", "mandatory"],
  ["conditional", "conditional"], ["condition", "conditional"],
  ["optional", "optional"], ["informational", "informational"], ["information", "informational"],
  ["unclear", "unclear"], ["unknown", "unclear"],
]);

const normalize = (value) => String(value ?? "").normalize("NFKC").toLocaleLowerCase()
  .replace(/[’‘]/g, "'").replace(/[^\p{L}\p{N}%]+/gu, " ").trim().replace(/\s+/g, " ");

function categoryValue(value) {
  const key = normalize(value);
  return CATEGORIES.find((category) => normalize(category) === key) || value;
}

function evidenceFor(item) {
  const sources = Array.isArray(item.source_evidence) ? item.source_evidence : [];
  const collected = sources.filter((source) => source && typeof source === "object")
    .map((source) => ({ ...source }));
  if (typeof item.evidence_text === "string" && item.evidence_text.trim()) {
    collected.push({ source_page: Number.isInteger(item.source_page) ? item.source_page : null,
      evidence_text: item.evidence_text });
  }
  const seen = new Set();
  return collected.filter((source) => {
    const key = `${source.source_page ?? ""}|${normalize(source.evidence_text)}`;
    if (!normalize(source.evidence_text) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function valueAnchors(value) {
  const text = String(value ?? "");
  const numbers = text.match(/\d+(?:[.,]\d+)*(?:\s?%|\s?(?:days?|weeks?|months?|years?|hours?|months?|crores?|lakhs?|lacs?|rupees?|inr|usd))?/gi) || [];
  return numbers.map(normalize).filter(Boolean);
}

function valueSupported(value, sources) {
  const expected = normalize(value);
  if (!expected) return true;
  const evidence = sources.map((source) => normalize(source.evidence_text)).filter(Boolean);
  if (!evidence.length) return false;
  if (evidence.some((text) => text.includes(expected))) return true;
  const anchors = valueAnchors(value);
  if (!anchors.length) return false;
  return evidence.some((text) => anchors.every((anchor) => text.includes(anchor)));
}

function duplicateKey(item, sources) {
  const title = normalize(item.requirement_title);
  const description = normalize(item.description);
  if (!title && !description) return null;
  const evidenceMarkers = sources.map((source) => {
    const text = normalize(source.evidence_text);
    const numbers = valueAnchors(text).sort();
    const qualifiers = text.match(/\b(?:unless|except|only|if|where|subject to|provided that|at least|not less than|minimum|maximum)\b/g) || [];
    return [...numbers, ...qualifiers].sort().join(",");
  }).sort().join("|");
  const constraints = [item.requirement_value, item.applicability_condition,
    item.requirement_status, evidenceMarkers].map(normalize).join("|");
  return [normalize(item.category), title, description, constraints].join("\u001f");
}

/** Build a deterministic display-only view. Source objects are never mutated. */
export function deriveDisplayRequirements(requirements) {
  const groups = new Map();
  for (const original of Array.isArray(requirements) ? requirements : []) {
    if (!original || typeof original !== "object" || Array.isArray(original)) continue;
    const item = { ...original };
    const sources = evidenceFor(original);
    if (original.display_original !== undefined) {
      item.display_original = original.display_original;
    } else {
      item.display_original = {
        ...original,
        source_evidence: sources.map((source) => ({ ...source })),
      };
    }
    item.display_original_ids = original.display_original_ids
      ? [...original.display_original_ids]
      : [original._id].filter((id) => id !== undefined && id !== null);
    const rawStatus = typeof original.requirement_status === "string" ? original.requirement_status : "";
    const normalizedStatus = STATUS_ALIASES.get(normalize(rawStatus))
      || (original.mandatory === true ? "mandatory" : original.mandatory === false ? "optional" : null);
    const reasons = Array.isArray(original.validation_reasons)
      ? original.validation_reasons.filter((reason) => typeof reason === "string") : [];
    let needsReview = original.validation_status !== "verified";
    if (normalizedStatus) item.requirement_status = normalizedStatus;
    else if (rawStatus && !STATUSES.has(rawStatus)) {
      needsReview = true;
      reasons.push("The requirement status is unclear and should be checked against the tender.");
    }
    item.category = categoryValue(original.category || "Other Mandatory Bidder Conditions");
    item.display_description = typeof original.description === "string" && original.description.trim()
      ? original.description.trim()
      : [original.requirement_title, original.requirement_value,
        sources[0]?.evidence_text].find((value) => typeof value === "string" && value.trim())?.trim() || "";
    item.requirement_title = typeof original.requirement_title === "string" && original.requirement_title.trim()
      ? original.requirement_title.trim()
      : item.display_description
        ? item.display_description.slice(0, 120)
        : "Tender condition";

    if (String(original.requirement_value ?? "").trim()
      && !valueSupported(original.requirement_value, sources)) {
      needsReview = true;
      reasons.push("The extracted value could not be confirmed in the cited evidence.");
    }
    item.source_evidence = sources;
    item.validation_status = needsReview ? "needs_review" : "verified";
    item.validation_reasons = [...new Set(reasons)];
    item.display_source_ids = Array.isArray(original.display_source_ids)
      ? [...original.display_source_ids]
      : [original._id].filter((id) => id !== undefined && id !== null);
    item.display_merged_count = Number.isInteger(original.display_merged_count)
      ? original.display_merged_count
      : 1;
    const key = duplicateKey(item, sources);
    if (!key) {
      groups.set(Symbol(), item);
      continue;
    }
    const prior = groups.get(key);
    if (!prior) {
      groups.set(key, item);
      continue;
    }
    const mergedSources = evidenceFor({ source_evidence: [...prior.source_evidence, ...item.source_evidence] });
    prior.source_evidence = mergedSources;
    prior.display_source_ids = [...new Set([...prior.display_source_ids, ...item.display_source_ids])];
    prior.display_original_ids = [...new Set([...(prior.display_original_ids || []), ...(item.display_original_ids || [])])];
    prior.display_original = prior.display_original && item.display_original
      ? [prior.display_original, item.display_original]
      : prior.display_original || item.display_original;
    prior.display_merged_count += 1;
    if (prior.validation_status !== "verified" || item.validation_status !== "verified") {
      prior.validation_status = "needs_review";
    }
    prior.validation_reasons = [...new Set([...prior.validation_reasons, ...item.validation_reasons])];
  }
  return [...groups.values()];
}

export function displayValueSupported(value, sources) {
  return valueSupported(value, Array.isArray(sources) ? sources : []);
}
