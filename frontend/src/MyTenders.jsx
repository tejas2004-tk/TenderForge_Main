import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE_URL, request } from "./auth.js";

function formatSize(size) {
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value) {
  return new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export default function MyTenders({ user, onBack, onAnalyse }) {
  const fileInput = useRef(null);
  const [tenders, setTenders] = useState([]);
  const [maxSizeMb, setMaxSizeMb] = useState(25);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [processingTenderId, setProcessingTenderId] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState(null);

  const loadTenders = useCallback(async () => {
    setLoading(true);
    try {
      const data = await request("/tenders");
      setTenders(data.tenders);
      setMaxSizeMb(data.max_upload_size_mb || 25);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadTenders(); }, [loadTenders]);

  async function uploadFile(file) {
    if (!file) return;
    setNotice(null);
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setNotice({ kind: "error", message: "Only PDF files can be uploaded." });
      return;
    }
    if (file.size > maxSizeMb * 1024 * 1024) {
      setNotice({ kind: "error", message: `This file is larger than the ${maxSizeMb} MB upload limit.` });
      return;
    }

    const form = new FormData();
    form.append("file", file);
    setUploading(true);
    try {
      const data = await request("/tenders", { method: "POST", body: form });
      await loadTenders();
      setNotice({ kind: data.duplicate ? "info" : "success", message: data.message });
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function downloadTender(tender) {
    setNotice(null);
    try {
      const response = await fetch(`${API_BASE_URL}/tenders/${tender.id}/download`, { credentials: "include" });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || "Could not download this tender.");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = window.document.createElement("a");
      link.href = url; link.download = tender.filename; link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    }
  }

  async function removeTender(tender) {
    if (!window.confirm(`Delete ${tender.filename}? This also removes the uploaded file.`)) return;
    setNotice(null);
    try {
      await request(`/tenders/${tender.id}`, { method: "DELETE" });
      setTenders((current) => current.filter((item) => item.id !== tender.id));
      setNotice({ kind: "success", message: "Tender and uploaded file deleted." });
    } catch (error) {
      setNotice({ kind: "error", message: error.message });
    }
  }

  async function processTender(tender) {
    setNotice(null);
    setProcessingTenderId(tender.id);
    setTenders((current) => current.map((item) => item.id === tender.id
      ? { ...item, extraction: { ...item.extraction, status: "processing", error_message: null } }
      : item));
    try {
      const data = await request(`/tenders/${tender.id}/process`, { method: "POST" });
      const extraction = data.extraction;
      setTenders((current) => current.map((item) => item.id === tender.id ? { ...item, extraction } : item));
      if (extraction.status === "completed") {
        setNotice({ kind: "success", message: `PDF processed: ${extraction.page_count} pages and ${extraction.total_chunks} text chunks.` });
      } else if (extraction.status === "needs_ocr") {
        setNotice({ kind: "info", message: "Scanned PDF — OCR required. OCR is not available yet." });
      } else if (extraction.status === "failed") {
        setNotice({ kind: "error", message: extraction.error_message || "Processing failed. You can retry." });
      } else {
        setNotice({ kind: "info", message: "PDF processing is already in progress." });
      }
    } catch (error) {
      setTenders((current) => current.map((item) => item.id === tender.id
        ? { ...item, extraction: { ...item.extraction, status: "failed", error_message: error.message } }
        : item));
      setNotice({ kind: "error", message: error.message });
    } finally {
      setProcessingTenderId(null);
    }
  }

  function handleDrop(event) {
    event.preventDefault(); setDragging(false);
    uploadFile(event.dataTransfer.files[0]);
  }

  return (
    <main className="tenders-page">
      <header className="topbar tenders-topbar">
        <a className="brand" href="#home" aria-label="TenderForge home" onClick={(event) => { event.preventDefault(); onBack(); }}>
          <span className="brand-mark"><span /></span><span>Tender<span className="brand-light">Forge</span></span>
        </a>
        <div className="topbar-actions"><span className="user-name">{user.company_name}</span><button className="logout-button" onClick={onBack}>Back to dashboard</button></div>
      </header>

      <section className="tenders-hero">
        <button className="profile-back" onClick={onBack}>← <span>Dashboard</span></button>
        <div className="eyebrow"><span /> YOUR WORKSPACE</div>
        <div className="tenders-title-row"><div><h1>My Tenders</h1><p>Keep your tender documents together in one place.</p></div><button className="primary-button upload-top-button" onClick={() => fileInput.current?.click()} disabled={uploading}>{uploading ? <><span className="spinner" />Uploading…</> : <>Upload Tender <span>↑</span></>}</button></div>
      </section>

      {notice && <div className={`tender-notice ${notice.kind}`} role="status">{notice.message}</div>}

      <label className={`tender-dropzone ${dragging ? "dragging" : ""} ${uploading ? "uploading" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={handleDrop}>
        <input ref={fileInput} type="file" accept="application/pdf,.pdf" onChange={(event) => uploadFile(event.target.files?.[0])} disabled={uploading} />
        <span className="tender-upload-icon">↑</span>
        <span className="dropzone-title">{uploading ? "Uploading your tender…" : "Drop a tender PDF here"}</span>
        <span className="dropzone-subtitle">or click to browse · PDF only · up to {maxSizeMb} MB</span>
      </label>

      <section className="tender-list-section" aria-labelledby="tenders-list-heading">
        <div className="tender-list-heading"><div><span className="section-kicker">DOCUMENT LIBRARY</span><h2 id="tenders-list-heading">Uploaded tenders <span className="tender-count">{tenders.length}</span></h2></div><button className="refresh-button" onClick={loadTenders} disabled={loading}>↻ <span>Refresh</span></button></div>

        {loading ? <div className="tender-loading"><span className="spinner" />Loading your tenders…</div> : tenders.length === 0 ? (
          <div className="tender-empty"><span className="empty-document-icon">▤</span><h3>No tenders yet</h3><p>Upload a PDF tender to start building your library.</p><button className="secondary-button" onClick={() => fileInput.current?.click()}>Upload your first tender</button></div>
        ) : (
          <div className="tender-table-wrap"><table className="tender-table"><thead><tr><th>TENDER</th><th>UPLOADED</th><th>FILE SIZE</th><th>STATUS</th><th>ACTIONS</th></tr></thead><tbody>
            {tenders.map((tender) => (
              <tr key={tender.id}>
                <td><div className="tender-file-cell"><span className="pdf-icon">PDF</span><span className="tender-filename" title={tender.filename}>{tender.filename}<small className={`extraction-detail ${tender.extraction?.status || "not_processed"}`}>{tender.extraction?.status === "completed" ? `${tender.extraction.page_count} pages · ${tender.extraction.total_chunks} chunks` : tender.extraction?.status === "needs_ocr" ? "Scanned PDF · OCR required" : tender.extraction?.status === "failed" ? tender.extraction.error_message || "Processing failed" : tender.extraction?.status === "processing" ? "Processing PDF…" : ""}</small></span></div></td>
                <td>{formatDate(tender.uploaded_at)}</td>
                <td>{formatSize(tender.size)}</td>
                <td><span className={`tender-status ${tender.extraction?.status || "uploaded"}`}><i />{tender.extraction?.status === "completed" ? "Completed" : tender.extraction?.status === "needs_ocr" ? "OCR required" : tender.extraction?.status === "failed" ? "Processing failed" : tender.extraction?.status === "processing" ? "Processing" : tender.status}</span></td>
                <td><div className="tender-actions"><button className="text-action" onClick={() => downloadTender(tender)}>Download</button><button className="text-action delete-action" onClick={() => removeTender(tender)}>Delete</button><button className="process-button" onClick={() => processTender(tender)} disabled={processingTenderId === tender.id || tender.extraction?.status === "processing"}>{processingTenderId === tender.id ? <><span className="spinner" />Processing…</> : "Process PDF"}</button><button className="analyse-button" disabled={tender.extraction?.status === "processing"} onClick={() => onAnalyse(tender)}>{tender.analysis?.status === "processing" ? "View progress" : tender.analysis?.status === "paused_rate_limit" || tender.analysis?.status === "failed" ? "Resume analysis" : "Analyse Tender"}</button></div></td>
              </tr>
            ))}
          </tbody></table></div>
        )}
      </section>

      <footer className="tenders-footer"><span>© 2026 TenderForge</span><span>My Tenders</span></footer>
    </main>
  );
}
