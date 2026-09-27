"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, Info, PlugZap, XCircle } from "lucide-react";
import { api, qs } from "@/lib/api";
import type { ConnectionTest, ConnectionTestKind, EffectiveWarehouse, SslMode } from "@/lib/types";
import { cx, fmtAgo, fmtDuration } from "@/lib/format";
import { Badge, Button, Field, Input, Select, Spinner, Switch, Textarea } from "@/components/ui/primitives";
import { PasswordInput } from "@/components/password-input";
import { useToast } from "@/components/ui/feedback";

// ─────────────────────────────────────────────────────────────────────────────
// SSL/TLS
// ─────────────────────────────────────────────────────────────────────────────
export const SSL_MODES: { value: SslMode; label: string; help: string }[] = [
  { value: "disable", label: "disable — sin cifrado", help: "Nunca cifra. Solo si el DWH está en la misma red privada que el agente." },
  { value: "allow", label: "allow — cifra solo si el servidor lo exige", help: "Intenta sin cifrado primero. No recomendado." },
  { value: "prefer", label: "prefer — cifra si el servidor lo ofrece (predeterminado)", help: "Cifra si el servidor admite SSL; si no, se conecta sin cifrar. No verifica el certificado." },
  { value: "require", label: "require — cifrado obligatorio", help: "Falla si el servidor no admite SSL. Cifra pero no verifica la identidad del servidor (si indicas una CA, la verifica como verify-ca)." },
  { value: "verify-ca", label: "verify-ca — cifrado + CA verificada (CA obligatoria)", help: "Exige que el certificado del servidor esté firmado por la CA indicada abajo (obligatoria en este modo)." },
  { value: "verify-full", label: "verify-full — cifrado + CA + nombre del host (recomendado)", help: "Además comprueba que el certificado corresponda al host. Recomendado si el tráfico sale de la red local. La CA es opcional: sin ella el agente usa las CAs de confianza de su sistema." },
];

export function sslLabel(mode: SslMode | string | null | undefined): string {
  return mode ? String(mode) : "prefer";
}

// ─────────────────────────────────────────────────────────────────────────────
// Campos de conexión al DWH (grupo o destino propio de la empresa)
// ─────────────────────────────────────────────────────────────────────────────
export interface WarehouseForm {
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
  clear_password: boolean;
  schema: string;
  sslmode: SslMode;
  sslrootcert: string;
}

export const EMPTY_WAREHOUSE: WarehouseForm = {
  host: "",
  port: "5432",
  database: "",
  username: "",
  password: "",
  clear_password: false,
  schema: "public",
  sslmode: "prefer",
  sslrootcert: "",
};

const SCHEMA_RE = /^[a-z_][a-z0-9_]{0,62}$/;

/** Mensaje de error de validación local (null = válido). */
export function validateWarehouse(w: WarehouseForm, opts: { requireConnection?: boolean } = {}): string | null {
  const port = Number(w.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "El puerto del DWH debe estar entre 1 y 65535.";
  if (!SCHEMA_RE.test(w.schema.trim()) || w.schema.trim().startsWith("pg_"))
    return "Esquema destino no válido: minúsculas, números y _ (no puede empezar con número ni con pg_).";
  if (w.sslmode === "verify-ca" && !w.sslrootcert.trim()) return "verify-ca requiere el certificado de la CA (PEM). Con verify-full puede omitirse.";
  if (w.sslrootcert.trim() && !w.sslrootcert.includes("-----BEGIN CERTIFICATE-----"))
    return "El certificado de la CA debe estar en formato PEM (-----BEGIN CERTIFICATE-----).";
  if (opts.requireConnection && (!w.host.trim() || !w.database.trim() || !w.username.trim()))
    return "El destino propio necesita host, base de datos y usuario.";
  return null;
}

/** Cuerpo para la API con el prefijo warehouse_ (contraseña solo si se escribió). */
export function warehouseBody(w: WarehouseForm, opts: { isEdit: boolean; hasPassword: boolean; undecryptable?: string[]; clearKey?: string }) {
  const body: Record<string, unknown> = {
    warehouse_port: Number(w.port),
    warehouse_schema: w.schema.trim(),
    warehouse_sslmode: w.sslmode,
    warehouse_sslrootcert: w.sslrootcert.trim(),
  };
  (["host", "database", "username"] as const).forEach((k) => {
    // Si no se pudo descifrar y el campo quedó vacío, se conserva el valor actual.
    if ((opts.undecryptable ?? []).includes(`warehouse_${k}`) && !w[k]) return;
    body[`warehouse_${k}`] = w[k].trim();
  });
  if (w.password) body.warehouse_password = w.password;
  if (opts.isEdit && w.clear_password && !w.password && opts.hasPassword) body[opts.clearKey ?? "clear_password"] = true;
  return body;
}

export function WarehouseFields({
  idPrefix,
  value,
  onChange,
  disabled,
  isEdit,
  hasPassword,
  hasCa,
}: {
  idPrefix: string;
  value: WarehouseForm;
  onChange: (v: WarehouseForm) => void;
  disabled?: boolean;
  isEdit: boolean;
  hasPassword: boolean;
  hasCa?: boolean;
}) {
  const set = <K extends keyof WarehouseForm>(k: K, v: WarehouseForm[K]) => onChange({ ...value, [k]: v });
  const mode = SSL_MODES.find((m) => m.value === value.sslmode);
  const needsCa = value.sslmode === "verify-ca";
  return (
    <fieldset disabled={disabled} className="contents">
      <Field label="Host" className="sm:col-span-4" htmlFor={`${idPrefix}-host`}>
        <Input id={`${idPrefix}-host`} value={value.host} onChange={(e) => set("host", e.target.value)} placeholder="dwh.midominio.com" />
      </Field>
      <Field label="Puerto" className="sm:col-span-2" htmlFor={`${idPrefix}-port`}>
        <Input id={`${idPrefix}-port`} inputMode="numeric" value={value.port} onChange={(e) => set("port", e.target.value)} />
      </Field>
      <Field label="Base de datos" className="sm:col-span-3" htmlFor={`${idPrefix}-db`}>
        <Input id={`${idPrefix}-db`} value={value.database} onChange={(e) => set("database", e.target.value)} />
      </Field>
      <Field label="Usuario" className="sm:col-span-3" htmlFor={`${idPrefix}-user`}>
        <Input id={`${idPrefix}-user`} autoComplete="off" value={value.username} onChange={(e) => set("username", e.target.value)} />
      </Field>
      <Field
        label="Contraseña"
        className="sm:col-span-3"
        htmlFor={`${idPrefix}-pass`}
        hint={isEdit ? "Vacía = conservar la actual." : "Se guarda cifrada si el backend tiene clave Fernet."}
      >
        <PasswordInput id={`${idPrefix}-pass`} value={value.password} onChange={(v) => set("password", v)} hasPassword={hasPassword} isEdit={isEdit} />
      </Field>
      <Field
        label="Esquema destino"
        className="sm:col-span-3"
        htmlFor={`${idPrefix}-schema`}
        hint="Para tablas del catálogo sin esquema (p. ej. «clientes» → esquema.clientes). Un esquema explícito del catálogo (dwh.carter) se respeta."
      >
        <Input id={`${idPrefix}-schema`} value={value.schema} maxLength={63} onChange={(e) => set("schema", e.target.value.toLowerCase())} placeholder="public" />
      </Field>
      {isEdit && hasPassword && (
        <div className="sm:col-span-6">
          <Switch checked={value.clear_password} disabled={disabled} onChange={(v) => set("clear_password", v)} label="Borrar la contraseña guardada" />
        </div>
      )}
      <Field label="SSL/TLS (sslmode)" className="sm:col-span-6" htmlFor={`${idPrefix}-ssl`} hint={mode?.help}>
        <Select id={`${idPrefix}-ssl`} value={value.sslmode} onChange={(e) => set("sslmode", e.target.value as SslMode)}>
          {SSL_MODES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </Select>
      </Field>
      {(value.sslmode === "require" || value.sslmode === "verify-ca" || value.sslmode === "verify-full" || value.sslrootcert) && (
        <Field
          label={
            needsCa
              ? "Certificado de la CA (PEM) — obligatorio para verify-ca"
              : value.sslmode === "verify-full"
                ? "Certificado de la CA (PEM, opcional: sin él se usan las CAs del sistema del agente)"
                : "Certificado de la CA (PEM, opcional)"
          }
          className="sm:col-span-6"
          htmlFor={`${idPrefix}-ca`}
          hint={
            <>
              Certificado público de la autoridad que firmó el certificado del servidor (no es secreto). El agente lo guarda en su carpeta de datos.
              {hasCa && !value.sslrootcert && " Ya hay uno guardado; déjalo vacío para quitarlo."}
            </>
          }
        >
          <Textarea
            id={`${idPrefix}-ca`}
            mono
            rows={4}
            value={value.sslrootcert}
            onChange={(e) => set("sslrootcert", e.target.value)}
            placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
          />
        </Field>
      )}
    </fieldset>
  );
}

/** Ubicación física (host/puerto/base/esquema): si cambia, las tablas destino son otras. */
export function warehouseLocation(w: WarehouseForm): string {
  return [w.host.trim().toLowerCase(), String(Number(w.port) || 5432), w.database.trim(), w.schema.trim() || "public"].join("|");
}

/** Aviso antes de guardar un cambio de destino: reinicio de la carga de los extractores afectados. */
export function DestinationChangeNotice({
  tasks,
  reset,
  onReset,
  disabled,
}: {
  tasks: number;
  reset: boolean;
  onReset: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="status" data-testid="destination-change-notice">
      <p className="font-medium">
        Cambia el destino: {tasks > 0 ? `al guardar se reiniciará la carga de ${tasks} extractor(es)` : "no hay extractores afectados"}.
      </p>
      <p className="mt-0.5">
        Las tablas del destino nuevo empiezan vacías: con el reinicio la próxima ejecución hace una carga completa. La tarea que esté corriendo termina en el destino anterior; los agentes toman el cambio en su siguiente refresco de configuración.
      </p>
      {tasks > 0 && (
        <div className="mt-2">
          <Switch checked={reset} disabled={disabled} onChange={onReset} label="Reiniciar la carga (recomendado)" />
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Resumen del destino efectivo
// ─────────────────────────────────────────────────────────────────────────────
export function DestinationBadge({ source }: { source: "group" | "company" }) {
  return source === "company" ? <Badge tone="blue">Destino propio</Badge> : <Badge tone="slate">Destino del grupo</Badge>;
}

export function EffectiveDestination({ eff, compact }: { eff: EffectiveWarehouse; compact?: boolean }) {
  return (
    <div className={cx("text-xs", compact ? "" : "space-y-0.5")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <DestinationBadge source={eff.source} />
        <span className="font-mono text-slate-700" title="Esquema destino">{eff.schema}</span>
        <span className="text-slate-400" title="SSL/TLS">· ssl {sslLabel(eff.sslmode)}</span>
      </div>
      {eff.host !== null ? (
        <p className="font-mono text-slate-500">
          {eff.host || "—"}:{eff.port}/{eff.database || "—"}
        </p>
      ) : (
        <p className="italic text-slate-400" title="Requiere el permiso «Administrar credenciales» sobre el grupo">
          {eff.configured ? "Conexión oculta (credenciales)" : "Sin configurar"}
        </p>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Probar conexión (la ejecuta un agente en línea; Nexus nunca se conecta)
// ─────────────────────────────────────────────────────────────────────────────
const TERMINAL = new Set(["ok", "failed", "expired", "no_agent"]);

const STATUS_UI: Record<string, { label: string; tone: "green" | "red" | "amber" | "slate" | "blue" }> = {
  pending: { label: "Esperando a un agente…", tone: "blue" },
  running: { label: "El agente está probando…", tone: "blue" },
  ok: { label: "Conexión correcta", tone: "green" },
  failed: { label: "Falló", tone: "red" },
  expired: { label: "Sin respuesta", tone: "amber" },
  no_agent: { label: "Sin agente en línea", tone: "amber" },
};

const CHECK_LABELS: Record<string, string> = {
  CONNECT: "Conexión",
  SSL: "SSL/TLS",
  SCHEMA_EXISTS: "Esquema",
  SCHEMA_USAGE: "Uso del esquema",
  CREATE_TABLE: "Crear tablas",
  CREATE_SCHEMA: "Crear esquema",
  QUERY: "Consulta",
  UNEXPECTED: "Error",
};

function CheckIcon({ ok, severity }: { ok: boolean | null; severity: string }) {
  if (ok === true) return <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />;
  if (ok === false && severity === "error") return <XCircle className="h-4 w-4 shrink-0 text-red-600" />;
  if (severity === "warning" || ok === false) return <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />;
  return <Info className="h-4 w-4 shrink-0 text-slate-400" />;
}

export function ConnectionTestPanel({
  targetKind,
  groupId,
  companyId,
  canRun,
  disabledReason,
  title = "Probar conexión",
}: {
  targetKind: ConnectionTestKind;
  groupId?: number;
  companyId?: number;
  canRun: boolean;
  /** Motivo por el que no se puede probar (p. ej. cambios sin guardar). */
  disabledReason?: string | null;
  title?: string;
}) {
  const toast = useToast();
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [starting, setStarting] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const alive = useRef(true);

  const poll = useCallback(async (id: string, deadline: number) => {
    try {
      const t = await api<ConnectionTest>(`admin/connection-tests/${id}`);
      if (!alive.current) return;
      setTest(t);
      if (!TERMINAL.has(t.status) && Date.now() < deadline) {
        timer.current = setTimeout(() => void poll(id, deadline), 2000);
      }
    } catch {
      if (alive.current && Date.now() < deadline) timer.current = setTimeout(() => void poll(id, deadline), 4000);
    }
  }, []);

  // Última prueba de este destino/origen (para mostrar el resultado anterior).
  useEffect(() => {
    alive.current = true;
    const params = qs({ target_kind: targetKind, group_id: groupId, company_id: companyId, limit: 1 });
    api<{ items: ConnectionTest[] }>(`admin/connection-tests${params}`)
      .then((r) => {
        if (!alive.current || !r.items[0]) return;
        setTest(r.items[0]);
        if (!TERMINAL.has(r.items[0].status)) void poll(r.items[0].id, Date.now() + 5 * 60_000);
      })
      .catch(() => undefined);
    return () => {
      alive.current = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [targetKind, groupId, companyId, poll]);

  async function start() {
    setStarting(true);
    try {
      const body: Record<string, unknown> = { target_kind: targetKind };
      if (targetKind === "group_dwh") body.group_id = groupId;
      else body.company_id = companyId;
      const t = await api<ConnectionTest>("admin/connection-tests", { method: "POST", body });
      setTest(t);
      if (timer.current) clearTimeout(timer.current);
      if (!TERMINAL.has(t.status)) void poll(t.id, Date.now() + 5 * 60_000);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  const busy = test ? !TERMINAL.has(test.status) : false;
  const ui = test ? STATUS_UI[test.status] : null;
  const blocked = !canRun || Boolean(disabledReason);

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50/60 p-3" data-testid={`conn-test-${targetKind}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <PlugZap className="h-4 w-4 text-slate-500" />
          <span className="text-sm font-medium text-slate-700">{title}</span>
          {ui && (
            <Badge tone={ui.tone}>
              {busy && <Spinner className="h-3 w-3 text-current" />}
              {ui.label}
            </Badge>
          )}
        </div>
        <Button type="button" size="sm" variant="secondary" onClick={() => void start()} loading={starting} disabled={blocked || busy}
          title={!canRun ? "Requiere el permiso «Administrar configuración» sobre el grupo" : disabledReason ?? undefined}
          icon={<CircleDashed className="h-3.5 w-3.5" />}>
          {test ? "Probar de nuevo" : "Probar conexión"}
        </Button>
      </div>
      <p className="mt-1 text-xs text-slate-500">
        La prueba la ejecuta un agente en línea (versión 5.3 o superior) con alcance sobre esta conexión: Nexus nunca se conecta a tus servidores. Usa la configuración <b>guardada</b> y no crea nada (solo lectura).
      </p>
      {disabledReason && <p className="mt-1 text-xs text-amber-700">{disabledReason}</p>}
      {test && (
        <div className="mt-2 space-y-1.5 text-xs">
          <p className="text-slate-500">
            {fmtAgo(test.finished_at ?? test.created_at)} · pedida por {test.requested_by || "—"}
            {test.installation_name && <> · agente «{test.installation_name}»</>}
            {test.result?.duration_ms != null && <> · {fmtDuration(test.result.duration_ms)}</>}
            {test.status === "pending" && test.eligible_installations > 0 && <> · {test.eligible_installations} agente(s) en línea pueden tomarla</>}
          </p>
          {test.config_changed && TERMINAL.has(test.status) && (
            <p className="text-amber-700">La configuración cambió después de esta prueba: vuelve a probar.</p>
          )}
          {test.message && (test.status !== "ok") && (
            <p className={cx(test.status === "failed" ? "text-red-700" : "text-amber-800")}>
              {test.error_code && <span className="mr-1 font-mono">{test.error_code}</span>}
              {test.message}
            </p>
          )}
          {test.result && (
            <>
              {(test.result.server_version || test.result.ssl_in_use !== null) && (
                <p className="text-slate-600">
                  {test.result.server_version}
                  {test.result.ssl_in_use === true && <> · cifrada ({test.result.ssl_version || "TLS"})</>}
                  {test.result.ssl_in_use === false && <> · sin cifrar</>}
                </p>
              )}
              <ul className="space-y-1">
                {test.result.checks.map((c, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <CheckIcon ok={c.ok} severity={c.severity} />
                    <span>
                      <span className="font-medium text-slate-700">{CHECK_LABELS[c.code] ?? c.code}:</span>{" "}
                      <span className="text-slate-600">{c.message}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
