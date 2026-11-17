import test from "node:test";
import assert from "node:assert/strict";
import { formatRateLimitPause, normalizeProgress } from "../src/analysisProgress.js";

test("normalizes all live logical batch counters", () => {
  assert.deepEqual(normalizeProgress({
    total_batches: "6",
    completed_batches: "2",
    processing_batches: "1",
    pending_batches: "2",
    manual_review_batches: "1",
    split_batches: "3",
    retrying_batches: "1",
    failed_batches: "2",
  }), {
    total_batches: 6,
    completed_batches: 2,
    processing_batches: 1,
    pending_batches: 2,
    manual_review_batches: 1,
    split_batches: 3,
    retrying_batches: 1,
    failed_batches: 2,
  });

  test("displays the persisted provider cooldown duration", () => {
    const message = formatRateLimitPause("2026-11-17T06:28:08.300Z", 3.3);
    assert.match(message, /about 3.3 seconds/);
    assert.match(message, /until/);
  });
});
