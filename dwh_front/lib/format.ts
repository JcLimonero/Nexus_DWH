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
