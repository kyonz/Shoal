const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = (t - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return diff < 0 ? "just now" : "in a moment";
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["minute", 60],
    ["hour", 3600],
    ["day", 86400],
    ["week", 604800],
    ["month", 2592000],
    ["year", 31536000],
  ];
  let unit: Intl.RelativeTimeFormatUnit = "minute";
  let size = 60;
  for (const [u, s] of units) if (abs >= s) [unit, size] = [u, s];
  return rtf.format(Math.round(diff / size), unit);
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function duration(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function between(start?: string | null, end?: string | null, now = Date.now()): string {
  if (!start) return "—";
  return duration((end ? new Date(end).getTime() : now) - new Date(start).getTime());
}

export function usd(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return "<$0.01";
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: n < 100 ? 2 : 0 });
}

export function compact(n: number): string {
  return Intl.NumberFormat(undefined, { notation: "compact" }).format(n);
}

export function plural(n: number, word: string, many = `${word}s`) {
  return `${n} ${n === 1 ? word : many}`;
}
