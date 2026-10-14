// Small display helpers.

/** "running_balance_mismatch" -> "running balance mismatch" */
export const humanize = (s: string): string => s.replace(/_/g, " ");

/** True for the API's money format: "118000.50" (exactly two decimals, as a string). */
export const isMoney = (v: unknown): boolean => typeof v === "string" && /^-?\d+\.\d{2}$/.test(v);

/** Indian digit grouping for display only (the API value stays exact): 118000.50 -> 1,18,000.50 */
export function money(v: string): string {
  const negative = v.startsWith("-");
  const [whole, dec] = v.replace("-", "").split(".");
  const last3 = whole.slice(-3);
  const rest = whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${rest ? rest + "," : ""}${last3}.${dec}`;
}

export const number = (n: number | undefined): string => (n ?? 0).toLocaleString("en-IN");

/** AI cost per document is a fraction of a cent: show enough digits to be meaningful. */
export function usd(n: number | undefined): string {
  const v = n ?? 0;
  if (v === 0) return "USD 0";
  return `USD ${v < 0.01 ? v.toFixed(6).replace(/0+$/, "") : v.toFixed(4)}`;
}

/** Seconds between two ms timestamps, e.g. "7.4 s". */
export function duration(start?: number, end?: number): string {
  if (!start) return "";
  return `${(((end ?? Date.now()) - start) / 1000).toFixed(1)} s`;
}

/** Short model name: "us.anthropic.claude-haiku-5-5" -> "Claude Haiku 5.5" */
export function modelName(id?: string | null): string {
  if (!id) return "";
  if (id.includes("nova-lite")) return "Amazon Nova Lite";
  const m = id.match(/claude-(\w+)-(\d+)-(\d+)/);
  return m ? `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : id;
}
