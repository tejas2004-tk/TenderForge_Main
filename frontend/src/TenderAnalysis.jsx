import { Component, useCallback, useEffect, useRef, useState } from "react";
import { request } from "./auth.js";
import { canStartFreshAnalysis, confirmFreshAnalysis, freshAnalysisRequest } from "./analysisFreshRun.js";
import { acceptAnalysisUpdate, normalizeProgress } from "./analysisProgress.js";
import { deriveDisplayRequirements } from "./analysisQuality.js";

const OVERVIEW_LABELS = {
  tender_title: "Tender title",
  issuing_authority: "Issuing authority",
  tender_reference_number: "Tender reference number",
  procurement_method: "Procurement method",
  submission_deadline: "Submission deadline",
  emd: "EMD",
  tender_fees: "Tender fees",
  important_conditions: "Important conditions",
};

function normalizeDiagnostic(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    operation: typeof value.operation === "string" ? value.operation : "",
    batch_id: typeof value.batch_id === "string" ? value.batch_id : "",
    batch_index: Number.isInteger(value.batch_index) ? value.batch_index : null,
    exception_type: typeof value.exception_type === "string" ? value.exception_type : "",
    exception_message: typeof value.exception_message === "string" ? value.exception_message : "",
    error_code: value.error_code ?? null,
    timestamp: typeof value.timestamp === "string" ? value.timestamp : null,
  };
}

function normalizeProviderDiagnostic(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    operation: typeof value.operation === "string" ? value.operation : "",
    endpoint: typeof value.endpoint === "string" ? value.endpoint : "",
    status_code: Number.isInteger(value.status_code) ? value.status_code : null,
    provider_code: typeof value.provider_code === "string" ? value.provider_code : "",
    provider_type: typeof value.provider_type === "string" ? value.provider_type : "",
    provider_message: typeof value.provider_message === "string" ? value.provider_message : "",
    model: typeof value.model === "string" ? value.model : "",
    estimated_input_tokens: Number(value.estimated_input_tokens) || 0,
    max_output_tokens: Number(value.max_output_tokens) || 0,
    temperature: Number(value.temperature),
    response_mime_type: typeof value.response_mime_type === "string" ? value.response_mime_type : "",
    retry_after_seconds: Number.isFinite(Number(value.retry_after_seconds))
      ? Number(value.retry_after_seconds) : null,
    retry_after_source: typeof value.retry_after_source === "string" ? value.retry_after_source : "",
    timestamp: typeof value.timestamp === "string" ? value.timestamp : null,
  };
}

export function normalizeAnalysis(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const counts = value.requirement_counts && typeof value.requirement_counts === "object"
    ? value.requirement_counts : {};
  const coverage = value.coverage && typeof value.coverage === "object" ? value.coverage : null;
  const requirements = Array.isArray(value.requirements) ? value.requirements
    .filter((item) => item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      ...item,
      category: typeof item.category === "string" ? item.category : "Other Mandatory Bidder Conditions",
      requirement_title: typeof item.requirement_title === "string" ? item.requirement_title : "",
      description: typeof item.description === "string" ? item.description : "",
      requirement_value: typeof item.requirement_value === "string" ? item.requirement_value : "",
      requirement_status: ["mandatory", "conditional", "optional", "informational", "unclear"].includes(item.requirement_status)
        ? item.requirement_status : item.mandatory === true ? "mandatory" : item.mandatory === false ? "optional" : "unclear",
      applicability_condition: typeof item.applicability_condition === "string" ? item.applicability_condition : "",
      validation_status: typeof item.validation_status === "string" ? item.validation_status : "needs_review",
      preliminary: item.preliminary === true,
      source_evidence: Array.isArray(item.source_evidence) ? item.source_evidence.filter((source) => source && typeof source === "object" && !Array.isArray(source)) : [],
      validation_reasons: Array.isArray(item.validation_reasons) ? item.validation_reasons.filter((reason) => typeof reason === "string") : [],
    })) : [];
  const displayRequirements = deriveDisplayRequirements(requirements);
  const displayCounts = {
    total: displayRequirements.length,
    verified: displayRequirements.filter((item) => item.validation_status === "verified").length,
    needs_review: displayRequirements.filter((item) => item.validation_status !== "verified").length,
  };
  return {
    ...value,
    status: typeof value.status === "string" ? value.status : "not_started",
    extraction_status: typeof value.extraction_status === "string" ? value.extraction_status : "not_processed",
    is_current_version: value.is_current_version !== false,
    progress: normalizeProgress(value.progress),
    retry_after_seconds: Number.isFinite(Number(value.retry_after_seconds))
      ? Number(value.retry_after_seconds) : null,
    mongo_diagnostic: normalizeDiagnostic(value.mongo_diagnostic),
    batch_diagnostic: normalizeDiagnostic(value.batch_diagnostic),
    provider_diagnostic: normalizeProviderDiagnostic(value.provider_diagnostic),
    requirement_counts: {
      total: displayCounts.total,
      verified: displayCounts.verified,
      needs_review: displayCounts.needs_review,
      eligible_for_phase7: Number(counts.eligible_for_phase7) || 0,
    },
    requirements: displayRequirements,
    coverage: coverage ? {
      ...coverage,
      ratio: Number(coverage.ratio) || 0,
      selected_pages: Number(coverage.selected_pages) || 0,
      pages_with_text: Number(coverage.pages_with_text) || 0,
      total_pages: Number(coverage.total_pages) || 0,
      unexamined_pages: Number(coverage.unexamined_pages) || 0,
      unexamined_page_numbers: Array.isArray(coverage.unexamined_page_numbers) ? coverage.unexamined_page_numbers : [],
    } : null,
  };
}

class TenderAnalysisErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <main className="analysis-page"><section className="analysis-prerequisite" role="alert"><span className="analysis-prerequisite-icon">!</span><div><h2>Could not display tender analysis</h2><p>An unexpected page error occurred. Return to My Tenders and try opening this analysis again.</p><button className="secondary-button" onClick={this.props.onBack}>Back to My Tenders</button></div></section></main>;
    }
    return this.props.children;
  }
}

const CATEGORIES = [
  "Tender Overview",
  "Eligibility Requirements",
  "Financial Requirements",
  "Technical Requirements",
  "Product and Equipment Specifications",
  "Experience Requirements",
  "Required Documents and Certificates",
  "EMD and Tender Fees",
  "Important Dates and Deadlines",
  "Contractual Terms",
  "Procurement Policies",
  "Other Mandatory Bidder Conditions",
];

function OverviewFields({ fields }) {
  if (!fields || typeof fields !== "object") return null;
  const entries = Object.entries(OVERVIEW_LABELS).filter(([key]) => fields[key]);
  if (!entries.length) return null;
  return <section className="analysis-overview"><span className="section-kicker">TENDER OVERVIEW</span><dl className="analysis-facts analysis-overview-fields">
    {entries.map(([key, label]) => {
      const field = fields[key];
      const sources = Array.isArray(field.sources) ? field.sources : [];
      const pages = [...new Set(sources.map((source) => source?.source_page).filter(Number.isInteger))];
      return <div key={key}><dt>{label}</dt><dd>{field.conflict ? `Conflicting values in source${pages.length ? ` pages ${pages.join(", ")}` : ""}; review the tender.` : typeof field.value === "string" && field.value ? field.value : "Not stated in the analysed text."}</dd></div>;
    })}
  </dl></section>;
}

function Requirements({ requirements }) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [importanceFilter, setImportanceFilter] = useState("all");
  const [sort, setSort] = useState("category");
  const completed = requirements.filter((item) => !item.preliminary);
  const preliminary = requirements.filter((item) => item.preliminary);
  const matches = (item) => {
    const query = search.trim().toLocaleLowerCase();
    const searchable = [
      item.requirement_title,
      item.display_description,
      item.description,
      item.requirement_value,
      item.applicability_condition,
      item.category,
      ...(item.source_evidence || []).map((source) => source.evidence_text),
    ].join(" ").toLocaleLowerCase();
    if (query && !searchable.includes(query)) return false;
    if (statusFilter !== "all" && item.validation_status !== statusFilter) return false;
    if (importanceFilter === "mandatory" && item.requirement_status !== "mandatory") return false;
    if (importanceFilter === "other" && item.requirement_status === "mandatory") return false;
    return true;
  };
  const filteredCompleted = completed.filter(matches);
  const filteredPreliminary = preliminary.filter(matches);
  const filteredCount = filteredCompleted.length + filteredPreliminary.length;
  const renderGroups = (items) => {
  const sortedItems = [...items].sort((a, b) => {
    if (sort === "title") return String(a.requirement_title || "").localeCompare(String(b.requirement_title || ""));
    if (sort === "page") return Number(a.source_page || 999999) - Number(b.source_page || 999999);
    if (sort === "review") return (a.validation_status === "needs_review" ? 0 : 1) - (b.validation_status === "needs_review" ? 0 : 1);
    return String(a.category || "").localeCompare(String(b.category || ""));
  });
  const groups = (() => {
    const known = CATEGORIES.map((category) => ({
      category,
      items: sortedItems.filter((item) => item.category === category),
    })).filter((group) => group.items.length);
    const other = sortedItems.filter((item) => !CATEGORIES.includes(item.category));
    if (other.length) known.push({ category: "Other", items: other });
    return known;
  })();
  if (!groups.length) return <div className="analysis-empty">No requirement findings have been saved yet.</div>;
  return <div className="analysis-groups">{groups.map(({ category, items }) => (
    <section className="analysis-category" key={category}>
      <div className="analysis-category-heading"><h2>{category}</h2><span>{items.length}</span></div>
      <div className="analysis-requirements">{items.map((item, index) => (
        <article className="analysis-requirement" key={`${category}-${item.source_page}-${index}`}>
          <div className="analysis-requirement-top">
            <div className="analysis-requirement-title"><h3>{item.requirement_title || "Tender condition"}</h3><span className={`importance-pill ${item.requirement_status === "mandatory" ? "mandatory" : ""}`}>{item.requirement_status === "mandatory" ? "Mandatory requirement" : item.requirement_status === "conditional" ? "Conditional requirement" : item.requirement_status === "optional" ? "Optional requirement" : "Requirement status: unclear"}</span></div>
            <span className={`verification-pill ${item.validation_status}`}>{item.validation_status === "verified" ? "Evidence supported" : "Review required"}</span>
          </div>
          {(item.display_description || item.description) && <p>{item.display_description || item.description}</p>}
          <dl className="analysis-facts">
            {item.requirement_value && <div><dt>Extracted value</dt><dd>{item.requirement_value}</dd></div>}
            <div><dt>Compliance status</dt><dd>{item.requirement_status === "unclear" ? "Review required" : item.requirement_status}</dd></div>
            {item.requirement_status === "conditional" && item.applicability_condition && <div><dt>Applies when</dt><dd>{item.applicability_condition}</dd></div>}
            {item.source_page && <div><dt>Source page</dt><dd>{item.source_page}</dd></div>}
          </dl>
          {item.source_evidence?.length
            ? item.source_evidence.map((source, sourceIndex) => (
                <blockquote key={`${source.source_page}-${sourceIndex}`}><span>Evidence · {source.source_page ? `Source page ${source.source_page}` : "Source page not stated"}</span>{source.evidence_text}</blockquote>
              ))
            : <p className="analysis-no-evidence">No supporting evidence is cited. Review against the tender document.</p>}
          {item.validation_reasons?.length > 0 && <ul className="analysis-review-reasons">{item.validation_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
          {item.display_merged_count > 1 && <p className="analysis-merged-note">Combined from {item.display_merged_count} matching entries while preserving all cited evidence.</p>}
        </article>
      ))}</div>
    </section>
  ))}</div>;
  };
  return (
    <div className="analysis-findings">
      <div className="analysis-filters" aria-label="Requirement filters">
        <label className="analysis-search"><span>Search requirements</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search requirement, value, or evidence" /></label>
        <label><span>Compliance status</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All findings</option><option value="verified">Evidence supported</option><option value="needs_review">Review required</option></select></label>
        <label><span>Importance</span><select value={importanceFilter} onChange={(event) => setImportanceFilter(event.target.value)}><option value="all">All requirements</option><option value="mandatory">Mandatory</option><option value="other">Conditional or other</option></select></label>
        <label><span>Sort by</span><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="category">Category</option><option value="title">Requirement title</option><option value="page">Source page</option><option value="review">Review needed first</option></select></label>
      </div>
      <div className="analysis-filter-summary">{filteredCount} of {requirements.length} requirements shown</div>
      {renderGroups(filteredCompleted)}
      {filteredPreliminary.length > 0 && <section className="analysis-preliminary"><h2>Preliminary findings</h2><p>These findings came from an incomplete analysis section and require review.</p>{renderGroups(filteredPreliminary)}</section>}
      {filteredCount === 0 && <div className="analysis-empty">No requirements match these filters.</div>}
    </div>
  );
}

function TenderAnalysisPage({ tenderId, user, onBack }) {
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState(null);
  const runGuard = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await request(`/tenders/${tenderId}/analysis`);
      const normalized = normalizeAnalysis(result?.analysis);
      if (!normalized) throw new Error("The backend returned an invalid analysis response. Refresh the page or return to My Tenders.");
      setAnalysis((previous) => acceptAnalysisUpdate(previous, normalized));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    } finally {
      setLoading(false);
    }
  }, [tenderId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!working) return undefined;
    let stopped = false;
    const poll = async () => {
      try {
        const result = await request(`/tenders/${tenderId}/analysis`);
        const normalized = normalizeAnalysis(result?.analysis);
        if (!stopped && normalized) {
          setAnalysis((previous) => acceptAnalysisUpdate(previous, normalized));
        }
      } catch {
        // The active analysis request remains authoritative; the next poll can recover.
      }
    };
    poll();
    const timer = setInterval(poll, 1800);
    return () => { stopped = true; clearInterval(timer); };
  }, [working, tenderId]);

  async function runAnalysis(resume = false, fresh = false) {
    if (runGuard.current) return;
    runGuard.current = true;
    setWorking(true);
    setNotice(null);
    try {
      const requestId = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const freshRequest = fresh ? freshAnalysisRequest(tenderId, requestId) : null;
      let result = await request(freshRequest?.path || (resume
        ? `/tenders/${tenderId}/analysis/resume`
        : `/tenders/${tenderId}/analysis`), freshRequest?.options || { method: "POST" });
      let current = normalizeAnalysis(result?.analysis);
      if (!current) throw new Error("The backend returned an invalid analysis response. Please try again.");
      setAnalysis((previous) => acceptAnalysisUpdate(previous, current));
      let steps = 0;
      while (current.status === "processing" && !current.in_progress && steps < 500) {
        result = await request(`/tenders/${tenderId}/analysis/resume`, { method: "POST" });
        current = normalizeAnalysis(result?.analysis);
        if (!current) throw new Error("The backend returned an invalid analysis response. Please try again.");
        setAnalysis((previous) => acceptAnalysisUpdate(previous, current));
        steps += 1;
      }
      if (current.status === "paused_rate_limit") {
        setNotice({ kind: "info", message: "Analysis capacity is temporarily unavailable. Resume it later." });
      } else if (current.status === "failed") {
        setNotice({ kind: "error", message: current.error_message || "Analysis failed. You can resume it." });
      } else if (current.status === "needs_review") {
        setNotice({ kind: "info", message: "Analysis finished with items or document coverage that need review." });
      } else if (current.status === "completed") {
        setNotice({ kind: "success", message: "Tender analysis completed. Verified findings include source evidence." });
      } else if (current.in_progress) {
        setNotice({ kind: "info", message: "Analysis is active in another request. Refresh this page to check progress." });
      }
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
      await load();
    } finally {
      runGuard.current = false;
      setWorking(false);
    }
  }

  function startFreshAnalysis() {
    if (!canStartFreshAnalysis(analysis, working) || runGuard.current) return;
    if (!confirmFreshAnalysis()) return;
    runAnalysis(false, true);
  }

  const progress = analysis?.progress || {};
  const canStart = analysis?.extraction_status === "completed";
  const canResume = ["pending", "paused_rate_limit", "failed"].includes(analysis?.status)
    || (analysis?.status === "needs_review" && progress.manual_review_batches > 0);
  const workflowPercentage = progress.total_batches
    ? Math.round(progress.workflow_completed_batches * 100 / progress.total_batches) : 0;
  const analysisIncomplete = analysis && progress.total_batches > 0 && workflowPercentage < 100;
  const statusLabel = analysis?.status === "completed" || analysis?.status === "completed_with_warnings" || analysis?.status === "needs_review"
    ? "Analysis ready" : analysis?.status === "processing" || analysis?.status === "pending"
      ? "Processing requirements" : "Analyzing tender";
  const prerequisite = analysis?.extraction_status === "needs_ocr"
    ? { title: "This PDF needs OCR", message: "This tender appears to be scanned. Text-based analysis requires searchable PDF text; OCR is not available in this phase." }
    : analysis?.extraction_status === "failed"
      ? { title: "Document text processing needs attention", message: "The PDF text could not be prepared. Return to My Tenders and process the document again before starting analysis." }
      : analysis?.extraction_status === "processing"
        ? { title: "PDF processing is underway", message: "Wait for document text processing to finish, then return here to analyse the tender." }
        : { title: "Process the PDF first", message: "Document text preparation must finish before analysis can begin. Return to My Tenders and choose Process PDF." };

  return (
    <main className="analysis-page">
      <header className="topbar analysis-topbar">
        <a className="brand" href="#home" onClick={(event) => { event.preventDefault(); onBack(); }} aria-label="TenderForge">
          <span className="brand-mark"><span /></span><span>Tender<span className="brand-light">Forge</span></span>
        </a>
        <div className="topbar-actions"><span className="user-name">{user.company_name}</span><button className="logout-button" onClick={onBack}>Back to My Tenders</button></div>
      </header>

      <section className="analysis-hero">
        <button className="profile-back" onClick={onBack}>← <span>My Tenders</span></button>
        <div className="eyebrow"><span /> TENDER COMPLIANCE REVIEW</div>
        <div className="analysis-title-row"><div><h1>Tender analysis</h1><p>Clear tender conditions and supporting document evidence.</p></div>
          <div className="analysis-actions">
          {!loading && analysis && (analysis.is_current_version === false
            ? <button className="primary-button" disabled={!canStart || working} onClick={() => runAnalysis(false)}>{working ? <><span className="spinner" />Starting…</> : "Update analysis"}</button>
            : analysis.status === "not_started"
            ? <button className="primary-button" disabled={!canStart || working} onClick={() => runAnalysis(false)}>{working ? <><span className="spinner" />Starting…</> : "Analyse Tender"}</button>
            : canResume
              ? <button className="primary-button" disabled={working} onClick={() => runAnalysis(true)}>{working ? <><span className="spinner" />Continuing…</> : "Continue analysis"}</button>
              : analysis.status === "processing"
                ? analysis.in_progress
                  ? <button className="secondary-button" disabled={working} onClick={load}>{working ? "Updating…" : "Refresh progress"}</button>
                  : <button className="primary-button" disabled={working} onClick={() => runAnalysis(true)}>{working ? <><span className="spinner" />Continuing…</> : "Continue analysis"}</button>
                : <button className="secondary-button" disabled={working} onClick={load}>Refresh</button>)}
            {!loading && analysis && <button className="secondary-button" disabled={!canStartFreshAnalysis(analysis, working)} onClick={startFreshAnalysis}>Run a fresh analysis</button>}
          </div>
        </div>
      </section>

      {!loading && analysis?.is_current_version === false && <div className="tender-notice info" role="status">This is a saved analysis. Newer analysis settings are available; updating it is optional.</div>}
      {notice && <div className={`tender-notice ${notice.kind}`} role="status">{notice.message}</div>}
      {loading ? <div className="tender-loading"><span className="spinner" />Loading tender analysis…</div> : !analysis ? null : !canStart && analysis.status === "not_started" ? (
        <section className="analysis-prerequisite"><span className="analysis-prerequisite-icon">i</span><div><h2>{prerequisite.title}</h2><p>{prerequisite.message}</p></div></section>
      ) : (
        <>
          <section className="analysis-status-card">
            <div className="analysis-status-top"><div><span className="section-kicker">ANALYSIS STATUS</span><h2>{statusLabel}</h2></div><span className={`analysis-status-badge ${analysis.status}`}>{statusLabel}</span></div>
            {progress.total_batches > 0 && <><div className="analysis-progress-label"><span>{progress.workflow_completed_batches} sections complete · {progress.processing_batches} in progress · {progress.pending_batches + progress.retrying_batches + progress.failed_batches + progress.manual_review_batches} remaining</span><span>{workflowPercentage}%</span></div><div className="analysis-progress-track"><span style={{ width: `${workflowPercentage}%` }} /></div><p className="analysis-usage-note">Document coverage: {Math.round((analysis.coverage?.text_coverage_ratio || 0) * 100)}% of extracted readable text reviewed.</p></>}
            {analysis.processing_started_at && <p className="analysis-usage-note">Run started {new Date(analysis.processing_started_at).toLocaleString()}{analysis.processing_finished_at ? ` · finished ${new Date(analysis.processing_finished_at).toLocaleString()}` : ""}</p>}
          </section>
          {analysis.status === "paused_rate_limit" && <div className="tender-notice info" role="status">Analysis is temporarily paused. Continue it when processing capacity is available.</div>}
          {analysisIncomplete && <div className="tender-notice info" role="status">This analysis is incomplete. Findings below come only from completed sections and may not cover the entire tender.</div>}
          {analysis && progress.total_batches > 0 && progress.completed_batches === 0 && <div className="tender-notice info" role="status">No analysis section has completed yet. Any preliminary findings are not a completed analysis.</div>}
          {analysis.coverage && <section className={`analysis-coverage ${analysis.coverage.complete ? "good" : "review"}`}><div><strong>Document coverage: {Math.round((analysis.coverage.text_coverage_ratio || 0) * 100)}%</strong><span>{analysis.coverage.selected_pages} of {analysis.coverage.pages_with_text} text-bearing pages reviewed ({analysis.coverage.total_pages} PDF pages total); {analysis.coverage.unexamined_pages} text-bearing pages remain to be reviewed.</span></div>{!analysis.coverage.complete && <p>Coverage is partial. Treat findings as incomplete and review the listed unreviewed pages.</p>}</section>}
          {(analysis.error_message || analysis.mongo_diagnostic || analysis.batch_diagnostic || analysis.provider_diagnostic) && <div className="analysis-error-detail" role="status">{analysis.status === "failed" ? "Analysis needs attention. You can continue when ready." : analysis.status === "paused_rate_limit" ? "Analysis is paused and can continue when processing capacity is available." : analysis.status === "completed_with_warnings" || analysis.status === "needs_review" ? "Analysis is ready with some items or source text to review." : "Analysis is processing."}</div>}
          <OverviewFields fields={analysis.overview_fields} />
          {analysis.overview && !Object.keys(analysis.overview_fields || {}).length && <section className="analysis-overview"><span className="section-kicker">TENDER OVERVIEW</span><p>{analysis.overview}</p></section>}
          {analysis.coverage?.unexamined_page_numbers?.length > 0 && <details className="analysis-unexamined"><summary>Pages not yet reviewed</summary><p>{analysis.coverage.unexamined_page_numbers.join(", ")}{analysis.coverage.unexamined_pages > analysis.coverage.unexamined_page_numbers.length ? " …" : ""}</p></details>}
          <section className="analysis-results-heading"><div><span className="section-kicker">STRUCTURED FINDINGS</span><h2>Requirements</h2></div><div className="analysis-counts"><span>{analysis.requirement_counts.verified} verified</span><span>{analysis.requirement_counts.needs_review} to review</span></div></section>
          <Requirements requirements={analysis.requirements || []} />
          <p className="analysis-phase7-note">Findings marked as review required should be checked against the supporting document before use in a compliance decision.</p>
        </>
      )}
      <footer className="tenders-footer"><span>© 2026 TenderForge</span><span>Tender compliance review</span></footer>
    </main>
  );
}

export default function TenderAnalysis(props) {
  return <TenderAnalysisErrorBoundary onBack={props.onBack}><TenderAnalysisPage {...props} /></TenderAnalysisErrorBoundary>;
}
