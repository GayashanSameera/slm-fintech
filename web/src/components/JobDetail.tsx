// Everything about one job, top to bottom:
//   1. file name + Delete
//   2. status timeline (the stages of the flow the job used)
//   3. verdict: "All checks passed" (green) or "Review needed" with each warning explained
//   4. facts: status, type, read by (parser or which AI model), verified by, pages, time,
//      tokens, AI cost, job id
//   5. per-model token and cost table
//   6. the result as readable tables, or the raw JSON exactly as the API returned it
//
// Everything shown comes from the API's job object (docs/integration-guide.md, section 6);
// only the file name and timer are local to this browser.

import { useState } from "react";
import type { TrackedJob } from "../types";
import { FINAL_STATUSES } from "../types";
import { duration, humanize, isMoney, modelName, money, number, usd } from "../format";
import { StatusPill } from "./JobList";

// What each warning means for someone deciding whether to trust the result.
const WARNING_HELP: Record<string, string> = {
  running_balance_mismatch: "Statement arithmetic doesn't hold: review before reconciling.",
  closing_balance_mismatch: "Opening + credits − debits ≠ closing: review before reconciling.",
  text_reconciliation_failed: "Rows don't match the PDF's text exactly: review before reconciling.",
  line_items_sum_mismatch: "Line items don't add up to the subtotal.",
  total_mismatch: "Subtotal + taxes ≠ total.",
  earnings_sum_mismatch: "Earnings don't add up to gross pay.",
  net_pay_mismatch: "Gross − deductions ≠ net pay.",
  pan_format: "A PAN has an invalid format.",
  gstin_format: "A GSTIN has an invalid format.",
  ifsc_format: "The IFSC has an invalid format.",
  document_expired: "The identity document has expired.",
  required_fields_missing: "The KYC form is missing name, date of birth or PAN.",
  document_type_mismatch: "Nothing of the requested type was found: check the file and type.",
};

// The stages a job goes through. The upload-link flow has two extra steps before the queue
// (upload received, malware scan). A failed or expired job marks the last step in red.
function Timeline({ status, flow }: { status: string; flow?: string }) {
  const steps = flow === "presigned"
    ? [["awaiting_upload", "uploaded"], ["scanning", "malware scan"], ["queued", "queued"], ["processing", "extracting"], ["succeeded", "done"]]
    : [["queued", "queued"], ["processing", "extracting"], ["succeeded", "done"]];
  const failed = status === "failed" || status === "expired";
  const index = failed ? steps.length - 1 : steps.findIndex(([s]) => s === status);
  return (
    <ol className="timeline">
      {steps.map(([key, label], i) => {
        const state = i < index ? "done" : i === index ? (failed ? "fail" : status === "succeeded" ? "done" : "now") : "";
        return <li key={key} className={state}>{failed && i === index ? humanize(status) : label}</li>;
      })}
    </ol>
  );
}

// One value in a table: "—" for null/empty, yes/no for booleans, Indian digit grouping for
// money (display only: the API value stays an exact 2-decimal string), JSON for objects.
function Cell({ v }: { v: unknown }) {
  if (v === null || v === undefined || v === "") return <span className="null">—</span>;
  if (typeof v === "boolean") return <>{v ? "yes" : "no"}</>;
  if (typeof v === "object") return <span className="mono">{JSON.stringify(v)}</span>;
  return <>{isMoney(v) ? money(v as string) : String(v)}</>;
}

// A table from a list of objects (transactions, line items, earnings, ...). The columns are
// every key seen in any row, so rows with missing fields still line up.
function Table({ rows }: { rows: Record<string, unknown>[] }) {
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r || {})))];
  return (
    <div className="scroll">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{humanize(c)}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c} className={isMoney(r?.[c]) ? "num" : ""}><Cell v={r?.[c]} /></td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// The result in three groups: simple fields first (as a key/value table), then nested
// objects (seller, buyer, address), then lists as tables. Field order is the API's, which
// follows the document type's schema.
function ResultView({ result }: { result: Record<string, unknown> }) {
  const scalars = Object.entries(result).filter(([, v]) => !Array.isArray(v) && (v === null || typeof v !== "object"));
  const objects = Object.entries(result).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v));
  const lists = Object.entries(result).filter(([, v]) => Array.isArray(v));
  return (
    <>
      <table className="kv"><tbody>
        {scalars.map(([k, v]) => <tr key={k}><td>{humanize(k)}</td><td className={isMoney(v) ? "num" : ""}><Cell v={v} /></td></tr>)}
      </tbody></table>
      {objects.map(([k, v]) => (
        <div key={k}>
          <h3>{humanize(k)}</h3>
          <table className="kv"><tbody>
            {Object.entries(v as Record<string, unknown>).map(([a, b]) => <tr key={a}><td>{humanize(a)}</td><td><Cell v={b} /></td></tr>)}
          </tbody></table>
        </div>
      ))}
      {lists.map(([k, v]) => (
        <div key={k}>
          <h3>{humanize(k)} ({(v as unknown[]).length})</h3>
          {(v as unknown[]).length ? <Table rows={v as Record<string, unknown>[]} /> : <span className="null">none</span>}
        </div>
      ))}
    </>
  );
}

interface Props {
  tracked?: TrackedJob;
  onDelete: (id: string) => void;
}

export function JobDetail({ tracked, onDelete }: Props) {
  const [view, setView] = useState<"table" | "json">("table");
  if (!tracked) return <section className="card"><div className="empty">Upload a document or pick a job to see its result here.</div></section>;
  const { job, fileName, localError } = tracked;
  const u = job.usage;

  // The summary boxes. "Verified by" says what independently confirmed the result: for a
  // parser result the AI model that re-read it (or "checks only" when the client chose no AI);
  // for an AI result the automatic checks it had to pass.
  const facts: [string, React.ReactNode][] = [
    ["Status", <StatusPill status={localError ? "failed" : job.status} />],
    ["Document type", job.document_type ? <>{humanize(job.document_type)}{job.document_type_source && <span className="muted"> ({job.document_type_source})</span>}</> : "—"],
    ["Read by", job.method === "parser" ? <>Python parser <span className="mono">{job.parser_id}</span></> : job.method === "ai" ? <>AI · {modelName(job.model_id)}</> : "—"],
    ["Verified by", job.method === "parser" ? (job.verified_by ? modelName(job.verified_by) : "checks only (no AI)") : job.method === "ai" ? (job.document_type === "bank_statement" ? "arithmetic + text reconciliation" : "consistency checks") : "—"],
    ["Pages", job.page_count ?? "—"],
    ["Time", duration(tracked.startedAt, tracked.finishedAt) || "—"],
    ["Tokens", u ? <>{number(u.input_tokens)} in · {number(u.output_tokens)} out<div className="muted small">{u.model_calls} model call{u.model_calls === 1 ? "" : "s"}{u.cache_read_tokens ? ` · ${number(u.cache_read_tokens)} cached` : ""}{job.deduplicated ? " · from an earlier identical upload" : ""}</div></> : "—"],
    ["AI cost", u?.estimated_cost_usd !== undefined ? <>{usd(u.estimated_cost_usd)}<div className="muted small">{u.estimated_cost_usd
        ? `≈ USD ${(u.estimated_cost_usd * 1000).toFixed(2)} per 1,000 documents like this` : "no AI model was called"}
        {job.deduplicated ? " · not charged again (earlier identical upload)" : ""}</div></> : "—"],
    ["Job id", <span className="mono small">{job.id}</span>],
  ];

  return (
    <section className="card">
      <div className="row between">
        <h2 className="title">{fileName}</h2>
        {job.id.startsWith("ext_") && <button type="button" className="danger" onClick={() => onDelete(job.id)}>Delete</button>}
      </div>
      {!localError && <Timeline status={job.status} flow={job.flow} />}
      {(localError || job.error) && (
        <div className="notice bad"><b>Failed:</b> <span className="mono">{localError || job.error?.code}</span></div>
      )}
      {job.status === "succeeded" && (job.warnings?.length ? (
        <div className="notice warn">
          <b>Review needed.</b>
          <ul>{job.warnings.map((w) => <li key={w}><span className="mono">{w}</span>: {WARNING_HELP[w] || humanize(w)}</li>)}</ul>
        </div>
      ) : (
        <div className="notice ok">All checks passed{job.method === "parser" ? ": every row reconciled with the document." : "."}</div>
      ))}
      <div className="facts">
        {facts.map(([k, v]) => <div className="fact" key={k}><div className="k">{k}</div><div className="v">{v}</div></div>)}
      </div>
      {u && u.by_model.length > 0 && (
        <>
          <h3>Model calls</h3>
          <Table rows={u.by_model.map((m) => ({ model: modelName(m.model_id), calls: m.calls, input_tokens: number(m.input_tokens),
                                               output_tokens: number(m.output_tokens), cached_tokens: number(m.cache_read_tokens),
                                               cost: usd(m.estimated_cost_usd) }))} />
          <p className="muted small">AI cost is an estimate at AWS Bedrock list prices for the model tokens only; your invoice is the reference.</p>
        </>
      )}
      {job.result ? (
        <>
          <div className="tabs">
            <button type="button" className={view === "table" ? "on" : ""} onClick={() => setView("table")}>Result</button>
            <button type="button" className={view === "json" ? "on" : ""} onClick={() => setView("json")}>Raw JSON</button>
            <button type="button" onClick={() => navigator.clipboard.writeText(JSON.stringify(job, null, 2))}>Copy JSON</button>
          </div>
          {view === "table" ? <ResultView result={job.result} /> : <pre>{JSON.stringify(job, null, 2)}</pre>}
        </>
      ) : !FINAL_STATUSES.has(job.status) && !localError ? (
        <div className="empty"><span className="spin" aria-hidden /> Working on it… this page updates by itself.</div>
      ) : null}
    </section>
  );
}
