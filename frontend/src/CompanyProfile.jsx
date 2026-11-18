import { useEffect, useRef, useState } from "react";
import { API_BASE_URL, request } from "./auth.js";
import { formatEvidenceWarning, getGeneralProfileChecklist, getTenderCategoryView } from "./companyProfileUtils.js";

const GROUPS = [
  {
    title: "Company basic details",
    fields: [
      { name: "legal_name", label: "Legal company name", placeholder: "As shown on registration documents" },
      { name: "trade_name", label: "Trading name", placeholder: "Public or operating name" },
      { name: "company_type", label: "Company type / entity type", placeholder: "e.g. Private limited company" },
      { name: "year_of_establishment", label: "Year of establishment", placeholder: "e.g. 2018" },
      { name: "website", label: "Website", type: "url", placeholder: "https://company.com" },
      { name: "description", label: "Company overview", multiline: true, wide: true, placeholder: "A short introduction to your company" },
      { name: "address", label: "Registered address", multiline: true, wide: true, placeholder: "Street and building" },
      { name: "office_address", label: "Office address", multiline: true, wide: true, placeholder: "Operational office address" },
      { name: "city", label: "City" }, { name: "state", label: "State / province" },
      { name: "postal_code", label: "Postal code" }, { name: "country", label: "Country" },
      { name: "company_email", label: "Company email", type: "email" },
      { name: "company_phone", label: "Company phone", type: "tel" },
    ],
  },
  {
    title: "Registration details",
    fields: [
      { name: "registration_type", label: "Business type", placeholder: "e.g. Private limited company" },
      { name: "registration_number", label: "Registration number" },
      { name: "incorporation_date", label: "Date of incorporation", type: "date" },
      { name: "cin_llpin", label: "CIN / LLPIN", placeholder: "If applicable" },
      { name: "udyam_msme", label: "Udyam / MSME registration", placeholder: "If applicable" },
      { name: "gst_number", label: "GST number" },
      { name: "pan_number", label: "PAN number" },
      { name: "other_registration_numbers", label: "Other relevant registration numbers", multiline: true, wide: true, placeholder: "Add any additional registrations separately" },
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
    title: "Financial information",
    fields: [
      { name: "financial_year", label: "Financial year", placeholder: "e.g. 2025-26" },
      { name: "annual_turnover", label: "Annual turnover", placeholder: "Amount and currency" },
      { name: "net_worth", label: "Net worth", placeholder: "Amount and currency, if available" },
      { name: "turnover_current_year", label: "Current financial year" },
      { name: "turnover_current_amount", label: "Current year turnover", placeholder: "Amount and currency" },
      { name: "turnover_previous_year", label: "Previous financial year" },
      { name: "turnover_previous_amount", label: "Previous year turnover", placeholder: "Amount and currency" },
      { name: "audited_financial_information", label: "Audited financial information", multiline: true, wide: true, placeholder: "Add key audited financial details entered by you" },
    ],
  },
  {
    title: "Experience and capabilities",
    fields: [
      { name: "years_experience", label: "Years in business", type: "number", placeholder: "e.g. 8" },
      { name: "employee_count", label: "Number of employees", type: "number", placeholder: "e.g. 42" },
      { name: "sectors", label: "Sectors and experience", multiline: true, wide: true, placeholder: "Describe your areas of experience" },
      { name: "core_business_activities", label: "Core business activities", multiline: true, wide: true, placeholder: "Main business activities" },
      { name: "services", label: "Services offered", multiline: true, wide: true, placeholder: "List your main services" },
      { name: "product_categories", label: "Product categories", multiline: true, wide: true, placeholder: "Products or categories supplied" },
      { name: "industries_sectors_served", label: "Industries / sectors served", multiline: true, wide: true, placeholder: "Industries where the company has worked" },
    ],
  },
  {
    title: "Existing project summary",
    fields: [
      { name: "previous_projects", label: "Previous projects", multiline: true, wide: true, placeholder: "Project, client, year, and relevant details" },
    ],
  },
];

const EMPTY_PROFILE = Object.fromEntries(GROUPS.flatMap((group) => group.fields.map((field) => [field.name, ""])));
const EMPTY_PAST_PROJECT = {
  project_name: "", client_department: "", work_description: "",
  contract_value: "", completion_date: "", supporting_document_id: "",
};
const EMPTY_CERTIFICATION = {
  certification_name: "", issuing_authority: "", certificate_number: "",
  issue_date: "", expiry_date: "", supporting_document_id: "",
};
const PROFILE_FIELDS_BY_NAME = Object.fromEntries(GROUPS.flatMap((group) => group.fields.map((field) => [field.name, field])));

function cleanEntries(entries, emptyEntry) {
  if (!Array.isArray(entries)) return [];
  return entries.map((entry) => ({ ...emptyEntry, ...(entry || {}) }))
    .filter((entry) => Object.values(entry).some((value) => String(value || "").trim()));
}

export default function CompanyProfile({ user, onBack }) {
  const [profile, setProfile] = useState({
    ...EMPTY_PROFILE,
    legal_name: user.company_name,
    supporting_documents: [],
    past_projects: [],
    certification_entries: [],
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState(null);
  const [tenders, setTenders] = useState([]);
  const [selectedTenderId, setSelectedTenderId] = useState("");
  const [tenderWorkspace, setTenderWorkspace] = useState(null);
  const [tenderLoading, setTenderLoading] = useState(false);
  const [responseSaving, setResponseSaving] = useState("");
  const [profileFieldSaving, setProfileFieldSaving] = useState("");
  const [documentUploading, setDocumentUploading] = useState("");
  const [activeTab, setActiveTab] = useState("general");
  const [profileChangesSaved, setProfileChangesSaved] = useState(true);
  const [activeTenderCategory, setActiveTenderCategory] = useState("Eligibility & Company Documents");
  const tenderLoadSequence = useRef(0);

  useEffect(() => {
    request("/company-profile")
      .then((data) => {
        setProfile({
          ...EMPTY_PROFILE,
          ...data.profile,
          supporting_documents: Array.isArray(data.profile?.supporting_documents) ? data.profile.supporting_documents : [],
          past_projects: cleanEntries(data.profile?.past_projects, EMPTY_PAST_PROJECT),
          certification_entries: cleanEntries(data.profile?.certification_entries, EMPTY_CERTIFICATION),
        });
        setProfileChangesSaved(true);
      })
      .catch((error) => setMessage({ kind: "error", text: error.message }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    request("/tenders")
      .then((data) => setTenders(Array.isArray(data.tenders) ? data.tenders : []))
      .catch(() => setTenders([]));
  }, []);

  async function loadTenderWorkspace(tenderId) {
    const sequence = ++tenderLoadSequence.current;
    setSelectedTenderId(tenderId);
    setTenderWorkspace(null);
    if (!tenderId) {
      setTenderLoading(false);
      return;
    }
    setTenderLoading(true);
    try {
      const data = await request(`/company-profile/tenders/${tenderId}/responses`);
      if (sequence === tenderLoadSequence.current) setTenderWorkspace(data);
    } catch (error) {
      if (sequence === tenderLoadSequence.current) setMessage({ kind: "error", text: error.message });
    } finally {
      if (sequence === tenderLoadSequence.current) setTenderLoading(false);
    }
  }

  function updateCardResponse(requirementId, field, value) {
    setTenderWorkspace((current) => ({
      ...current,
      cards: current.cards.map((card) =>
        card.requirement_id === requirementId
          ? { ...card, response_data: { ...(card.response_data || {}), [field]: value } }
          : card),
    }));
  }

  function updateCardProfileValue(requirementId, field, value) {
    setTenderWorkspace((current) => ({
      ...current,
      cards: current.cards.map((card) => card.requirement_id === requirementId
        ? { ...card, response_data: { ...(card.response_data || {}), profile_values: { ...(card.response_data?.profile_values || {}), [field]: value } } }
        : card),
    }));
  }

  async function saveCardProfileFields(card) {
    const values = card.response_data?.profile_values || {};
    const editableFields = card.editable_profile_fields || [];
    const updates = Object.fromEntries([...new Set([...(card.matching_profile_fields || []), ...editableFields])]
      .filter((field) => PROFILE_FIELDS_BY_NAME[field] && (!String(profile[field] || "").trim() || editableFields.includes(field)) && String(values[field] || "").trim())
      .map((field) => [field, String(values[field]).trim()]));
    if (!Object.keys(updates).length) {
      setMessage({ kind: "error", text: "Enter at least one missing General Information value first." });
      return;
    }
    setProfileFieldSaving(card.requirement_id);
    setMessage(null);
    try {
      const nextProfile = { ...profile, ...updates };
      const saved = await request("/company-profile", { method: "PUT", body: JSON.stringify(nextProfile) });
      setProfile({ ...EMPTY_PROFILE, ...saved.profile,
        supporting_documents: Array.isArray(saved.profile?.supporting_documents) ? saved.profile.supporting_documents : [],
        past_projects: cleanEntries(saved.profile?.past_projects, EMPTY_PAST_PROJECT),
        certification_entries: cleanEntries(saved.profile?.certification_entries, EMPTY_CERTIFICATION),
      });
      setProfileChangesSaved(true);
      const workspace = await request(`/company-profile/tenders/${selectedTenderId}/responses`);
      setTenderWorkspace(workspace);
      setMessage({ kind: "success", text: "Missing details were saved to General Information and the tender match was refreshed." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    } finally {
      setProfileFieldSaving("");
    }
  }

  async function saveTenderResponse(card) {
    setResponseSaving(card.requirement_id);
    setMessage(null);
    try {
      const data = await request(`/company-profile/tenders/${selectedTenderId}/responses`, {
        method: "POST",
        body: JSON.stringify({
          requirement_id: card.requirement_id,
          response_data: card.response_data || {},
        }),
      });
      setTenderWorkspace(data);
      setMessage({ kind: "success", text: "Tender-specific response saved." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    } finally {
      setResponseSaving("");
    }
  }

  async function uploadTenderDocuments(card, event) {
    const files = event.target.files;
    if (!files?.length) return;
    const form = new FormData();
    for (const file of files) form.append("files", file);
    setDocumentUploading(card.requirement_id);
    setMessage(null);
    try {
      const data = await request(
        `/company-profile/tenders/${selectedTenderId}/responses/${encodeURIComponent(card.requirement_id)}/documents`,
        { method: "POST", body: form },
      );
      setTenderWorkspace(data);
      setMessage({ kind: "success", text: "Tender-specific document uploaded." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    } finally {
      setDocumentUploading("");
      event.target.value = "";
    }
  }

  async function removeTenderDocument(card, documentId) {
    try {
      const data = await request(
        `/company-profile/tenders/${selectedTenderId}/responses/${encodeURIComponent(card.requirement_id)}/documents/${documentId}`,
        { method: "DELETE" },
      );
      setTenderWorkspace(data);
      setMessage({ kind: "success", text: "Tender-specific document removed." });
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
  }

  async function downloadTenderDocument(card, document) {
    try {
      const response = await fetch(
        `${API_BASE_URL}/company-profile/tenders/${selectedTenderId}/responses/${encodeURIComponent(card.requirement_id)}/documents/${document.id}`,
        { credentials: "include" },
      );
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.message || "Could not download this document.");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = window.document.createElement("a");
      link.href = url;
      link.download = document.name;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
  }

  const tenderCategories = [
    "Eligibility & Company Documents",
    "Financial Requirements",
    "Experience & Past Performance",
    "Certifications & Authorizations",
    "Tender Declarations & Commitments",
  ];
  const profileChecklist = getGeneralProfileChecklist(profile, profileChangesSaved);
  const activeCategoryView = getTenderCategoryView(tenderWorkspace?.cards, activeTenderCategory);

  function updateField(event) {
    setProfileChangesSaved(false);
    setProfile((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  function updateEntry(collection, index, event) {
    setProfileChangesSaved(false);
    setProfile((current) => ({
      ...current,
      [collection]: current[collection].map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, [event.target.name]: event.target.value } : entry),
    }));
  }

  function addEntry(collection, emptyEntry) {
    setProfileChangesSaved(false);
    setProfile((current) => ({
      ...current,
      [collection]: [...(current[collection] || []), { ...emptyEntry }],
    }));
  }

  function removeEntry(collection, index) {
    setProfileChangesSaved(false);
    setProfile((current) => ({
      ...current,
      [collection]: current[collection].filter((_, entryIndex) => entryIndex !== index),
    }));
  }

  async function saveProfile(event) {
    event.preventDefault(); setSaving(true); setMessage(null);
    try {
      const data = await request("/company-profile", {
        method: "PUT",
        body: JSON.stringify(profile),
      });
      setProfile({
        ...EMPTY_PROFILE,
        ...data.profile,
        supporting_documents: Array.isArray(data.profile?.supporting_documents) ? data.profile.supporting_documents : [],
        past_projects: cleanEntries(data.profile?.past_projects, EMPTY_PAST_PROJECT),
        certification_entries: cleanEntries(data.profile?.certification_entries, EMPTY_CERTIFICATION),
      });
      setProfileChangesSaved(true);
      setMessage({ kind: "success", text: "General information saved." });
      if (selectedTenderId && getGeneralProfileChecklist(data.profile, true).complete) {
        await loadTenderWorkspace(selectedTenderId);
      }
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
        past_projects: (current.past_projects || []).map((entry) =>
          entry.supporting_document_id === documentId ? { ...entry, supporting_document_id: "" } : entry),
        certification_entries: (current.certification_entries || []).map((entry) =>
          entry.supporting_document_id === documentId ? { ...entry, supporting_document_id: "" } : entry),
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
        <button className="profile-back" onClick={onBack}>â† <span>Dashboard</span></button>
        <div className="eyebrow"><span /> COMPANY WORKSPACE</div>
        <h1>Company Profile</h1>
        <p>Keep your reusable company information ready for future tenders.</p>
      </div>

      <div className="profile-tabs" role="tablist" aria-label="Company Profile sections">
        <button type="button" role="tab" aria-selected={activeTab === "general"} className={activeTab === "general" ? "active" : ""} onClick={() => setActiveTab("general")}>General Information</button>
        <button type="button" role="tab" aria-selected={activeTab === "tender"} className={activeTab === "tender" ? "active" : ""} onClick={() => setActiveTab("tender")}>Tender Specific</button>
      </div>

      {message && <div className={`profile-notice ${message.kind}`} role="status">{message.text}</div>}

      {activeTab === "general" && (
      <section className="profile-section" aria-labelledby="general-information-heading">
        <div className="profile-section-heading">
          <div><span className="section-kicker">SECTION 1</span><h2 id="general-information-heading">General Information</h2><p>Reusable details about your company.</p></div>
          <span className="profile-saved-indicator">{profileChecklist.complete ? "READY FOR TENDER MATCHING" : "PROFILE INCOMPLETE"}</span>
        </div>

        <div className="profile-readiness" aria-live="polite">
          <div className="profile-readiness-heading"><strong>Company profile readiness</strong><span>{profileChecklist.completed} of 3 prerequisites complete</span></div>
          <div className="tender-progress-track"><span style={{ width: `${profileChecklist.completed * 100 / 3}%` }} /></div>
          {profileChecklist.complete
            ? <p>Required company details and identity proof are saved. Tender Specific is unlocked.</p>
            : <><p>Complete and save these items to unlock Tender Specific:</p><ul>{profileChecklist.missing.map((item) => <li key={item}>{item}</li>)}</ul></>}
        </div>

        {loading ? <div className="profile-loading"><span className="spinner" />Loading company informationâ€¦</div> : (
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

            <fieldset className="profile-group repeatable-profile-group">
              <legend>Past work experience</legend>
              <p className="group-hint">Maintain reusable project or work-order entries. Add only work experience you want available for future tenders.</p>
              {(profile.past_projects || []).map((entry, index) => (
                <div className="profile-entry" key={`past-project-${index}`}>
                  <div className="profile-entry-heading"><strong>Project / work {index + 1}</strong><button type="button" className="remove-entry" onClick={() => removeEntry("past_projects", index)}>Remove</button></div>
                  <div className="profile-fields">
                    <label className="profile-field"><span>Project / work name</span><input name="project_name" value={entry.project_name || ""} onChange={(event) => updateEntry("past_projects", index, event)} /></label>
                    <label className="profile-field"><span>Client / department</span><input name="client_department" value={entry.client_department || ""} onChange={(event) => updateEntry("past_projects", index, event)} /></label>
                    <label className="profile-field wide"><span>Work description</span><textarea name="work_description" rows={3} value={entry.work_description || ""} onChange={(event) => updateEntry("past_projects", index, event)} /></label>
                    <label className="profile-field"><span>Contract value</span><input name="contract_value" value={entry.contract_value || ""} onChange={(event) => updateEntry("past_projects", index, event)} placeholder="Amount and currency" /></label>
                    <label className="profile-field"><span>Completion date</span><input name="completion_date" type="date" value={entry.completion_date || ""} onChange={(event) => updateEntry("past_projects", index, event)} /></label>
                    <label className="profile-field wide"><span>Supporting document</span><select name="supporting_document_id" value={entry.supporting_document_id || ""} onChange={(event) => updateEntry("past_projects", index, event)}><option value="">No supporting document selected</option>{profile.supporting_documents.map((document) => <option value={document.id} key={document.id}>{document.name}</option>)}</select></label>
                  </div>
                </div>
              ))}
              <button type="button" className="secondary-button" onClick={() => addEntry("past_projects", EMPTY_PAST_PROJECT)}>+ Add past project</button>
            </fieldset>

            <fieldset className="profile-group repeatable-profile-group">
              <legend>Certifications and qualifications</legend>
              <p className="group-hint">Add any certification or qualification. The list is open and is not limited to predefined certificate types.</p>
              {(profile.certification_entries || []).map((entry, index) => (
                <div className="profile-entry" key={`certification-${index}`}>
                  <div className="profile-entry-heading"><strong>Certification / qualification {index + 1}</strong><button type="button" className="remove-entry" onClick={() => removeEntry("certification_entries", index)}>Remove</button></div>
                  <div className="profile-fields">
                    <label className="profile-field"><span>Certification name</span><input name="certification_name" value={entry.certification_name || ""} onChange={(event) => updateEntry("certification_entries", index, event)} /></label>
                    <label className="profile-field"><span>Issuing authority</span><input name="issuing_authority" value={entry.issuing_authority || ""} onChange={(event) => updateEntry("certification_entries", index, event)} /></label>
                    <label className="profile-field"><span>Certificate number</span><input name="certificate_number" value={entry.certificate_number || ""} onChange={(event) => updateEntry("certification_entries", index, event)} /></label>
                    <label className="profile-field"><span>Issue date</span><input name="issue_date" type="date" value={entry.issue_date || ""} onChange={(event) => updateEntry("certification_entries", index, event)} /></label>
                    <label className="profile-field"><span>Expiry date</span><input name="expiry_date" type="date" value={entry.expiry_date || ""} onChange={(event) => updateEntry("certification_entries", index, event)} /></label>
                    <label className="profile-field wide"><span>Supporting document</span><select name="supporting_document_id" value={entry.supporting_document_id || ""} onChange={(event) => updateEntry("certification_entries", index, event)}><option value="">No supporting document selected</option>{profile.supporting_documents.map((document) => <option value={document.id} key={document.id}>{document.name}</option>)}</select></label>
                  </div>
                </div>
              ))}
              <button type="button" className="secondary-button" onClick={() => addEntry("certification_entries", EMPTY_CERTIFICATION)}>+ Add certification</button>
            </fieldset>

            <fieldset className="profile-group document-group">
              <legend>General supporting documents</legend>
              <p className="group-hint">Upload reusable company documents such as registration certificates, tax documents, or certifications.</p>
              <label className="upload-dropzone">
                <input type="file" multiple accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png" onChange={uploadDocuments} disabled={uploading} />
                <span className="upload-icon">â†‘</span>
                <span>{uploading ? "Uploading documentsâ€¦" : "Choose files to upload"}</span>
                <small>PDF, Word, Excel, JPG, or PNG Â· up to 5 files, 10 MB each</small>
              </label>
              {profile.supporting_documents?.length > 0 && (
                <ul className="document-list">
                  {profile.supporting_documents.map((document) => (
                    <li key={document.id}><span className="document-icon">â–¤</span><span className="document-name">{document.name}<small>{(document.size / 1024).toFixed(0)} KB</small></span><button type="button" onClick={() => downloadDocument(document)}>Download</button><button className="remove-document" type="button" onClick={() => removeDocument(document.id)} aria-label={`Remove ${document.name}`}>Ã—</button></li>
                  ))}
                </ul>
              )}
            </fieldset>

            <div className="profile-save-row"><span>Changes are saved to your authenticated company workspace.</span><button className="primary-button" type="submit" disabled={saving}>{saving && <span className="spinner" />}{saving ? "Savingâ€¦" : "Save general information"}</button></div>
          </form>
        )}
      </section>
      )}

      {activeTab === "tender" && (
      <section className="profile-section tender-specific-section" aria-labelledby="tender-requirements-heading">
        <div className="profile-section-heading">
          <div><span className="section-kicker">SECTION 2</span><h2 id="tender-requirements-heading">Tender Specific</h2><p>Answers and documents needed only for the selected tender.</p></div>
          {tenderWorkspace && <span className="future-badge">{tenderWorkspace.progress.completed} of {tenderWorkspace.progress.total} complete</span>}
        </div>
        {profileChecklist.complete && <div className="tender-specific-controls">
          <label className="profile-field">
            <span>Select tender</span>
            <select value={selectedTenderId} onChange={(event) => loadTenderWorkspace(event.target.value)}>
              <option value="">Choose an analysed tender</option>
              {tenders.map((tender) => <option value={tender.id} key={tender.id}>{tender.filename}</option>)}
            </select>
          </label>
          {selectedTenderId && tenderWorkspace && <span className="tender-specific-tender-name">{tenderWorkspace.tender?.filename}</span>}
          {selectedTenderId && tenderWorkspace && <button type="button" className="secondary-button" onClick={() => loadTenderWorkspace(selectedTenderId)}>Refresh matching</button>}
        </div>}
        {!profileChecklist.complete ? (
          <div className="tender-locked-state">
            <h3>Complete General Information to continue</h3>
            <p>Complete and save your General Information to unlock Tender Specific requirements.</p>
            <ul>{profileChecklist.missing.map((item) => <li key={item}>{item}</li>)}</ul>
            <button type="button" className="primary-button" onClick={() => setActiveTab("general")}>Complete General Information</button>
          </div>
        ) : tenderLoading ? (
          <div className="profile-loading"><span className="spinner" />Loading tender requirementsâ€¦</div>
        ) : selectedTenderId && tenderWorkspace ? (
          <>
            {tenderWorkspace.progress.total > 0 && (
              <div className="tender-progress" aria-label="Tender-specific completion progress">
                <div><span>{tenderWorkspace.progress.completed} completed</span><span>{tenderWorkspace.progress.incomplete} remaining</span></div>
                <div className="tender-progress-track"><span style={{ width: `${tenderWorkspace.progress.total ? tenderWorkspace.progress.completed * 100 / tenderWorkspace.progress.total : 0}%` }} /></div>
              </div>
            )}
            {tenderWorkspace.cards.length === 0 ? (
              <div className="future-message"><span className="future-icon">âœ“</span><p>No additional tender-specific information is identified as needing action.</p></div>
            ) : (
              <div className="tender-specific-categories">
                <div className="tender-category-tabs" role="tablist" aria-label="Tender requirement categories">
                  {tenderCategories.map((category) => {
                    const view = getTenderCategoryView(tenderWorkspace.cards, category);
                    return <button type="button" role="tab" aria-selected={activeTenderCategory === category} className={activeTenderCategory === category ? "active" : ""} key={category} onClick={() => setActiveTenderCategory(category)}>
                      <span>{category}</span><small>{view.completed}/{view.total} complete</small>
                    </button>;
                  })}
                </div>
                <div className="tender-specific-cards" role="tabpanel" aria-label={activeTenderCategory}>
                        {activeCategoryView.cards.map((card) => {
                          const reusableFields = [...new Set([...(card.matching_profile_fields || []), ...(card.editable_profile_fields || [])])].filter((field) => PROFILE_FIELDS_BY_NAME[field]);
                          const missingProfileFields = reusableFields.filter((field) => !String(profile[field] || "").trim() || (card.editable_profile_fields || []).includes(field));
                          const projectProfileNeeded = (card.matching_profile_fields || []).includes("past_projects");
                          return (
                  <article className={`tender-response-card ${card.current_status}`} key={card.requirement_id}>
                    <div className="tender-response-heading">
                      <div><span className="tender-category">{card.category || "Tender requirement"}</span><h3>{card.requirement_title || "Requirement needs review"}</h3></div>
                      <span className={`tender-response-status ${card.current_status}`}>
                        {card.match_status === "satisfied" ? "Satisfied" : card.current_status === "completed" ? "Completed" : card.match_status === "review_required" ? "Review required" : card.match_status === "document_needed" ? "Document needed" : "Missing information"}
                      </span>
                    </div>
                    <p className="tender-response-description">{card.description || card.reason}</p>
                    {card.reason && <p className="tender-response-reason">{card.reason}</p>}
                    {card.source_page && <span className="tender-source-page">Source page {card.source_page}</span>}
                    {card.source_pages?.length > 0 && <span className="tender-source-page">Source pages {card.source_pages.join(", ")}</span>}
                    {card.preserved_responses?.length > 0 && (
                      <details className="tender-preserved-responses">
                        <summary>Previously saved answers for grouped requirements ({card.preserved_responses.length})</summary>
                        <ul>{card.preserved_responses.map((saved) => (
                          <li key={saved.requirement_id}>{saved.response_data?.value || (typeof saved.response_data?.confirmed === "boolean" ? (saved.response_data.confirmed ? "Yes" : "No") : "Saved response")}</li>
                        ))}</ul>
                      </details>
                    )}

                    {card.financial_subconditions?.length > 0 && (
                      <div className="tender-financial-subconditions">
                        <strong>Financial qualification details</strong>
                        <ul>{card.financial_subconditions.map((condition) => (
                          <li key={(condition.requirement_ids || []).join("|")}>
                            {condition.threshold && <span><b>Turnover threshold:</b> {condition.threshold} Â· </span>}
                            {condition.period && <span><b>Period:</b> {condition.period} Â· </span>}
                            {condition.method && <span><b>Calculation:</b> {condition.method.replace("_", " ")} Â· </span>}
                            {condition.evidence?.length > 0 && <span><b>Evidence:</b> {condition.evidence.join(" or ")} Â· </span>}
                            {condition.source_pages?.length > 0 && <span><b>Source pages:</b> {condition.source_pages.join(", ")} Â· </span>}
                            <span><b>Status:</b> {condition.status.replaceAll("_", " ")}</span>
                          </li>
                        ))}</ul>
                      </div>
                    )}
                    {card.financial_assessment?.length > 0 && <div className="tender-response-reason"><strong>Financial values checked</strong>{card.financial_assessment.map((assessment, index) => <p key={index}>Threshold: {assessment.threshold ?? "not stated"}; years: {assessment.financial_years?.join(", ") || "not specified"}; method: {assessment.calculation_method}. Available: {Object.entries(assessment.available_values || {}).map(([year, value]) => `${year}: ${value}`).join("; ") || "none"}. Missing years: {assessment.missing_years?.join(", ") || "none"}.</p>)}</div>}

                    {card.match_status === "missing_information" && missingProfileFields.length > 0 && (
                      <div className="tender-response-fields">
                        <strong>Missing General Information</strong>
                        <div className="profile-fields">
                          {missingProfileFields.map((fieldName) => {
                            const field = PROFILE_FIELDS_BY_NAME[fieldName];
                            const value = card.response_data?.profile_values?.[fieldName] ?? profile[fieldName] ?? "";
                            return <label className={field.wide ? "profile-field wide" : "profile-field"} key={fieldName}>
                              <span>{field.label}</span>
                              {field.multiline
                                ? <textarea rows={3} value={value} onChange={(event) => updateCardProfileValue(card.requirement_id, fieldName, event.target.value)} placeholder={field.placeholder || "Enter the missing company detail"} />
                                : <input type={field.type || "text"} value={value} onChange={(event) => updateCardProfileValue(card.requirement_id, fieldName, event.target.value)} placeholder={field.placeholder || "Enter the missing company detail"} />}
                            </label>;
                          })}
                        </div>
                        <button type="button" className="secondary-button" disabled={profileFieldSaving === card.requirement_id} onClick={() => saveCardProfileFields(card)}>
                          {profileFieldSaving === card.requirement_id ? "Savingâ€¦" : "Save missing details to General Information"}
                        </button>
                      </div>
                    )}
                    {(reusableFields.length > 0 || projectProfileNeeded) && card.match_status !== "satisfied" && (
                      <button type="button" className="text-button" onClick={() => setActiveTab("general")}>Open General Information to review reusable company details</button>
                    )}
                    {card.completion_requires_profile_match && missingProfileFields.length === 0 && <p className="tender-response-reason">This requirement can only be completed by correcting or adding the reusable profile data above, then refreshing the match.</p>}
                    {projectProfileNeeded && <p className="tender-response-reason">Add qualifying projects, dates, and supply details under Past work experience in General Information.</p>}

                    {card.evidence_warnings?.length > 0 && <div className="tender-response-reason"><strong>Extraction evidence warning</strong><ul>{card.evidence_warnings.map((warning, index) => <li key={index}>{formatEvidenceWarning(warning)}</li>)}</ul></div>}
                    {card.experience_assessment?.length > 0 && <div className="tender-response-reason"><strong>Past supply match details</strong>{card.experience_assessment.map((assessment, index) => <div key={index}><p>Required: {assessment.required_count || "at least one"} order(s){assessment.required_periods?.length ? ` during ${assessment.required_periods.join(", ")}` : ""}. Matching projects found: {assessment.matched_count}.</p>{assessment.similarity_terms?.length > 0 && <p>Similarity terms checked: {assessment.similarity_terms.join(", ")}.</p>}<ul>{assessment.projects_considered?.map((project, projectIndex) => <li key={`${project.project_name}-${projectIndex}`}>{project.project_name} ({project.completion_date}): {project.matched ? "matched" : `did not match${project.criteria_missing?.length ? ` â€” ${project.criteria_missing.join("; ")}` : ""}`}</li>)}</ul></div>)}</div>}
                    {card.match_status === "satisfied" || missingProfileFields.length > 0 || projectProfileNeeded || card.completion_requires_profile_match || card.suggested_input_type === "document" ? null : card.match_status === "review_required" ? (
                      <div className="tender-response-fields">
                        {(card.review_actions || []).map((action, index) => <p className="tender-response-reason" key={index}><strong>Action:</strong> {action}</p>)}
                        {(card.review_contexts || []).map((context, index) => <p className="tender-response-reason" key={index}><strong>Source{context.source_page ? ` page ${context.source_page}` : ""}:</strong> {context.evidence_text || context.condition || "Review the extracted clauses and verify the controlling requirement."}</p>)}
                        <label className="profile-field wide"><span>Resolution details</span><textarea rows={3} value={card.response_data?.value || ""} onChange={(event) => updateCardResponse(card.requirement_id, "value", event.target.value)} placeholder="Record the specific decision or evidence to verify" /></label>
                      </div>
                    ) : card.suggested_input_type === "number" ? (
                      <label className="profile-field"><span>Additional value</span><input type="number" value={card.response_data?.value ?? ""} onChange={(event) => updateCardResponse(card.requirement_id, "value", event.target.value)} placeholder="Enter the required amount or number" /></label>
                    ) : card.suggested_input_type === "confirmation" ? (
                      <div className="tender-confirmation-options">
                        <label><input type="radio" name={`confirm-${card.requirement_id}`} checked={card.response_data?.confirmed === true} onChange={() => updateCardResponse(card.requirement_id, "confirmed", true)} /> Yes</label>
                        <label><input type="radio" name={`confirm-${card.requirement_id}`} checked={card.response_data?.confirmed === false} onChange={() => updateCardResponse(card.requirement_id, "confirmed", false)} /> No</label>
                      </div>
                    ) : (
                      <label className="profile-field wide"><span>Response</span><textarea rows={3} value={card.response_data?.value || ""} onChange={(event) => updateCardResponse(card.requirement_id, "value", event.target.value)} placeholder="Enter the tender-specific information" /></label>
                    )}

                    {card.supporting_document_needed && (
                      <fieldset className="tender-document-area">
                        <legend>Supporting document</legend>
                        <label className="upload-dropzone">
                          <input type="file" multiple accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png" disabled={documentUploading === card.requirement_id} onChange={(event) => uploadTenderDocuments(card, event)} />
                          <span className="upload-icon">â†‘</span>
                          <span>{documentUploading === card.requirement_id ? "Uploadingâ€¦" : "Choose supporting documents"}</span>
                          <small>PDF, Word, Excel, JPG, or PNG Â· up to 5 files, 10 MB each</small>
                        </label>
                        {card.documents?.length > 0 && (
                          <ul className="document-list">
                            {card.documents.map((document) => (
                              <li key={document.id}><span className="document-icon">â–¤</span><span className="document-name">{document.name}<small>{(document.size / 1024).toFixed(0)} KB</small></span><button type="button" onClick={() => downloadTenderDocument(card, document)}>Download</button><button type="button" className="remove-document" onClick={() => removeTenderDocument(card, document.id)} aria-label={`Remove ${document.name}`}>Ã—</button></li>
                            ))}
                          </ul>
                        )}
                      </fieldset>
                    )}

                    <div className="tender-response-actions"><span>{card.mandatory ? "Mandatory requirement" : "Optional or conditional item"}</span>{card.match_status !== "satisfied" && missingProfileFields.length === 0 && !projectProfileNeeded && !card.completion_requires_profile_match && card.suggested_input_type !== "document" && <button type="button" className="primary-button" disabled={responseSaving === card.requirement_id} onClick={() => saveTenderResponse(card)}>{responseSaving === card.requirement_id ? "Savingâ€¦" : "Save response"}</button>}</div>
                  </article>
                          );
                        })}
                  {activeCategoryView.total === 0 && <div className="future-message"><p>No actionable requirements in this category.</p></div>}
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="future-message"><span className="future-icon">âŒ</span><p>Select a tender to view only the items that need your information or review.</p></div>
        )}
      </section>
      )}

      <footer className="profile-footer"><span>Â© 2026 TenderForge</span><span>Company Profile</span></footer>
    </main>
  );
}
