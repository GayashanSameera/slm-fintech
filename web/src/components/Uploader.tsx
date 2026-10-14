// Upload panel: drop files (or pick them), choose type and flow, or send a synthetic sample.

import { useRef, useState } from "react";
import { DOCUMENT_TYPES } from "../types";

export type Flow = "auto" | "direct" | "presigned";

interface Props {
  onFiles: (files: File[], documentType: string, flow: Flow) => void;
  samples: { name: string; label: string; title: string }[];
  disabled: boolean;
}

const ACCEPT = ".pdf,.jpg,.jpeg,.png,.docx";
const MAX_BYTES = 25 * 1024 * 1024;

export function Uploader({ onFiles, samples, disabled }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [docType, setDocType] = useState("auto");
  const [flow, setFlow] = useState<Flow>("auto");
  const [problem, setProblem] = useState("");

  function send(list: FileList | File[]) {
    const files = Array.from(list);
    const tooBig = files.filter((f) => f.size > MAX_BYTES);
    setProblem(tooBig.length ? `Skipped (over 25 MB): ${tooBig.map((f) => f.name).join(", ")}` : "");
    const ok = files.filter((f) => f.size <= MAX_BYTES);
    if (ok.length) onFiles(ok, docType, flow);
  }

  async function sendSample(name: string) {
    const r = await fetch(`samples/${name}`);
    if (!r.ok) { setProblem("Sample not available in this build."); return; }
    const blob = await r.blob();
    send([new File([blob], name, { type: blob.type })]);
  }

  return (
    <section className="card">
      <h2>Upload a document</h2>
      <div
        className={`drop ${over ? "over" : ""} ${disabled ? "disabled" : ""}`}
        onClick={() => !disabled && input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); if (!disabled) send(e.dataTransfer.files); }}
        role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" && !disabled) input.current?.click(); }}
      >
        <strong>Drop files here or click to choose</strong>
        <span className="muted">PDF, JPG, PNG or DOCX · up to 25 MB · several at once is fine</span>
      </div>
      <input ref={input} type="file" multiple accept={ACCEPT} hidden
             onChange={(e) => { if (e.target.files) send(e.target.files); e.target.value = ""; }} />

      <label className="field">
        Document type
        <select value={docType} onChange={(e) => setDocType(e.target.value)}>
          {DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
      </label>
      <label className="field">
        How to send it
        <select value={flow} onChange={(e) => setFlow(e.target.value as Flow)}>
          <option value="auto">Automatic (in the request up to 4 MB, else upload link)</option>
          <option value="direct">Direct: file in the request, wait for the result</option>
          <option value="presigned">Upload link: S3 upload + malware scan, then fetch</option>
        </select>
      </label>
      {samples.length > 0 && (
        <div className="row">
          <span className="muted">Try a synthetic sample:</span>
          {samples.map((s) => (
            <button key={s.name} type="button" title={s.title} disabled={disabled} onClick={() => sendSample(s.name)}>
              {s.label}
            </button>
          ))}
        </div>
      )}
      {problem && <div className="notice warn">{problem}</div>}
    </section>
  );
}
