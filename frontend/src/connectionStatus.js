export function connectionStatusLabel(status) {
  if (status === "checking") return "Checking";
  if (status === "connected" || status === "online") return "Connected";
  if (status === "offline") return "Connection failed";
  return "Not tested";
}

export function connectionStatusDetail(status, service) {
  if (status === "checking") return service === "Gemini" ? "Checking Gemini…" : `Checking ${service}…`;
  if (status === "connected" || status === "online") {
    return service === "Gemini" ? "AI service is responding" : `${service} service is responding`;
  }
  if (status === "offline") return `${service} connection failed`;
  return service === "Gemini" ? "Run a test to check the connection" : "Not tested";
}

export function formatGeminiConnectionSuccess(model, message) {
  return model ? `Model ${model}: ${message}` : message;
}

export function formatGeminiConnectionError(message, model) {
  return model ? `${message} (model: ${model})` : message;
}
