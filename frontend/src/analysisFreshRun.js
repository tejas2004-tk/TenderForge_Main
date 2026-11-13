export const FRESH_RUN_CONFIRMATION = "Start a completely fresh Agent 1 analysis using the latest code? This may consume Groq API quota. Previous analysis results will be kept.";

export function canStartFreshAnalysis(analysis, working) {
  return Boolean(
    analysis
    && analysis.extraction_status === "completed"
    && !working
    && !analysis.in_progress
    && analysis.status !== "processing"
  );
}

export function confirmFreshAnalysis(confirmFn = globalThis.confirm) {
  return typeof confirmFn === "function" && Boolean(confirmFn.call(globalThis, FRESH_RUN_CONFIRMATION));
}

export function freshAnalysisRequest(tenderId, requestId) {
  return {
    path: `/tenders/${tenderId}/analysis/fresh`,
    options: { method: "POST", headers: { "X-Run-Request-Id": requestId } },
  };
}
