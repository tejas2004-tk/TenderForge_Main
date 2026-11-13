import { Component, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { request } from "./auth.js";
import { canStartFreshAnalysis, confirmFreshAnalysis, freshAnalysisRequest } from "./analysisFreshRun.js";

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

export function normalizeAnalysis(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const progress = value.progress && typeof value.progress === "object" ? value.progress : {};
  const counts = value.requirement_counts && typeof value.requirement_counts === "object"
    ? value.requirement_counts : {};
  const coverage = value.coverage && typeof value.coverage === "object" ? value.coverage : null;
  return {
    ...value,
    status: typeof value.status === "string" ? value.status : "not_started",
    extraction_status: typeof value.extraction_status === "string" ? value.extraction_status : "not_processed",
    is_current_version: value.is_current_version !== false,
    progress: {
      total_batches: Number(progress.total_batches) || 0,
      completed_batches: Number(progress.completed_batches) || 0,
      pending_batches: Number(progress.pending_batches) || 0,
      failed_batches: Number(progress.failed_batches) || 0,
    },
    requirement_counts: {
      total: Number(counts.total) || 0,
      verified: Number(counts.verified) || 0,
      needs_review: Number(counts.needs_review) || 0,
      eligible_for_phase7: Number(counts.eligible_for_phase7) || 0,
    },
    requirements: Array.isArray(value.requirements) ? value.requirements
      .filter((item) => item && typeof item === "object" && !Array.isArray(item))
      .map((item) => ({
        ...item,
        category: typeof item.category === "string" ? item.category : "Other Mandatory Bidder Conditions",
        requirement_title: typeof item.requirement_title === "string" ? item.requirement_title : "Untitled requirement",
        description: typeof item.description === "string" ? item.description : "",
        requirement_value: typeof item.requirement_value === "string" ? item.requirement_value : "",
        requirement_status: ["mandatory", "conditional", "optional", "informational", "unclear"].includes(item.requirement_status)
          ? item.requirement_status : item.mandatory === true ? "mandatory" : item.mandatory === false ? "optional" : "unclear",
        applicability_condition: typeof item.applicability_condition === "string" ? item.applicability_condition : "",
        validation_status: typeof item.validation_status === "string" ? item.validation_status : "needs_review",
        source_evidence: Array.isArray(item.source_evidence) ? item.source_evidence.filter((source) => source && typeof source === "object" && !Array.isArray(source)) : [],
        validation_reasons: Array.isArray(item.validation_reasons) ? item.validation_reasons.filter((reason) => typeof reason === "string") : [],
      })) : [],
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
  const groups = useMemo(() => {
    const known = CATEGORIES.map((category) => ({
      category,
      items: requirements.filter((item) => item.category === category),
    })).filter((group) => group.items.length);
    const other = requirements.filter((item) => !CATEGORIES.includes(item.category));
    if (other.length) known.push({ category: "Other", items: other });
    return known;
  }, [requirements]);
  if (!groups.length) return <div className="analysis-empty">No requirement findings have been saved yet.</div>;
  return <div className="analysis-groups">{groups.map(({ category, items }) => (
    <section className="analysis-category" key={category}>
      <div className="analysis-category-heading"><h2>{category}</h2><span>{items.length}</span></div>
      <div className="analysis-requirements">{items.map((item, index) => (
        <article className="analysis-requirement" key={`${category}-${item.source_page}-${index}`}>
          <div className="analysis-requirement-top">
            <h3>{item.requirement_title}</h3>
            <span className={`verification-pill ${item.validation_status}`}>
              {item.validation_status === "verified" ? "Evidence verified" : "Needs review"}
            </span>
          </div>
          <p>{item.description}</p>
          <dl className="analysis-facts">
            <div><dt>Extracted value</dt><dd>{item.requirement_value || "Not specified"}</dd></div>
            <div><dt>Requirement status</dt><dd>{item.requirement_status}</dd></div>
            {item.requirement_status === "conditional" && item.applicability_condition && <div><dt>Applies when</dt><dd>{item.applicability_condition}</dd></div>}
            <div><dt>PDF page</dt><dd>{item.source_page ? item.source_page : "Unverified"}</dd></div>
          </dl>
          {(item.source_evidence?.length ? item.source_evidence : item.evidence_text ? [{ source_page: item.source_page, evidence_text: item.evidence_text }] : []).map((source, sourceIndex) => (
            <blockquote key={`${source.source_page}-${sourceIndex}`}><span>Source evidence · page {source.source_page || "unverified"}</span>{source.evidence_text || "No evidence text was provided."}</blockquote>
          ))}
          {item.validation_reasons?.length > 0 && <ul className="analysis-review-reasons">{item.validation_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
        </article>
      ))}</div>
    </section>
  ))}</div>;
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
      setAnalysis(normalized);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    } finally {
      setLoading(false);
    }
  }, [tenderId]);

  useEffect(() => { load(); }, [load]);

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
      setAnalysis(current);
      let steps = 0;
      while (current.status === "processing" && !current.in_progress && steps < 500) {
        result = await request(`/tenders/${tenderId}/analysis/resume`, { method: "POST" });
        current = normalizeAnalysis(result?.analysis);
        if (!current) throw new Error("The backend returned an invalid analysis response. Please try again.");
        setAnalysis(current);
        steps += 1;
      }
      if (current.status === "paused_rate_limit") {
        setNotice({ kind: "info", message: current.error_message || "Groq rate limited this analysis. Resume it later." });
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
  const canResume = ["pending", "paused_rate_limit", "failed"].includes(analysis?.status);
  const percentage = progress.total_batches ? Math.round(progress.completed_batches * 100 / progress.total_batches) : 0;
  const analysisIncomplete = analysis && progress.total_batches > 0 && (
    !["completed", "needs_review"].includes(analysis.status)
    || progress.completed_batches < progress.total_batches
  );
  const prerequisite = analysis?.extraction_status === "needs_ocr"
    ? { title: "This PDF needs OCR", message: "This tender appears to be scanned. Text-based analysis requires searchable PDF text; OCR is not available in this phase." }
    : analysis?.extraction_status === "failed"
      ? { title: "Retry PDF processing", message: "Phase 5 PDF text processing failed. Return to My Tenders and retry Process PDF before starting analysis." }
      : analysis?.extraction_status === "processing"
        ? { title: "PDF processing is underway", message: "Wait for Phase 5 text processing to finish, then return here to analyse the tender." }
        : { title: "Process the PDF first", message: "Phase 5 text extraction must finish before Agent 1 can analyse this tender. Return to My Tenders and choose Process PDF." };

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
        <div className="eyebrow"><span /> AGENT 1 · TENDER ANALYSIS</div>
        <div className="analysis-title-row"><div><h1>Tender analysis</h1><p>Evidence-based bidder requirements from your saved PDF text.</p>{analysis && <p className="analysis-version">Analysis run {analysis.analysis_version || "unknown"} · Agent 1 code {analysis.analysis_logic_version || "unknown"} · Prompt {analysis.prompt_version || "unknown"}</p>}</div>
          <div className="analysis-actions">
          {!loading && analysis && (analysis.is_current_version === false
            ? <button className="primary-button" disabled={!canStart || working} onClick={() => runAnalysis(false)}>{working ? <><span className="spinner" />Startingâ€¦</> : "Run updated analysis"}</button>
            : analysis.status === "not_started"
            ? <button className="primary-button" disabled={!canStart || working} onClick={() => runAnalysis(false)}>{working ? <><span className="spinner" />Starting…</> : "Analyse Tender"}</button>
            : canResume
              ? <button className="primary-button" disabled={working} onClick={() => runAnalysis(true)}>{working ? <><span className="spinner" />Resuming…</> : "Resume analysis"}</button>
              : analysis.status === "processing"
                ? analysis.in_progress
                  ? <button className="secondary-button" disabled={working} onClick={load}>{working ? "Updating…" : "Refresh progress"}</button>
                  : <button className="primary-button" disabled={working} onClick={() => runAnalysis(true)}>{working ? <><span className="spinner" />Continuing…</> : "Continue analysis"}</button>
                : <button className="secondary-button" disabled={working} onClick={load}>Refresh</button>)}
            {!loading && analysis && <button className="secondary-button" disabled={!canStartFreshAnalysis(analysis, working)} onClick={startFreshAnalysis}>Re-analyse with latest code</button>}
          </div>
        </div>
      </section>

      {!loading && analysis?.is_current_version === false && <div className="tender-notice info" role="status">Showing saved analysis version {analysis.analysis_version}. A newer analysis version is available; run it to refresh these findings.</div>}
      {notice && <div className={`tender-notice ${notice.kind}`} role="status">{notice.message}</div>}
      {loading ? <div className="tender-loading"><span className="spinner" />Loading tender analysis…</div> : !analysis ? null : !canStart && analysis.status === "not_started" ? (
        <section className="analysis-prerequisite"><span className="analysis-prerequisite-icon">i</span><div><h2>{prerequisite.title}</h2><p>{prerequisite.message}</p></div></section>
      ) : (
        <>
          <section className="analysis-status-card">
            <div className="analysis-status-top"><div><span className="section-kicker">ANALYSIS STATUS</span><h2>{analysis.status.replaceAll("_", " ")}</h2></div><span className={`analysis-status-badge ${analysis.status}`}>{analysis.status.replaceAll("_", " ")}</span></div>
            {progress.total_batches > 0 && <><div className="analysis-progress-label"><span>{progress.completed_batches} of {progress.total_batches} batches</span><span>{percentage}%</span></div><div className="analysis-progress-track"><span style={{ width: `${percentage}%` }} /></div></>}
            <div className="analysis-stats"><span><strong>{analysis.request_count || 0}</strong> Groq requests</span><span><strong>{analysis.estimated_input_tokens || 0}</strong> estimated input tokens</span><span><strong>{analysis.actual_input_tokens || 0}</strong> reported input tokens</span><span><strong>{analysis.actual_output_tokens || 0}</strong> reported output tokens</span><span><strong>{progress.failed_batches || 0}</strong> failed batches</span></div>
            {analysis.unknown_usage_requests > 0 && <p className="analysis-usage-note">Groq returned no usage totals for {analysis.unknown_usage_requests} request attempts. Reported totals above include only calls that returned usage metadata.</p>}
            {analysis.processing_started_at && <p className="analysis-usage-note">Run started {new Date(analysis.processing_started_at).toLocaleString()}{analysis.processing_finished_at ? ` · finished ${new Date(analysis.processing_finished_at).toLocaleString()}` : ""}</p>}
          </section>
          {analysis.status === "paused_rate_limit" && <div className="tender-notice info" role="status">Paused by {analysis.pause_reason === "daily_quota" ? "daily quota exhaustion" : "rate limit"}{analysis.pause_until ? ` until ${new Date(analysis.pause_until).toLocaleString()}` : ""}. Resume the current run after the cooldown.</div>}
          {analysisIncomplete && <div className="tender-notice info" role="status">This analysis is incomplete. Findings below come only from completed batches and may not cover the entire tender.</div>}
          {analysis.coverage && <section className={`analysis-coverage ${analysis.coverage.complete ? "good" : "review"}`}><div><strong>Text coverage: {Math.round((analysis.coverage.ratio || 0) * 100)}%</strong><span>{analysis.coverage.selected_pages} of {analysis.coverage.pages_with_text} text-bearing pages sent for analysis ({analysis.coverage.total_pages} PDF pages total); {analysis.coverage.unexamined_pages} text-bearing pages not sent.</span></div>{!analysis.coverage.complete && <p>Coverage is partial. Treat findings as incomplete and review the listed unexamined pages.</p>}</section>}
          {analysis.error_message && <div className="analysis-error-detail">{analysis.error_message}</div>}
          <OverviewFields fields={analysis.overview_fields} />
          {analysis.overview && !Object.keys(analysis.overview_fields || {}).length && <section className="analysis-overview"><span className="section-kicker">TENDER OVERVIEW</span><p>{analysis.overview}</p></section>}
          {analysis.coverage?.unexamined_page_numbers?.length > 0 && <details className="analysis-unexamined"><summary>Unexamined page numbers</summary><p>{analysis.coverage.unexamined_page_numbers.join(", ")}{analysis.coverage.unexamined_pages > analysis.coverage.unexamined_page_numbers.length ? " …" : ""}</p></details>}
          <section className="analysis-results-heading"><div><span className="section-kicker">STRUCTURED FINDINGS</span><h2>Requirements</h2></div><div className="analysis-counts"><span>{analysis.requirement_counts.verified} verified</span><span>{analysis.requirement_counts.needs_review} to review</span></div></section>
          <Requirements requirements={analysis.requirements || []} />
          <p className="analysis-phase7-note">Only evidence-verified requirement findings are eligible for a future Phase 7 workflow. Overview summaries, uncertain findings, and conflicting findings are not company input cards.</p>
        </>
      )}
      <footer className="tenders-footer"><span>© 2026 TenderForge</span><span>Agent 1 · Tender Analysis</span></footer>
    </main>
  );
}

export default function TenderAnalysis(props) {
  return <TenderAnalysisErrorBoundary onBack={props.onBack}><TenderAnalysisPage {...props} /></TenderAnalysisErrorBoundary>;
}
