// The extraction API client used by the console.
//
// It follows docs/integration-guide.md exactly, so it doubles as a working example
// for client developers:
//   * the API key travels as "Authorization: Bearer <key>" on every call;
//   * files up to 4 MB use the DIRECT flow (file in the request, wait up to 25 s);
//     larger files use the UPLOAD-LINK flow (one-time S3 POST, fields first, file LAST);
//   * results are fetched with long polling (?wait=20), never a tight loop.
//
// Security: the key is the CLIENT's own key, typed into the console. It is kept in
// sessionStorage (this tab only, gone when the tab closes) and sent only to the API.

import type { Job } from "./types";

export const DIRECT_LIMIT = 4 * 1024 * 1024;

export interface Settings {
  apiUrl: string;
  apiKey: string;
}

export class ApiClient {
  constructor(private settings: Settings) {}

  private url(path: string): string {
    return this.settings.apiUrl.replace(/\/+$/, "") + path;
  }

  /** One API call. Returns status + parsed JSON (or a synthetic error for network failures). */
  async call(method: string, path: string, init: RequestInit = {}): Promise<{ status: number; body: any }> {
    try {
      const r = await fetch(this.url(path), {
        ...init,
        method,
        headers: { Authorization: `Bearer ${this.settings.apiKey}`, ...(init.headers || {}) },
      });
      let body: any = null;
      try { body = await r.json(); } catch { /* empty body (e.g. 204) */ }
      return { status: r.status, body };
    } catch (e) {
      return { status: 0, body: { error: { code: "network_error", message: "Cannot reach the API (network or CORS)." } } };
    }
  }

  /** Health and key check: is the API reachable, and is the key accepted? */
  async check(): Promise<{ ok: boolean; detail: string }> {
    try {
      const h = await fetch(this.url("/healthz"));
      if (!h.ok) return { ok: false, detail: `API health check returned ${h.status}` };
    } catch {
      return { ok: false, detail: "Cannot reach the API" };
    }
    const t = await this.call("GET", "/v1/document-types");
    if (t.status === 200) return { ok: true, detail: "Connected" };
    if (t.status === 401) return { ok: false, detail: "API key not accepted" };
    if (t.status === 403) return { ok: false, detail: t.body?.error?.code === "tenant_suspended" ? "Account suspended" : "Access denied" };
    return { ok: false, detail: t.body?.error?.message || `HTTP ${t.status}` };
  }

  /**
   * Send one document. Returns the job as the API answers it:
   *   direct flow   -> final job (200) or queued job (202)
   *   upload link   -> job with status "uploaded" once S3 accepted the file
   */
  async extract(file: File, documentType: string, flow: "auto" | "direct" | "presigned"): Promise<Job> {
    const useDirect = flow === "direct" || (flow === "auto" && file.size <= DIRECT_LIMIT);
    if (useDirect) {
      const form = new FormData();
      if (documentType !== "auto") form.append("document_type", documentType);
      form.append("file", file, file.name);
      const r = await this.call("POST", "/v1/extractions?wait=25", { body: form });
      if (!r.body?.id) throw new Error(errorText(r));
      return r.body as Job;
    }

    // Upload-link flow, step 1: create the job and get a one-time S3 upload link.
    const r = await this.call("POST", "/v1/extractions", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(documentType !== "auto" ? { document_type: documentType } : {}),
    });
    if (r.status !== 201 || !r.body?.upload) throw new Error(errorText(r));
    const job = r.body as Job;

    // Step 2: upload straight to S3. Every signed field first, the file LAST.
    const form = new FormData();
    for (const [k, v] of Object.entries(job.upload!.fields)) form.append(k, v);
    form.append("file", file, file.name);
    let s3Status = 0;
    try { s3Status = (await fetch(job.upload!.url, { method: "POST", body: form })).status; } catch { /* network */ }
    if (s3Status !== 204 && s3Status !== 200) {
      throw new Error(s3Status === 403 ? "S3 refused the upload (link expired or file too large)." : `S3 upload failed (HTTP ${s3Status}).`);
    }
    const { upload: _signedFields, ...rest } = job;      // the signed fields are single-use: don't keep them
    void _signedFields;
    return { ...rest, status: "awaiting_upload" };
  }

  /** Long-poll one job: the server holds the request up to `wait` seconds until it's final. */
  async getJob(id: string, wait = 0): Promise<Job> {
    const r = await this.call("GET", `/v1/extractions/${encodeURIComponent(id)}?wait=${wait}`);
    if (r.status !== 200) throw new Error(errorText(r));
    return r.body as Job;
  }

  async listJobs(limit = 30): Promise<Job[]> {
    const r = await this.call("GET", `/v1/extractions?limit=${limit}`);
    if (r.status !== 200) throw new Error(errorText(r));
    return (r.body?.data || []) as Job[];
  }

  async deleteJob(id: string): Promise<void> {
    const r = await this.call("DELETE", `/v1/extractions/${encodeURIComponent(id)}`);
    if (r.status !== 204 && r.status !== 404) throw new Error(errorText(r));
  }
}

export function errorText(r: { status: number; body: any }): string {
  const e = r.body?.error;
  if (e?.code) return e.message ? `${e.code}: ${e.message}` : e.code;
  return r.status ? `HTTP ${r.status}` : "Network error";
}
