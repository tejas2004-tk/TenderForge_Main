import assert from "node:assert/strict";
import test from "node:test";
import {
  connectionStatusDetail,
  connectionStatusLabel,
  formatGeminiConnectionError,
  formatGeminiConnectionSuccess,
} from "../src/connectionStatus.js";

test("connection status labels distinguish all Gemini states", () => {
  assert.equal(connectionStatusLabel("idle"), "Not tested");
  assert.equal(connectionStatusLabel("checking"), "Checking");
  assert.equal(connectionStatusLabel("connected"), "Connected");
  assert.equal(connectionStatusLabel("offline"), "Connection failed");
});

test("connection status details explain the Gemini idle and failure states", () => {
  assert.equal(connectionStatusDetail("idle", "Gemini"), "Run a test to check the connection");
  assert.equal(connectionStatusDetail("offline", "Gemini"), "Gemini connection failed");
});

test("Gemini connection output names the configured model on success and failure", () => {
  assert.equal(formatGeminiConnectionSuccess("gemini-3.8-flash", "connected"), "Model gemini-3.8-flash: connected");
  assert.equal(formatGeminiConnectionError("rate limit reached", "gemini-3.8-flash"), "rate limit reached (model: gemini-3.8-flash)");
});
