// Shapes returned by the extraction API (docs/integration-guide.md, section 6), and the
// console's own bookkeeping type. Fields marked optional (?) are absent until they apply:
// e.g. `result` only once a job has succeeded, `error` only when it failed, `upload` only in
// the answer that creates an upload-link job.

export type JobStatus =
  | "awaiting_upload" | "scanning" | "queued" | "processing"
  | "succeeded" | "failed" | "expired";

// A job in one of these states never changes again: stop polling it.
export const FINAL_STATUSES: ReadonlySet<string> = new Set(["succeeded", "failed", "expired"]);

// Token use of one model within one job (usage.by_model[]).
export interface ModelUsage {
  model_id: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  estimated_cost_usd?: number;
}

// Token use and estimated AI cost of one job, as returned by the API (app/usage.py summarize).
export interface Usage {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  estimated_cost_usd?: number;   // AI (Bedrock) cost at list prices: an estimate
  by_model: ModelUsage[];
}

// The job object returned by every extraction endpoint.
export interface Job {
  id: string;
  status: JobStatus;
  flow?: "direct" | "presigned";
  document_type?: string;
  document_type_source?: "client" | "detected" | "parser";
  document_type_confidence?: number;
  schema_version?: string;
  page_count?: number;
  method?: "parser" | "ai";
  parser_id?: string;
  verified_by?: string | null;
  model_id?: string | null;
  warnings?: string[];
  result?: Record<string, unknown>;
  usage?: Usage;
  error?: { code: string; message?: string };
  deduplicated?: boolean;
  created_at?: string;
  completed_at?: string;
  upload?: { method: string; url: string; fields: Record<string, string>; max_bytes: number; expires_at: string };
}

// The body of every error response: {"error": {"code": "...", "message": "..."}}.
export interface ApiError {
  error: { code: string; message?: string };
}

/** One row in the console's job list: the API job plus what only this browser knows. */
export interface TrackedJob {
  job: Job;
  fileName: string;
  startedAt?: number;      // ms, when this browser sent it (for the timer)
  finishedAt?: number;
  localError?: string;     // a failure before the API returned a job (network, S3 upload)
}

// Choices for the "Document type" select; "auto" lets the API detect the type.
export const DOCUMENT_TYPES = [
  { value: "auto", label: "Detect automatically" },
  { value: "bank_statement", label: "Bank statement" },
  { value: "invoice", label: "Invoice" },
  { value: "payslip", label: "Payslip" },
  { value: "identity_document", label: "Identity document" },
  { value: "kyc_form", label: "KYC form" },
] as const;
