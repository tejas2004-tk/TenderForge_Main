import test from "node:test";
import assert from "node:assert/strict";
import { acceptAnalysisUpdate, formatRateLimitPause, normalizeProgress } from "../src/analysisProgress.js";

test("normalizes all live logical batch counters", () => {
  assert.deepEqual(normalizeProgress({
    total_batches: "6",
    completed_batches: "2",
    workflow_completed_batches: "4",
    processing_batches: "1",
    pending_batches: "2",
    manual_review_batches: "1",
    split_batches: "3",
    retrying_batches: "1",
    failed_batches: "2",
  }), {
    total_batches: 6,
    completed_batches: 2,
    workflow_completed_batches: 4,
    processing_batches: 1,
    pending_batches: 2,
    manual_review_batches: 1,
    split_batches: 3,
    retrying_batches: 1,
    failed_batches: 2,
  });
});

test("stale polling cannot replace a final analysis with processing", () => {
  const final = { id: "run-1", status: "completed" };
  const stale = { id: "run-1", status: "processing" };
  assert.equal(acceptAnalysisUpdate(final, stale), final);
  assert.equal(acceptAnalysisUpdate(final, { ...stale, id: "run-2" }).id, "run-2");
});

test("displays the persisted provider cooldown duration", () => {
  const message = formatRateLimitPause("2026-11-17T06:28:08.300Z", 3.3);
  assert.match(message, /about 3.3 seconds/);
  assert.match(message, /until/);
});
