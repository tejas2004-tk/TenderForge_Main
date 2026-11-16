"""TenderForge - Document Processing: Python-Based Relevance Classification

Deterministic Python scoring to identify and prioritize chunks relating to:
1. Tender overview and scope of work
2. Eligibility criteria
3. Bidder qualification
4. EMD and earnest money deposit
5. Tender fees and processing fees
6. Turnover and financial capacity
7. Experience and past performance
8. Technical specifications
9. Technical compliance
10. Statutory and legal requirements
11. Important dates and deadlines
12. Submission instructions
13. Required certificates and documents
14. Evaluation and selection criteria
15. Penalties and liquidated damages
16. Security deposits and performance guarantees
17. Contract terms and conditions
18. Payment terms
19. Warranty and maintenance
20. Disqualification conditions
Plus General Conditions (GCC/SCC), Annexures, Schedules, and Criteria Tables.

Safeguard: Never discard unclassified chunks. Preserve all chunks with transparent scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# Entity and numeric pattern regular expressions
_CURRENCY_RE = re.compile(r"(?:rs\.?|inr|₹|\$|usd|eur|€)\s*[\d,]+(?:\.\d+)?", re.I)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%", re.I)
_DATE_RE = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", re.I)
_PERIOD_RE = re.compile(r"\b\d+\s*(?:years?|yrs?|months?|weeks?|days?|hours?)\b", re.I)
_MANDATORY_RE = re.compile(
    r"\b(?:shall|must|mandatory|compulsory|strictly\s+required|will\s+be\s+rejected|disqualified|rejected|non-compliance|forfeiture|failure\s+to\s+comply)\b",
    re.I,
)
_TABLE_ROW_RE = re.compile(r"\|\s*[^|]+\s*\|")

# 20+ Category definitions with strong cues and supporting patterns
CATEGORIES = {
    "Tender Overview & Scope": {
        "strong": [
            r"notice\s+inviting\s+tender|nit|request\s+for\s+proposal|rfp|invitation\s+for\s+bids|ifb",
            r"tender\s+overview|scope\s+of\s+work|project\s+background|brief\s+description\s+of\s+work",
            r"tender\s+reference\s+no|tender\s+id|procurement\s+of",
        ],
        "general": [
            r"employer|procuring\s+entity|client|authority|work\s+description|site\s+location",
        ],
    },
    "Eligibility Criteria": {
        "strong": [
            r"eligibility\s+criteria|pre-?qualification\s+criteria|who\s+can\s+bid|eligible\s+bidders",
            r"basic\s+eligibility|minimum\s+eligibility|joint\s+venture\s+criteria|consortium\s+eligibility",
        ],
        "general": [
            r"bidder\s+shall\s+be|applicant\s+must|sole\s+proprietorship|partnership\s+firm|company\s+registered",
        ],
    },
    "Bidder Qualification": {
        "strong": [
            r"bidder\s+qualification|qualification\s+requirements|technical\s+capability|managerial\s+capacity",
            r"authorized\s+partner|oem\s+authorization|manufacturer\s+authorization\s+form|maf",
        ],
        "general": [
            r"personnel\s+capability|key\s+staff|equipment\s+capability|competence",
        ],
    },
    "EMD & Earnest Money": {
        "strong": [
            r"earnest\s+money\s+deposit|emd|bid\s+security|bid\s+security\s+declaration",
            r"emd\s+exemption|msme\s+exemption|nsic\s+exemption",
        ],
        "general": [
            r"bank\s+guarantee\s+for\s+emd|demand\s+draft|fdr\s+towards\s+emd|security\s+deposit",
        ],
    },
    "Tender Fees & Charges": {
        "strong": [
            r"tender\s+fee|cost\s+of\s+tender\s+document|document\s+fee|processing\s+fee|portal\s+charges",
            r"non-?refundable\s+fee",
        ],
        "general": [
            r"e-tender\s+processing|dd\s+in\s+favour\s+of|payment\s+of\s+fee",
        ],
    },
    "Turnover & Financial Capacity": {
        "strong": [
            r"annual\s+turnover|average\s+annual\s+turnover|financial\s+capacity|net\s+worth|solvency\s+certificate",
            r"audited\s+balance\s+sheet|profit\s+and\s+loss|working\s+capital|financial\s+standing",
        ],
        "general": [
            r"chartered\s+accountant|ca\s+certificate|udin|last\s+3\s+financial\s+years|financial\s+year",
        ],
    },
    "Experience & Past Performance": {
        "strong": [
            r"past\s+experience|similar\s+works?|experience\s+criteria|completion\s+certificates?",
            r"work\s+orders?|supply\s+experience|client\s+certificates?|track\s+record",
        ],
        "general": [
            r"satisfactory\s+performance|value\s+of\s+completed\s+work|execution\s+of\s+work|similar\s+nature",
        ],
    },
    "Technical Specifications": {
        "strong": [
            r"technical\s+specifications?|bill\s+of\s+quantities|boq|schedule\s+of\s+requirements",
            r"technical\s+parameters?|equipment\s+specifications?|make\s+and\s+model",
        ],
        "general": [
            r"operating\s+voltage|frequency|capacity|dimension|tolerance|standards?\s*:\s*(?:iso|bis|iec|astm)",
        ],
    },
    "Technical Compliance": {
        "strong": [
            r"technical\s+compliance|compliance\s+sheet|parameter-?by-?parameter\s+compliance",
            r"deviation\s+sheet|schedule\s+of\s+deviations|catalogue|datasheet|data\s+sheet",
        ],
        "general": [
            r"comply\s+or\s+not\s+comply|statement\s+of\s+compliance|supported\s+by\s+literature",
        ],
    },
    "Statutory & Legal Requirements": {
        "strong": [
            r"gst\s+registration|gstin|pan\s+card|statutory\s+requirements?|labor\s+laws?|labour\s+laws?",
            r"epf\s+registration|esi\s+registration|pf\s+code|integrity\s+pact|anti-?corruption",
        ],
        "general": [
            r"factory\s+license|shops?\s+and\s+establishment|incorporation\s+certificate",
        ],
    },
    "Important Dates & Deadlines": {
        "strong": [
            r"submission\s+deadline|last\s+date\s+and\s+time|due\s+date|bid\s+submission\s+end\s+date",
            r"bid\s+opening\s+date|pre-?bid\s+meeting|bid\s+validity\s+period|validity\s+of\s+bids?",
        ],
        "general": [
            r"critical\s+dates|tender\s+schedule|completion\s+period|delivery\s+schedule",
        ],
    },
    "Submission Instructions": {
        "strong": [
            r"submission\s+of\s+bids?|instructions\s+to\s+bidders|itb|how\s+to\s+apply",
            r"two-?bid\s+system|two-?cover\s+system|technical\s+cover|financial\s+cover|envelope",
        ],
        "general": [
            r"e-procurement\s+portal|digital\s+signature|dsc|encryption|online\s+upload",
        ],
    },
    "Required Certificates & Documents": {
        "strong": [
            r"mandatory\s+documents?|checklist\s+of\s+documents|documents\s+to\s+be\s+submitted",
            r"power\s+of\s+attorney|poa|undertaking|affidavit|self-?declaration",
        ],
        "general": [
            r"duly\s+signed|notarized|stamped|enclosed\s+herewith|scanned\s+copies",
        ],
    },
    "Evaluation & Selection Criteria": {
        "strong": [
            r"evaluation\s+criteria|selection\s+process|method\s+of\s+selection|qcbs|least\s+cost\s+system",
            r"lowest\s+bidder|l-?1\s+determination|technical\s+score|financial\s+evaluation",
        ],
        "general": [
            r"evaluation\s+committee|minimum\s+qualifying\s+marks|responsiveness\s+of\s+bid",
        ],
    },
    "Penalties & Liquidated Damages": {
        "strong": [
            r"liquidated\s+damages|ld\s+clause|penalty\s+for\s+delay|risk\s+and\s+cost",
            r"delay\s+damages|deduction\s+for\s+delay|failure\s+to\s+complete",
        ],
        "general": [
            r"penalty\s+at\s+the\s+rate|maximum\s+penalty|per\s+week\s+of\s+delay",
        ],
    },
    "Security Deposits & PBG": {
        "strong": [
            r"performance\s+security|performance\s+bank\s+guarantee|pbg|security\s+deposit",
            r"contract\s+performance\s+guarantee|retention\s+money|defect\s+liability\s+period",
        ],
        "general": [
            r"security\s+deposit\s+refund|forfeiture\s+of\s+security|guarantee\s+period",
        ],
    },
    "Contract Terms & Conditions": {
        "strong": [
            r"general\s+conditions\s+of\s+contract|gcc|special\s+conditions\s+of\s+contract|scc",
            r"contract\s+agreement|termination\s+of\s+contract|force\s+majeure|dispute\s+resolution",
            r"arbitration\s+clause|jurisdiction\s+of\s+courts?|governing\s+law",
        ],
        "general": [
            r"indemnity|confidentiality|sub-?contracting|assignment|variation|severability",
        ],
    },
    "Payment Terms": {
        "strong": [
            r"payment\s+terms?|terms\s+of\s+payment|milestone\s+payments?|advance\s+payment",
            r"mobilization\s+advance|progressive\s+payment|final\s+bill\s+payment",
        ],
        "general": [
            r"retention\s+release|taxes\s+and\s+duties|tds|gst\s+payment|invoicing",
        ],
    },
    "Warranty & Maintenance": {
        "strong": [
            r"warranty\s+period|guarantee\s+period|defect\s+liability\s+period|comprehensive\s+warranty",
            r"annual\s+maintenance\s+contract|amc|cmc|post-?warranty\s+maintenance",
        ],
    },
    "Disqualification Conditions": {
        "strong": [
            r"disqualification|debarred|blacklisted|holiday\s+list|conflict\s+of\s+interest",
            r"bankruptcy|insolvency|fraudulent\s+practices?|corrupt\s+practices?|grounds\s+for\s+rejection",
        ],
        "general": [
            r"rejected\s+summarily|non-?responsive|banned\s+by\s+government",
        ],
    },
    "General & Special Conditions": {
        "strong": [
            r"general\s+conditions|special\s+conditions|annexure\s*[-:]?\s*[a-z0-9]|schedule\s*[-:]?\s*[a-z0-9]",
            r"proforma\s*[-:]?\s*[a-z0-9]|appendix\s*[-:]?\s*[a-z0-9]",
        ],
        "general": [
            r"clause\s+\d+|condition\s+no|scope\s+and\s+terms",
        ],
    },
}

# Precompile all regexes for performance
_COMPILED_CATEGORIES = {}
for cat_name, patterns in CATEGORIES.items():
    _COMPILED_CATEGORIES[cat_name] = {
        "strong": [re.compile(p, re.I) for p in patterns["strong"]],
        "general": [re.compile(p, re.I) for p in patterns.get("general", [])],
    }


@dataclass
class ClassificationResult:
    relevance_category: str
    relevance_categories: list[str]
    relevance_score: int
    matched_keywords: list[str]
    selection_reason: str
    is_llm_candidate: bool
    source_page: int
    chunk_index: int


def score_single_chunk(text: str, source_page: int, chunk_index: int) -> ClassificationResult:
    """Classify and score a single text chunk deterministically across all 20+ topics."""
    if not text or not text.strip():
        return ClassificationResult(
            relevance_category="Unclassified",
            relevance_categories=[],
            relevance_score=0,
            matched_keywords=[],
            selection_reason="Chunk contains no text.",
            is_llm_candidate=False,
            source_page=source_page,
            chunk_index=chunk_index,
        )

    matched_keywords: list[str] = []
    category_scores: dict[str, int] = {}

    # 1. Topic-specific keyword scoring
    for cat_name, compiled in _COMPILED_CATEGORIES.items():
        score = 0
        cat_matches = []

        # Strong cues (+25 points)
        for pattern in compiled["strong"]:
            m = pattern.search(text)
            if m:
                score += 25
                cat_matches.append(m.group(0))

        # General cues (+10 points)
        for pattern in compiled["general"]:
            m = pattern.search(text)
            if m:
                score += 10
                cat_matches.append(m.group(0))

        if score > 0:
            category_scores[cat_name] = score
            matched_keywords.extend(cat_matches)

    # 2. Universal entity & imperative cue scoring
    entity_score = 0
    reasons = []

    # Currency amounts (+10 points for financial/commercial relevance)
    currencies = _CURRENCY_RE.findall(text)
    if currencies:
        entity_score += 10
        matched_keywords.extend(currencies[:3])
        reasons.append(f"Contains currency amount ({', '.join(currencies[:2])})")

    # Percentages (+10 points for thresholds, fees, damages)
    percentages = _PERCENT_RE.findall(text)
    if percentages:
        entity_score += 10
        matched_keywords.extend(percentages[:3])
        reasons.append(f"Contains percentage value ({', '.join(percentages[:2])})")

    # Dates (+10 points for deadlines, periods)
    dates = _DATE_RE.findall(text)
    if dates:
        entity_score += 10
        matched_keywords.extend(dates[:3])
        reasons.append(f"Contains date ({', '.join(dates[:2])})")

    # Periods/Durations (+5 points for experience, validity, warranty)
    periods = _PERIOD_RE.findall(text)
    if periods:
        entity_score += 5
        matched_keywords.extend(periods[:3])
        reasons.append(f"Contains time period ({', '.join(periods[:2])})")

    # Legal imperatives (+5 points)
    imperatives = _MANDATORY_RE.findall(text)
    if imperatives:
        entity_score += 5
        matched_keywords.extend(imperatives[:3])
        reasons.append("Contains mandatory bidder/contractual imperative")

    # Table rows (+10 points for structured criteria tables)
    if _TABLE_ROW_RE.search(text):
        entity_score += 10
        reasons.append("Contains structured table criteria or data")

    # Deduplicate matched keywords while preserving order
    unique_keywords = list(dict.fromkeys(matched_keywords))

    # Calculate final score
    if category_scores:
        best_cat = max(category_scores, key=category_scores.get)
        base_cat_score = category_scores[best_cat]
        final_score = min(100, base_cat_score + entity_score)
        qualifying_cats = [cat for cat, s in category_scores.items() if s >= 15]
        if best_cat not in qualifying_cats:
            qualifying_cats.insert(0, best_cat)

        reasons.insert(0, f"Matched '{best_cat}' keywords ({', '.join(unique_keywords[:4])})")
        selection_reason = "; ".join(reasons)
        is_candidate = final_score >= 15

        return ClassificationResult(
            relevance_category=best_cat,
            relevance_categories=qualifying_cats,
            relevance_score=final_score,
            matched_keywords=unique_keywords[:15],
            selection_reason=selection_reason,
            is_llm_candidate=is_candidate,
            source_page=source_page,
            chunk_index=chunk_index,
        )

    # If no category matched, but entity or table cues are present
    if entity_score >= 10:
        final_score = min(50, entity_score)
        selection_reason = "; ".join(reasons) if reasons else "Contains numerical or structured criteria"
        return ClassificationResult(
            relevance_category="General & Special Conditions",
            relevance_categories=["General & Special Conditions"],
            relevance_score=final_score,
            matched_keywords=unique_keywords[:15],
            selection_reason=selection_reason,
            is_llm_candidate=final_score >= 15,
            source_page=source_page,
            chunk_index=chunk_index,
        )

    # Completely unclassified chunk - retained safely
    return ClassificationResult(
        relevance_category="Unclassified",
        relevance_categories=[],
        relevance_score=0,
        matched_keywords=[],
        selection_reason="No tender criteria keywords or numeric patterns matched (retained in unclassified pool).",
        is_llm_candidate=False,
        source_page=source_page,
        chunk_index=chunk_index,
    )


def classify_document_chunks(chunks: list[Any]) -> list[ClassificationResult]:
    """Classify and score all chunks in a document with contextual relationship boosting.
    
    Includes:
    - Independent scoring of each chunk.
    - Context boost for chunks immediately adjacent to high-relevance chunks.
    - Clause continuation inheritance: continuation chunks inherit their parent clause's status.
    """
    if not chunks:
        return []

    # First pass: Score each chunk individually
    results: list[ClassificationResult] = []
    for chunk in chunks:
        text = chunk.text if hasattr(chunk, "text") else chunk.get("text", "")
        page_num = chunk.page_number if hasattr(chunk, "page_number") else chunk.get("page_number", 1)
        chunk_idx = chunk.chunk_index if hasattr(chunk, "chunk_index") else chunk.get("chunk_index", 0)
        results.append(score_single_chunk(text, page_num, chunk_idx))

    # Second pass: Context boosting & clause continuation
    for i, res in enumerate(results):
        chunk_obj = chunks[i]
        is_cont = chunk_obj.is_continuation if hasattr(chunk_obj, "is_continuation") else chunk_obj.get("is_continuation", False)
        clause_id = chunk_obj.clause_id if hasattr(chunk_obj, "clause_id") else chunk_obj.get("clause_id")

        # Clause continuation boost
        if is_cont and i > 0 and results[i - 1].is_llm_candidate:
            if not res.is_llm_candidate:
                res.is_llm_candidate = True
                res.relevance_score = max(res.relevance_score, results[i - 1].relevance_score - 5)
                if res.relevance_category == "Unclassified":
                    res.relevance_category = results[i - 1].relevance_category
                    res.relevance_categories = list(results[i - 1].relevance_categories)
                res.selection_reason += f" (Inherited from continuation of {clause_id or 'preceding clause'})"

        # Adjacent context boost: if adjacent chunk is high relevance and this chunk has some partial relevance or shared context
        if not res.is_llm_candidate and res.relevance_score > 0:
            prev_high = i > 0 and results[i - 1].relevance_score >= 40
            next_high = i + 1 < len(results) and results[i + 1].relevance_score >= 40
            if prev_high or next_high:
                res.relevance_score = max(res.relevance_score, 18)
                res.is_llm_candidate = True
                neighbor_cat = results[i - 1].relevance_category if prev_high else results[i + 1].relevance_category
                if res.relevance_category == "Unclassified":
                    res.relevance_category = neighbor_cat
                res.selection_reason += " (Adjacent context to high-priority tender section)"

    return results
