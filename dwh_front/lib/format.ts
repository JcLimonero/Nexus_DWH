/** Las fechas del backend son TIMESTAMP sin zona: se muestran tal cual (hora del servidor de BD). */
export function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return value;
  return `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}${m[6] ? ":" + m[6] : ""}`;
}

export function fmtSeconds(total: number): string {
  if (!total && total !== 0) return "—";
  if (total % 86400 === 0) return `${total / 86400} d`;
  if (total % 3600 === 0) return `${total / 3600} h`;
  if (total % 60 === 0) return `${total / 60} min`;
  return `${total} s`;
}

export function fmtNumber(n: number | null | undefined): string {
  return new Intl.NumberFormat("es-MX").format(n ?? 0);
}

export const SOURCE_TYPE_LABELS: Record<string, string> = {
  sqlserver: "SQL Server",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  pervasive: "Pervasive",
  firebird: "Firebird",
};

export const DEFAULT_PORTS: Record<string, number> = {
  sqlserver: 1433,
  mysql: 3306,
  postgresql: 5432,
  pervasive: 1583,
  firebird: 3050,
};

export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

/**
 * Zona horaria de visualización para fechas UTC (timestamptz) de las tablas
 * nuevas (instalaciones, ejecuciones). En BD se guardan en UTC.
 * Configurable con NEXT_PUBLIC_DWH_TIMEZONE (defecto America/Mexico_City).
 */
export const DISPLAY_TZ = process.env.NEXT_PUBLIC_DWH_TIMEZONE || "America/Mexico_City";

/** Fecha UTC (ISO con Z/offset) → hora en DISPLAY_TZ con etiqueta de zona explícita. */
export function fmtDateTz(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("es-MX", {
    timeZone: DISPLAY_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(d);
}

/** Etiqueta corta de la zona de visualización (p. ej. "GMT-6"). */
export function tzLabel(): string {
  try {
    const parts = new Intl.DateTimeFormat("es-MX", { timeZone: DISPLAY_TZ, timeZoneName: "short" }).formatToParts(new Date());
    return `${DISPLAY_TZ} (${parts.find((p) => p.type === "timeZoneName")?.value ?? ""})`;
  } catch {
    return DISPLAY_TZ;
  }
}

/** "hace 3 min" a partir de una fecha UTC. */
export function fmtAgo(value: string | null | undefined): string {
  if (!value) return "nunca";
  const ms = Date.now() - new Date(value).getTime();
  if (Number.isNaN(ms)) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `hace ${s} s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}

export function secondsSince(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : (Date.now() - t) / 1000;
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.round(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
