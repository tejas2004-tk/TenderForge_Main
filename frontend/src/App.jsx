import { useCallback, useEffect, useState } from "react";
import Login from "./Login.jsx";
import Register from "./Register.jsx";
import CompanyProfile from "./CompanyProfile.jsx";
import MyTenders from "./MyTenders.jsx";
import TenderAnalysis from "./TenderAnalysis.jsx";
import { request } from "./auth.js";
import {
  connectionStatusDetail,
  connectionStatusLabel,
  formatGeminiConnectionError,
  formatGeminiConnectionSuccess,
} from "./connectionStatus.js";

function StatusCard({ icon, label, status, detail }) {
  const state = status === "connected" || status === "online" ? "good" : status === "checking" ? "checking" : "bad";
  const text = connectionStatusLabel(status);
  return (
    <article className="status-card">
      <div className="status-top"><span className="status-icon">{icon}</span><span className={`pill ${state}`}><i />{text}</span></div>
      <h3>{label}</h3><p>{detail}</p>
    </article>
  );
}

function Dashboard({ user, onLogout, onOpenProfile, onOpenTenders }) {
  const [backend, setBackend] = useState("checking");
  const [database, setDatabase] = useState("checking");
  const [gemini, setGemini] = useState("idle");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const checkConnections = useCallback(async () => {
    setBackend("checking"); setDatabase("checking");
    try {
      await request("/health");
      setBackend("online");
    } catch {
      setBackend("offline"); setDatabase("offline"); return;
    }
    try {
      await request("/test-db"); setDatabase("connected");
    } catch {
      setDatabase("offline");
    }
  }, []);

  useEffect(() => { checkConnections(); }, [checkConnections]);

  async function runGeminiTest() {
    setLoading(true); setGemini("checking"); setResult(null);
    try {
      const data = await request("/test-gemini");
      setGemini("connected"); setResult({ kind: "success", message: formatGeminiConnectionSuccess(data.model, data.message) });
    } catch (error) {
      setGemini("offline");
      const model = error.data?.model;
      setResult({ kind: "error", message: formatGeminiConnectionError(error.message, model) });
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <a className="brand" href="#home" aria-label="TenderForge home">
          <span className="brand-mark"><span /></span><span>Tender<span className="brand-light">Forge</span></span>
        </a>
        <div className="topbar-actions"><span className="environment"><i /> FOUNDATION ENVIRONMENT</span><span className="user-name">{user.full_name}</span><button className="profile-nav-button" onClick={onOpenTenders}>My Tenders</button><button className="profile-nav-button" onClick={onOpenProfile}>Company Profile</button><button className="logout-button" onClick={onLogout}>Log out</button></div>
      </header>

      <section className="hero" id="home">
        <div className="eyebrow"><span /> YOUR WORKSPACE</div>
        <div className="hero-row"><div><h1>Good morning, {user.full_name.split(" ")[0]}.</h1><p>Your workspace is ready when you are.</p></div><button className="refresh-button" onClick={checkConnections} aria-label="Refresh connection status">↻ <span>Refresh status</span></button></div>
      </section>

      <section className="section-heading"><div><span className="section-kicker">SYSTEM OVERVIEW</span><h2>Connections</h2></div><span className="updated">LIVE STATUS</span></section>
      <section className="status-grid" aria-label="Service connection status">
        <StatusCard icon="⌘" label="Backend API" status={backend} detail={connectionStatusDetail(backend, "Flask")} />
        <StatusCard icon="◈" label="MongoDB Atlas" status={database} detail={database === "connected" ? "Database connection is healthy" : database === "checking" ? "Checking database…" : database === "offline" ? "Database connection failed" : "Not tested"} />
        <StatusCard icon="✳" label="Gemini" status={gemini} detail={connectionStatusDetail(gemini, "Gemini")} />
      </section>

      <section className="test-panel">
        <div className="test-copy"><span className="section-kicker">QUICK CHECK</span><h2>Test your AI connection</h2><p>Send a short test prompt to Gemini through your secure backend.</p></div>
        <button className="primary-button" onClick={runGeminiTest} disabled={loading || backend !== "online"}>{loading ? <><span className="spinner" />Testing…</> : <>Test Gemini <span>↗</span></>}</button>
      </section>

      <section className="response-section" aria-live="polite"><div className="response-heading"><div><span className="section-kicker">TEST OUTPUT</span><h2>Response</h2></div>{result && <span className={`result-label ${result.kind}`}>{result.kind === "success" ? "SUCCESS" : "NEEDS ATTENTION"}</span>}</div>
        <div className={`response-box ${result ? result.kind : "empty"}`}>{result ? <><span className="response-indicator">{result.kind === "success" ? "↳" : "!"}</span><p>{result.message}</p></> : <><span className="empty-icon">↳</span><p>Your Gemini test response will appear here.</p></>}</div>
      </section>
      <footer><span>© 2026 TenderForge</span><span>Built for clarity. Ready to forge.</span></footer>
    </main>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [path, setPath] = useState(window.location.pathname);

  const navigate = useCallback((nextPath, replace = false) => {
    if (replace) window.history.replaceState({}, "", nextPath);
    else window.history.pushState({}, "", nextPath);
    setPath(nextPath);
  }, []);

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    request("/auth/me")
      .then((data) => setUser(data.user))
      .catch(() => setUser(null))
      .finally(() => setCheckingSession(false));
  }, []);

  useEffect(() => {
    if (checkingSession) return;
    if (user && (path === "/login" || path === "/register")) navigate("/", true);
    else if (!user && path !== "/login" && path !== "/register") navigate("/login", true);
  }, [checkingSession, navigate, path, user]);

  async function logout() {
    try { await request("/auth/logout", { method: "POST" }); } catch { /* Clear local state even if the server is unreachable. */ }
    setUser(null);
    navigate("/login", true);
  }

  if (checkingSession || (user && (path === "/login" || path === "/register")) || (!user && path !== "/login" && path !== "/register")) {
    return <main className="auth-loading"><span className="spinner" />Loading your workspace…</main>;
  }
  if (user && path === "/company-profile") return <CompanyProfile user={user} onBack={() => navigate("/")} />;
  if (user && path === "/my-tenders") return <MyTenders user={user} onBack={() => navigate("/")} onAnalyse={(tender) => navigate(`/tenders/${tender.id}/analysis`)} />;
  const analysisPath = path.match(/^\/tenders\/([^/]+)\/analysis$/);
  if (user && analysisPath) return <TenderAnalysis tenderId={analysisPath[1]} user={user} onBack={() => navigate("/my-tenders")} />;
  if (user) return <Dashboard user={user} onLogout={logout} onOpenProfile={() => navigate("/company-profile")} onOpenTenders={() => navigate("/my-tenders")} />;
  if (path === "/register") return <Register onNavigate={navigate} />;
  return <Login onNavigate={navigate} onAuthenticated={(authenticatedUser) => { setUser(authenticatedUser); navigate("/", true); }} />;
}
