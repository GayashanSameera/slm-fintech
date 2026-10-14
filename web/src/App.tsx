// The console: connect with an API key, upload documents, watch them being processed.
//
// Where things live:
//   * API URL     config.json, written by scripts/deploy_web.py next to the built site
//   * API key     sessionStorage ("slm-key"): this tab only, typed in by the client
//   * file names  localStorage ("slm-names"): the API never stores file names (privacy),
//                 so this browser remembers which file each job id was

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, type Settings } from "./api";
import type { Job, TrackedJob } from "./types";
import { FINAL_STATUSES } from "./types";
import { KeyDialog } from "./components/KeyDialog";
import { Uploader, type Flow } from "./components/Uploader";
import { JobList } from "./components/JobList";
import { JobDetail } from "./components/JobDetail";

const SAMPLES = [
  { name: "sample-statement.pdf", label: "Bank statement PDF", title: "Digital statement: Python parser + reconciliation + AI check" },
  { name: "sample-invoice.png", label: "Invoice image", title: "Scanned invoice: read by Claude Haiku" },
];

const store = {
  get: (s: Storage, k: string) => { try { return s.getItem(k); } catch { return null; } },
  set: (s: Storage, k: string, v: string) => { try { s.setItem(k, v); } catch { /* private mode */ } },
  del: (s: Storage, k: string) => { try { s.removeItem(k); } catch { /* ignore */ } },
};

export default function App() {
  const [config, setConfig] = useState<{ apiUrl: string; environment?: string } | null>(null);
  const [settings, setSettings] = useState<Settings>({ apiUrl: "", apiKey: store.get(sessionStorage, "slm-key") || "" });
  const [connection, setConnection] = useState<{ ok: boolean; detail: string } | null>(null);
  const [askKey, setAskKey] = useState(false);
  const [jobs, setJobs] = useState<Map<string, TrackedJob>>(new Map());
  const [selected, setSelected] = useState<string>();
  const [availableSamples, setAvailableSamples] = useState<typeof SAMPLES>([]);
  const polling = useRef(new Set<string>());
  const names = useRef<Record<string, string>>(JSON.parse(store.get(localStorage, "slm-names") || "{}"));
  const [, tick] = useState(0);

  const client = useMemo(() => new ApiClient(settings), [settings]);

  // --- load config.json, check which samples this build includes -------------------------
  useEffect(() => {
    fetch("config.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { apiUrl: "" }))
      .catch(() => ({ apiUrl: "" }))
      .then((c) => {
        setConfig(c);
        setSettings((s) => ({ ...s, apiUrl: s.apiUrl || store.get(sessionStorage, "slm-url") || c.apiUrl || "" }));
      });
    Promise.all(SAMPLES.map((s) => fetch(`samples/${s.name}`, { method: "HEAD" }).then((r) => (r.ok ? s : null)).catch(() => null)))
      .then((found) => setAvailableSamples(found.filter(Boolean) as typeof SAMPLES));
  }, []);

  // --- connect whenever the settings change -----------------------------------------------
  useEffect(() => {
    if (!config) return;
    if (!settings.apiUrl || !settings.apiKey) { setAskKey(true); return; }
    client.check().then((c) => {
      setConnection(c);
      if (!c.ok) setAskKey(true);
      else loadList();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, config]);

  // A ticking clock for the running timers.
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const upsert = useCallback((id: string, update: (prev?: TrackedJob) => TrackedJob, replaceId?: string) => {
    setJobs((prev) => {
      const next = new Map(prev);
      const old = next.get(replaceId ?? id);
      if (replaceId) next.delete(replaceId);
      next.set(id, update(old));
      return next;
    });
  }, []);

  // --- long-poll one job until it is final -------------------------------------------------
  const poll = useCallback(async (id: string) => {
    if (polling.current.has(id)) return;
    polling.current.add(id);
    try {
      for (;;) {
        let job: Job;
        try { job = await client.getJob(id, 20); }
        catch { await new Promise((r) => setTimeout(r, 3000)); continue; }
        upsert(id, (prev) => ({ ...(prev as TrackedJob), job,
                                finishedAt: FINAL_STATUSES.has(job.status) ? (prev?.finishedAt ?? Date.now()) : undefined }));
        if (FINAL_STATUSES.has(job.status)) return;
      }
    } finally {
      polling.current.delete(id);
    }
  }, [client, upsert]);

  async function loadList() {
    try {
      const list = await client.listJobs(30);
      setJobs((prev) => {
        const next = new Map(prev);
        for (const job of list) {
          const known = next.get(job.id);
          if (!known) next.set(job.id, { job, fileName: names.current[job.id] || job.id });
          else if (!FINAL_STATUSES.has(known.job.status)) next.set(job.id, { ...known, job: { ...known.job, ...job } });
        }
        return next;
      });
      list.filter((j) => !FINAL_STATUSES.has(j.status)).forEach((j) => poll(j.id));
    } catch { /* shown by the connection badge */ }
  }

  // --- send files ----------------------------------------------------------------------------
  function sendFiles(files: File[], documentType: string, flow: Flow) {
    for (const file of files) {
      const tmp = `local-${Math.random().toString(36).slice(2)}`;
      upsert(tmp, () => ({ job: { id: tmp, status: "queued" }, fileName: file.name, startedAt: Date.now() }));
      setSelected(tmp);
      client.extract(file, documentType, flow).then(
        (job) => {
          names.current[job.id] = file.name;
          store.set(localStorage, "slm-names", JSON.stringify(names.current));
          upsert(job.id, (prev) => ({ ...(prev as TrackedJob), job,
                                      finishedAt: FINAL_STATUSES.has(job.status) ? Date.now() : undefined }), tmp);
          setSelected((s) => (s === tmp ? job.id : s));
          if (!FINAL_STATUSES.has(job.status)) poll(job.id);
        },
        (err: Error) => upsert(tmp, (prev) => ({ ...(prev as TrackedJob), localError: err.message, finishedAt: Date.now() })),
      );
    }
  }

  async function openJob(id: string) {
    setSelected(id);
    const t = jobs.get(id);
    if (t && t.job.status === "succeeded" && !t.job.result && id.startsWith("ext_")) {
      try { const job = await client.getJob(id); upsert(id, (prev) => ({ ...(prev as TrackedJob), job })); } catch { /* ignore */ }
    }
  }

  async function deleteJob(id: string) {
    if (!confirm("Delete this job, its file and its result? This can't be undone.")) return;
    await client.deleteJob(id).catch(() => undefined);
    setJobs((prev) => { const next = new Map(prev); next.delete(id); return next; });
    delete names.current[id];
    store.set(localStorage, "slm-names", JSON.stringify(names.current));
    setSelected(undefined);
  }

  function saveSettings(s: Settings) {
    store.set(sessionStorage, "slm-key", s.apiKey);
    if (s.apiUrl !== config?.apiUrl) store.set(sessionStorage, "slm-url", s.apiUrl);
    setConnection(null);
    setAskKey(false);
    setSettings(s);
  }

  function disconnect() {
    store.del(sessionStorage, "slm-key");
    setJobs(new Map());
    setSelected(undefined);
    setSettings((s) => ({ ...s, apiKey: "" }));
    setConnection(null);
  }

  const list = [...jobs.values()].sort((a, b) =>
    (b.startedAt ?? 0) - (a.startedAt ?? 0) || b.job.id.localeCompare(a.job.id));

  return (
    <>
      <header>
        <h1>Extraction Console</h1>
        {config?.environment && config.environment !== "prod" && <span className="pill">{config.environment}</span>}
        <span className="spacer" />
        {connection && (
          <span className={`pill ${connection.ok ? "ok" : "bad"}`}>
            {connection.ok ? `Connected · key …${settings.apiKey.slice(-4)}` : connection.detail}
          </span>
        )}
        <button type="button" onClick={() => setAskKey(true)}>API key</button>
        {settings.apiKey && <button type="button" onClick={disconnect}>Disconnect</button>}
      </header>

      <main>
        <div className="column">
          <Uploader onFiles={sendFiles} samples={availableSamples} disabled={!connection?.ok} />
          <JobList jobs={list} selected={selected} onSelect={openJob} onRefresh={loadList} />
        </div>
        <JobDetail tracked={selected ? jobs.get(selected) : undefined} onDelete={deleteJob} />
      </main>

      {askKey && config && (
        <KeyDialog
          initial={settings}
          lockedUrl={Boolean(config.apiUrl)}
          error={connection && !connection.ok ? connection.detail : undefined}
          onSave={saveSettings}
          onCancel={connection?.ok ? () => setAskKey(false) : undefined}
        />
      )}
    </>
  );
}
