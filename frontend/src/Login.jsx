import { useState } from "react";
import { request } from "./auth.js";

export default function Login({ onNavigate, onAuthenticated }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault(); setError(""); setLoading(true);
    try {
      const data = await request("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password, remember_me: remember }),
      });
      onAuthenticated(data.user);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthLayout>
      <div className="auth-kicker"><span /> WELCOME BACK</div>
      <h1>Sign in to your workspace</h1>
      <p className="auth-intro">Pick up where you left off.</p>
      <form className="auth-form" onSubmit={handleSubmit}>
        <label>Email address<input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@company.com" required maxLength={254} /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Enter your password" required maxLength={128} /></label>
        <label className="remember-row"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} /> <span>Remember me</span></label>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <button className="auth-submit" type="submit" disabled={loading}>{loading && <span className="spinner" />}{loading ? "Signing in…" : "Sign in"}<span aria-hidden="true">↗</span></button>
      </form>
      <p className="auth-switch">New to TenderForge? <a href="/register" onClick={(event) => { event.preventDefault(); onNavigate("/register"); }}>Create an account</a></p>
    </AuthLayout>
  );
}

function AuthLayout({ children }) {
  return <main className="auth-page"><div className="auth-card"><a className="brand auth-brand" href="/login"><span className="brand-mark"><span /></span><span>Tender<span className="brand-light">Forge</span></span></a><div className="auth-content">{children}</div><div className="auth-footer"><span>© 2026 TenderForge</span><span>Built for clarity. Ready to forge.</span></div></div><aside className="auth-aside"><div className="aside-decoration"><span /><span /><span /></div><div className="aside-copy"><span className="section-kicker">TENDER MANAGEMENT, REFINED</span><h2>Make every opportunity count.</h2><p>A calmer workspace for teams finding and shaping their next great opportunity.</p></div><span className="aside-caption">YOUR NEXT CHAPTER STARTS HERE</span></aside></main>;
}

export { AuthLayout };
