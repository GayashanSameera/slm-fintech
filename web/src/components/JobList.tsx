// The list of jobs (newest first), with live status.

import type { TrackedJob } from "../types";
import { FINAL_STATUSES } from "../types";
import { duration, humanize, usd } from "../format";

export function StatusPill({ status }: { status: string }) {
  const tone = status === "succeeded" ? "ok" : status === "failed" || status === "expired" ? "bad" : "run";
  return (
    <span className={`pill ${tone}`}>
      {!FINAL_STATUSES.has(status) && <span className="spin" aria-hidden />}
      {humanize(status)}
    </span>
  );
}

interface Props {
  jobs: TrackedJob[];
  selected?: string;
  onSelect: (id: string) => void;
  onRefresh: () => void;
}

export function JobList({ jobs, selected, onSelect, onRefresh }: Props) {
  // Total AI cost of the jobs shown. Duplicates answered from an earlier job cost nothing again.
  const total = jobs.reduce((sum, t) => sum + (t.job.deduplicated ? 0 : t.job.usage?.estimated_cost_usd ?? 0), 0);
  return (
    <section className="card">
      <div className="row between">
        <h2>Jobs</h2>
        <button type="button" onClick={onRefresh}>Refresh</button>
      </div>
      {total > 0 && <div className="muted small">AI cost of these jobs: {usd(total)}</div>}
      <div className="jobs">
        {jobs.length === 0 && <div className="empty">No jobs yet.</div>}
        {jobs.map((t) => (
          <button type="button" key={t.job.id} className={`job ${t.job.id === selected ? "sel" : ""}`}
                  onClick={() => onSelect(t.job.id)}>
            <div className="row between">
              <span className="name">{t.fileName}</span>
              <StatusPill status={t.localError ? "failed" : t.job.status} />
            </div>
            <div className="meta">
              {[t.job.document_type && humanize(t.job.document_type),
                t.job.method === "parser" ? "parser" : t.job.method === "ai" ? "AI" : "",
                // Jobs from before costs were reported have no estimate: show nothing, not "USD 0".
                t.job.usage?.estimated_cost_usd !== undefined ? usd(t.job.usage.estimated_cost_usd) : "",
                duration(t.startedAt, t.finishedAt)].filter(Boolean).join(" · ")}
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
