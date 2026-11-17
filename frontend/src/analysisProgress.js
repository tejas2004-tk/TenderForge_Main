export function normalizeProgress(value) {
  const progress = value && typeof value === "object" ? value : {};
  return {
    total_batches: Number(progress.total_batches) || 0,
    completed_batches: Number(progress.completed_batches) || 0,
    processing_batches: Number(progress.processing_batches) || 0,
    pending_batches: Number(progress.pending_batches) || 0,
    manual_review_batches: Number(progress.manual_review_batches) || 0,
    split_batches: Number(progress.split_batches) || 0,
    retrying_batches: Number(progress.retrying_batches) || 0,
    failed_batches: Number(progress.failed_batches) || 0,
  };
}

export function formatRateLimitPause(pauseUntil, retryAfterSeconds) {
  const until = pauseUntil ? new Date(pauseUntil) : null;
  const duration = Number.isFinite(Number(retryAfterSeconds))
    ? Math.max(0, Number(retryAfterSeconds))
    : null;
  const durationText = duration === null ? "" : Number.isInteger(duration) ? String(duration) : duration.toFixed(1);
  const untilText = until && !Number.isNaN(until.getTime())
    ? ` until ${until.toLocaleString()}`
    : "";
  return duration === null
    ? `Paused by rate limit${untilText}.`
    : `Paused by rate limit for about ${durationText} second${duration === 1 ? "" : "s"}${untilText}.`;
}
