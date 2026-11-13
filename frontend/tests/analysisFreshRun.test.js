import assert from "node:assert/strict";
import test from "node:test";
import {
  canStartFreshAnalysis,
  confirmFreshAnalysis,
  FRESH_RUN_CONFIRMATION,
  freshAnalysisRequest,
} from "../src/analysisFreshRun.js";

test("fresh analysis requires explicit quota confirmation", () => {
  let message = "";
  assert.equal(confirmFreshAnalysis((value) => { message = value; return false; }), false);
  assert.match(message, /Groq API quota/);
  assert.match(message, /Previous analysis results will be kept/);
  assert.equal(confirmFreshAnalysis(() => true), true);
  assert.match(FRESH_RUN_CONFIRMATION, /fresh Agent 1 analysis/);
});

test("fresh analysis is disabled unless extraction is complete and no run is active", () => {
  const ready = { extraction_status: "completed", status: "needs_review", in_progress: false };
  assert.equal(canStartFreshAnalysis(ready, false), true);
  assert.equal(canStartFreshAnalysis(ready, true), false);
  assert.equal(canStartFreshAnalysis({ ...ready, in_progress: true }, false), false);
  assert.equal(canStartFreshAnalysis({ ...ready, status: "processing" }, false), false);
  assert.equal(canStartFreshAnalysis({ ...ready, extraction_status: "processing" }, false), false);
});

test("fresh run targets its own endpoint with a stable idempotency key", () => {
  assert.deepEqual(freshAnalysisRequest("tender-1", "request-1"), {
    path: "/tenders/tender-1/analysis/fresh",
    options: { method: "POST", headers: { "X-Run-Request-Id": "request-1" } },
  });
});
