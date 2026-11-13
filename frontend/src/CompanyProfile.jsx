import { useEffect, useState } from "react";
import { API_BASE_URL, request } from "./auth.js";

const GROUPS = [
  {
    title: "Company details",
    fields: [
      { name: "legal_name", label: "Legal company name", placeholder: "As shown on registration documents" },
      { name: "trade_name", label: "Trading name", placeholder: "Public or operating name" },
      { name: "website", label: "Website", type: "url", placeholder: "https://company.com" },
      { name: "description", label: "Company overview", multiline: true, wide: true, placeholder: "A short introduction to your company" },
      { name: "address", label: "Registered address", multiline: true, wide: true, placeholder: "Street and building" },
      { name: "city", label: "City" }, { name: "state", label: "State / province" },
      { name: "postal_code", label: "Postal code" }, { name: "country", label: "Country" },
    ],
  },
  {
    title: "Registration details",
    fields: [
      { name: "registration_type", label: "Business type", placeholder: "e.g. Private limited company" },
      { name: "registration_number", label: "Registration number" },
      { name: "incorporation_date", label: "Date of incorporation", type: "date" },
    ],
  },
  {
    title: "Contact details",
    fields: [
      { name: "contact_name", label: "Primary contact" },
      { name: "contact_role", label: "Role / designation" },
      { name: "contact_email", label: "Contact email", type: "email" },
      { name: "contact_phone", label: "Phone number", type: "tel" },
      { name: "alternate_phone", label: "Alternate phone", type: "tel" },
    ],
  },
  {
    title: "GST and PAN",
    fields: [
      { name: "gst_number", label: "GST number" },
      { name: "pan_number", label: "PAN number" },
    ],
  },
  {
    title: "Turnover",
    fields: [
      { name: "turnover_current_year", label: "Current financial year" },
      { name: "turnover_current_amount", label: "Current year turnover", placeholder: "Amount and currency" },
      { name: "turnover_previous_year", label: "Previous financial year" },
      { name: "turnover_previous_amount", label: "Previous year turnover", placeholder: "Amount and currency" },
    ],
  },
  {
    title: "Experience and employees",
    fields: [
      { name: "years_experience", label: "Years in business", type: "number", placeholder: "e.g. 8" },
      { name: "employee_count", label: "Number of employees", type: "number", placeholder: "e.g. 42" },
      { name: "sectors", label: "Sectors and experience", multiline: true, wide: true, placeholder: "Describe your areas of experience" },
    ],
  },
  {
    title: "Services, previous projects, and certifications",
    fields: [
      { name: "services", label: "Services", multiline: true, wide: true, placeholder: "List your main services" },
      { name: "previous_projects", label: "Previous projects", multiline: true, wide: true, placeholder: "Project, client, year, and relevant details" },
      { name: "certifications", label: "Certifications", multiline: true, wide: true, placeholder: "List certifications and validity dates" },
    ],
  },
];

const EMPTY_PROFILE = Object.fromEntries(GROUPS.flatMap((group) => group.fields.map((field) => [field.name, ""])));

export default function CompanyProfile({ user, onBack }) {
  const [profile, setProfile] = useState({ ...EMPTY_PROFILE, legal_name: user.company_name, supporting_documents: [] });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    request("/company-profile")
      .then((data) => setProfile({ ...EMPTY_PROFILE, ...data.profile }))
      .catch((error) => setMessage({ kind: "error", text: error.message }))
      .finally(() => setLoading(false));
  }, []);

  function updateField(event) {
    setProfile((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  async function saveProfile(event) {
    event.preventDefault(); setSaving(true); setMessage(null);
    try {
      const data = await request("/company-profile", {
        method: "PUT",
        body: JSON.stringify(profile),
      });
      setProfile({ ...EMPTY_PROFILE, ...data.profile });
      setMessage({ kind: "success", text: "General information saved." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    } finally {
      setSaving(false);
    }
  }

  async function uploadDocuments(event) {
    const files = event.target.files;
    if (!files?.length) return;
    const form = new FormData();
    for (const file of files) form.append("files", file);
    setUploading(true); setMessage(null);
    try {
      const data = await request("/company-profile/documents", { method: "POST", body: form });
      setProfile((current) => ({ ...current, supporting_documents: [...current.supporting_documents, ...data.documents] }));
      setMessage({ kind: "success", text: "Supporting documents uploaded." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    } finally {
      setUploading(false); event.target.value = "";
    }
  }

  async function downloadDocument(document) {
    try {
      const response = await fetch(`${API_BASE_URL}/company-profile/documents/${document.id}`, { credentials: "include" });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.message || "Could not download this document.");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = window.document.createElement("a");
      link.href = url; link.download = document.name; link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
  }

  async function removeDocument(documentId) {
    try {
      await request(`/company-profile/documents/${documentId}`, { method: "DELETE" });
      setProfile((current) => ({
        ...current,
        supporting_documents: current.supporting_documents.filter((document) => document.id !== documentId),
      }));
      setMessage({ kind: "success", text: "Supporting document removed." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
  }

  return (
    <main className="profile-page">
      <header className="topbar profile-topbar">
        <a className="brand" href="#home" aria-label="TenderForge home" onClick={(event) => { event.preventDefault(); onBack(); }}>
          <span className="brand-mark"><span /></span><span>Tender<span className="brand-light">Forge</span></span>
        </a>
        <div className="topbar-actions"><span className="user-name">{user.company_name}</span></div>
      </header>

      <div className="profile-heading">
        <button className="profile-back" onClick={onBack}>← <span>Dashboard</span></button>
        <div className="eyebrow"><span /> COMPANY WORKSPACE</div>
        <h1>Company Profile</h1>
        <p>Keep your reusable company information ready for future tenders.</p>
      </div>

      {message && <div className={`profile-notice ${message.kind}`} role="status">{message.text}</div>}

      <section className="profile-section" aria-labelledby="general-information-heading">
        <div className="profile-section-heading">
          <div><span className="section-kicker">SECTION 1</span><h2 id="general-information-heading">General Information</h2><p>Reusable details about your company.</p></div>
          <span className="profile-saved-indicator">PRIVATE WORKSPACE</span>
        </div>

        {loading ? <div className="profile-loading"><span className="spinner" />Loading company information…</div> : (
          <form onSubmit={saveProfile}>
            {GROUPS.map((group) => (
              <fieldset className="profile-group" key={group.title}>
                <legend>{group.title}</legend>
                <div className="profile-fields">
                  {group.fields.map((field) => (
                    <label className={field.wide ? "profile-field wide" : "profile-field"} key={field.name}>
                      <span>{field.label}</span>
                      {field.multiline ? (
                        <textarea name={field.name} value={profile[field.name] || ""} onChange={updateField} placeholder={field.placeholder || "Add information"} rows={field.name === "description" || field.name === "address" ? 2 : 3} />
                      ) : (
                        <input name={field.name} type={field.type || "text"} value={profile[field.name] || ""} onChange={updateField} placeholder={field.placeholder || "Add information"} />
                      )}
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}

            <fieldset className="profile-group document-group">
              <legend>General supporting documents</legend>
              <p className="group-hint">Upload reusable company documents such as registration certificates, tax documents, or certifications.</p>
              <label className="upload-dropzone">
                <input type="file" multiple accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png" onChange={uploadDocuments} disabled={uploading} />
                <span className="upload-icon">↑</span>
                <span>{uploading ? "Uploading documents…" : "Choose files to upload"}</span>
                <small>PDF, Word, Excel, JPG, or PNG · up to 5 files, 10 MB each</small>
              </label>
              {profile.supporting_documents?.length > 0 && (
                <ul className="document-list">
                  {profile.supporting_documents.map((document) => (
                    <li key={document.id}><span className="document-icon">▤</span><span className="document-name">{document.name}<small>{(document.size / 1024).toFixed(0)} KB</small></span><button type="button" onClick={() => downloadDocument(document)}>Download</button><button className="remove-document" type="button" onClick={() => removeDocument(document.id)} aria-label={`Remove ${document.name}`}>×</button></li>
                  ))}
                </ul>
              )}
            </fieldset>

            <div className="profile-save-row"><span>Changes are saved to your authenticated company workspace.</span><button className="primary-button" type="submit" disabled={saving}>{saving && <span className="spinner" />}{saving ? "Saving…" : "Save general information"}</button></div>
          </form>
        )}
      </section>

      <section className="profile-section future-section" aria-labelledby="tender-requirements-heading">
        <div className="profile-section-heading"><div><span className="section-kicker">SECTION 2 · FUTURE</span><h2 id="tender-requirements-heading">Tender-Specific Requirements</h2><p>Requirements will be organized separately for each tender.</p></div><span className="future-badge">COMING LATER</span></div>
        <div className="future-message"><span className="future-icon">⌁</span><p>Tender-specific requirements will appear here after you analyse a tender.</p></div>
      </section>

      <footer className="profile-footer"><span>© 2026 TenderForge</span><span>Company Profile</span></footer>
    </main>
  );
}
