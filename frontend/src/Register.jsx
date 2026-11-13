import { useState } from "react";
import { request } from "./auth.js";
import { AuthLayout } from "./Login.jsx";

export default function Register({ onNavigate }) {
  const [form, setForm] = useState({ full_name: "", company_name: "", email: "", password: "", confirm_password: "" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  function update(event) {
    setForm((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  async function handleSubmit(event) {
    event.preventDefault(); setError("");
    if (form.password !== form.confirm_password) { setError("Passwords do not match."); return; }
    setLoading(true);
    try {
      await request("/auth/register", { method: "POST", body: JSON.stringify(form) });
      onNavigate("/login");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthLayout>
      <div className="auth-kicker"><span /> GET STARTED</div>
      <h1>Create your account</h1>
      <p className="auth-intro">Set up your TenderForge workspace.</p>
      <form className="auth-form register-form" onSubmit={handleSubmit}>
        <div className="auth-fields-row">
          <label>Full name<input name="full_name" autoComplete="name" value={form.full_name} onChange={update} placeholder="Alex Morgan" required maxLength={120} /></label>
          <label>Company name<input name="company_name" autoComplete="organization" value={form.company_name} onChange={update} placeholder="Acme Studio" required maxLength={160} /></label>
        </div>
        <label>Email address<input name="email" type="email" autoComplete="email" value={form.email} onChange={update} placeholder="you@company.com" required maxLength={254} /></label>
        <label>Password<input name="password" type="password" autoComplete="new-password" value={form.password} onChange={update} placeholder="At least 8 characters" required minLength={8} maxLength={128} /></label>
        <label>Confirm password<input name="confirm_password" type="password" autoComplete="new-password" value={form.confirm_password} onChange={update} placeholder="Enter your password again" required minLength={8} maxLength={128} /></label>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <button className="auth-submit" type="submit" disabled={loading}>{loading && <span className="spinner" />}{loading ? "Creating account…" : "Create account"}<span aria-hidden="true">↗</span></button>
      </form>
      <p className="auth-switch">Already have an account? <a href="/login" onClick={(event) => { event.preventDefault(); onNavigate("/login"); }}>Sign in</a></p>
    </AuthLayout>
  );
}
