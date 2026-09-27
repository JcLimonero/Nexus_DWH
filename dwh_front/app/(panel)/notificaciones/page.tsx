"use client";

import { useState, type FormEvent } from "react";
import { Info, Pencil, Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Delivery, IncidentCategory, NotificationChannel, Severity } from "@/lib/types";
import { fmtDateTz, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/feedback";
import { useActions } from "@/components/use-actions";
import { useRefData } from "@/components/ref-data";
import { useSession } from "@/components/session";
import { CATEGORY_LABEL, DELIVERY_STATUS, SEVERITY, TRANSITION_LABEL } from "@/components/health";

interface FormState {
  name: string;
  kind: "webhook" | "log";
  url: string;
  signing_secret: string;
  clear_secret: boolean;
  is_enabled: boolean;
  min_severity: Severity;
  group_id: string;
  categories: IncidentCategory[];
  notify_on_open: boolean;
  notify_on_resolve: boolean;
  reminder_interval_minutes: string;
  timeout_seconds: string;
  verify_tls: boolean;
}

const EMPTY: FormState = {
  name: "",
  kind: "webhook",
  url: "",
  signing_secret: "",
  clear_secret: false,
  is_enabled: true,
  min_severity: "warning",
  group_id: "",
  categories: [],
  notify_on_open: true,
  notify_on_resolve: true,
  reminder_interval_minutes: "0",
  timeout_seconds: "10",
  verify_tls: true,
};

export default function NotificacionesPage() {
  const channels = useApi<{ items: NotificationChannel[]; allow_http: boolean }>("admin/notification-channels");
  const [chFilter, setChFilter] = useState("");
  const [stFilter, setStFilter] = useState("");
  const deliveries = useApi<{ items: Delivery[] }>(`admin/notification-deliveries${qs({ channel_id: chFilter, status: stFilter, limit: 200 })}`);
  const { groups: allGroups } = useRefData();
  const { can, canAny, canGlobal } = useSession();
  // Canal de un grupo: credentials.manage en ese grupo; canal "todos los grupos": alcance global.
  const groups = allGroups.filter((g) => can("credentials.manage", g.id));
  const canGlobalChannel = canGlobal("credentials.manage");
  const toast = useToast();
  const { run, busy } = useActions(() => {
    void channels.reload();
    void deliveries.reload();
  });
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<NotificationChannel | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));
  const items = channels.data?.items ?? [];

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY, group_id: canGlobalChannel ? "" : String(groups[0]?.id ?? "") });
    setOpen(true);
  }
  function openEdit(c: NotificationChannel) {
    setEditing(c);
    setForm({
      ...EMPTY,
      name: c.name,
      kind: c.kind,
      is_enabled: c.is_enabled,
      min_severity: c.min_severity,
      group_id: c.group_id ? String(c.group_id) : "",
      categories: c.categories,
      notify_on_open: c.notify_on_open,
      notify_on_resolve: c.notify_on_resolve,
      reminder_interval_minutes: String(c.reminder_interval_minutes),
      timeout_seconds: String(c.timeout_seconds),
      verify_tls: c.verify_tls,
    });
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const reminder = Number(form.reminder_interval_minutes);
    const timeout = Number(form.timeout_seconds);
    if (!form.name.trim()) return toast.error("El nombre es obligatorio.");
    if (!Number.isInteger(reminder) || reminder < 0) return toast.error("El recordatorio debe ser un entero ≥ 0 (0 = sin recordatorio).");
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 60) return toast.error("El timeout debe estar entre 1 y 60 s.");
    if (!editing && form.kind === "webhook" && !form.url.trim()) return toast.error("La URL del webhook es obligatoria.");
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      is_enabled: form.is_enabled,
      min_severity: form.min_severity,
      categories: form.categories,
      notify_on_open: form.notify_on_open,
      notify_on_resolve: form.notify_on_resolve,
      reminder_interval_minutes: reminder,
      timeout_seconds: timeout,
      verify_tls: form.verify_tls,
    };
    if (editing) {
      body.group_id = form.group_id ? Number(form.group_id) : 0;
      if (form.url.trim()) body.url = form.url.trim();
      if (form.clear_secret) body.signing_secret = "";
      else if (form.signing_secret) body.signing_secret = form.signing_secret;
    } else {
      body.kind = form.kind;
      body.group_id = form.group_id ? Number(form.group_id) : null;
      if (form.kind === "webhook") body.url = form.url.trim();
      if (form.signing_secret) body.signing_secret = form.signing_secret;
    }
    const res = await run<NotificationChannel>("save", editing ? `admin/notification-channels/${editing.id}` : "admin/notification-channels", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Canal actualizado." : "Canal creado.",
    });
    if (res) setOpen(false);
  }

  const toggleCategory = (c: IncidentCategory) =>
    set("categories", form.categories.includes(c) ? form.categories.filter((x) => x !== c) : [...form.categories, c]);

  return (
    <>
      <PageHeader
        title="Notificaciones"
        description={`Canales externos para avisar de incidencias. Horas en ${tzLabel()}.`}
        actions={
          <>
            <Button
              variant="secondary"
              icon={<RefreshCw className="h-4 w-4" />}
              onClick={() => {
                void channels.reload();
                void deliveries.reload();
              }}
            >
              Actualizar
            </Button>
            {canAny("credentials.manage") && (
              <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate}>
                Nuevo canal
              </Button>
            )}
          </>
        }
      />

      <p className="mb-4 flex items-start gap-2 text-sm text-slate-500">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <span>
          Se notifica solo en las <b>transiciones</b> (apertura y resolución; recordatorio opcional mientras siga abierta y sin reconocer), nunca
          en cada ciclo. Las alertas del panel (menú Incidencias) funcionan siempre, haya o no canales. Los webhooks van firmados con
          HMAC-SHA256 (cabecera <code>X-Nexus-Signature</code>) y con <code>X-Nexus-Delivery</code> para descartar repetidos. La URL y el
          secreto son de solo escritura.
        </span>
      </p>

      <Card>
        <DataState
          loading={channels.loading}
          error={channels.error}
          hasData={Boolean(channels.data)}
          empty={items.length === 0}
          onRetry={channels.reload}
          emptyTitle="Sin canales"
          emptyDescription="No hay canales externos configurados: las incidencias solo se ven en el panel."
        >
          <Table>
            <thead>
              <tr>
                <Th>Canal</Th>
                <Th>Destino</Th>
                <Th>Filtros</Th>
                <Th>Eventos</Th>
                <Th>Entregas</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((c) => (
                <Tr key={c.id} className={c.is_enabled ? undefined : "opacity-60"}>
                  <Td>
                    <p className="font-medium text-slate-900">{c.name}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <Badge tone="blue">{c.kind === "webhook" ? "Webhook" : "Log (desarrollo)"}</Badge>
                      {c.is_enabled ? <Badge tone="green">Activo</Badge> : <Badge tone="slate">Inactivo</Badge>}
                    </div>
                  </Td>
                  <Td className="text-xs">
                    {c.kind === "webhook" ? (
                      <>
                        <p className="font-mono">{c.url_display || "—"}</p>
                        <p className="text-slate-500">
                          {c.has_secret ? "Firma HMAC configurada" : "Sin firma"}
                          {!c.encrypted && <span className="text-amber-700"> · sin cifrar (falta config_secret_key)</span>}
                          {!c.verify_tls && <span className="text-amber-700"> · TLS sin verificar</span>}
                        </p>
                      </>
                    ) : (
                      <span className="text-slate-500">Log del servidor</span>
                    )}
                  </Td>
                  <Td className="text-xs text-slate-600">
                    <p>Severidad ≥ {SEVERITY[c.min_severity].label}</p>
                    <p>{c.group_name ? `Grupo: ${c.group_name}` : "Todos los grupos"}</p>
                    <p>{c.categories.length ? c.categories.map((k) => CATEGORY_LABEL[k]).join(", ") : "Todas las categorías"}</p>
                  </Td>
                  <Td className="text-xs text-slate-600">
                    <p>
                      {[c.notify_on_open && "apertura", c.notify_on_resolve && "resolución"].filter(Boolean).join(" y ") || "ninguno"}
                    </p>
                    <p>{c.reminder_interval_minutes ? `Recordatorio cada ${c.reminder_interval_minutes} min` : "Sin recordatorio"}</p>
                  </Td>
                  <Td className="text-xs text-slate-600">
                    <p>Última: {fmtDateTz(c.last_delivery_at)}</p>
                    {c.pending > 0 && <p className="text-amber-700">{c.pending} pendiente(s)</p>}
                    {c.failed > 0 && <p className="text-red-600">{c.failed} fallida(s)</p>}
                  </Td>
                  <Td className="text-right">
                    {!can("credentials.manage", c.group_id) ? (
                      <span className="text-xs text-slate-400">Solo lectura</span>
                    ) : (
                    <div className="flex justify-end gap-0.5">
                      <IconButton
                        label="Enviar prueba"
                        disabled={busy === `test-${c.id}`}
                        onClick={async () => {
                          const r = await run<{ status: string; error: string | null; http_status: number | null }>(`test-${c.id}`, `admin/notification-channels/${c.id}/test`);
                          if (r) {
                            if (r.status === "delivered") toast.success("Prueba entregada.");
                            else toast.error(`La prueba no se entregó: ${r.error ?? r.status}`);
                          }
                        }}
                      >
                        <Send className="h-4 w-4" />
                      </IconButton>
                      <IconButton label="Editar" onClick={() => openEdit(c)}>
                        <Pencil className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        disabled={busy === `del-${c.id}`}
                        onClick={() =>
                          run(`del-${c.id}`, `admin/notification-channels/${c.id}`, {
                            method: "DELETE",
                            success: "Canal eliminado.",
                            confirm: { title: "Eliminar canal", message: <>Se eliminará <b>{c.name}</b> y su historial de entregas.</>, confirmLabel: "Eliminar", danger: true },
                          })
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
                    </div>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <div className="mb-3 mt-8 flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold text-slate-900">Registro de entregas</h2>
        <Select aria-label="Canal" className="w-full sm:w-52" value={chFilter} onChange={(e) => setChFilter(e.target.value)}>
          <option value="">Todos los canales</option>
          {items.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </Select>
        <Select aria-label="Estado de la entrega" className="w-full sm:w-44" value={stFilter} onChange={(e) => setStFilter(e.target.value)}>
          <option value="">Todos los estados</option>
          {Object.entries(DELIVERY_STATUS).map(([k, v]) => (
            <option key={k} value={k}>
              {v.label}
            </option>
          ))}
        </Select>
        {deliveries.data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(deliveries.data.items.length)} entrega(s)</span>}
      </div>
      <Card>
        <DataState
          loading={deliveries.loading}
          error={deliveries.error}
          hasData={Boolean(deliveries.data)}
          empty={(deliveries.data?.items ?? []).length === 0}
          onRetry={deliveries.reload}
          emptyTitle="Sin entregas"
        >
          <Table>
            <thead>
              <tr>
                <Th>Fecha</Th>
                <Th>Canal</Th>
                <Th>Evento</Th>
                <Th>Incidencia</Th>
                <Th>Estado</Th>
                <Th className="text-right">Intentos</Th>
                <Th>Último error</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {(deliveries.data?.items ?? []).map((d) => (
                <Tr key={d.id}>
                  <Td className="whitespace-nowrap text-xs">{fmtDateTz(d.created_at)}</Td>
                  <Td className="text-xs">{d.channel_name ?? `#${d.channel_id}`}</Td>
                  <Td className="text-xs">{TRANSITION_LABEL[d.transition] ?? d.transition}</Td>
                  <Td className="text-xs">{d.incident_id ? `#${d.incident_id} ${d.incident_title ?? ""}` : "—"}</Td>
                  <Td>
                    <Badge tone={DELIVERY_STATUS[d.status]?.tone ?? "slate"}>{DELIVERY_STATUS[d.status]?.label ?? d.status}</Badge>
                    {d.status === "pending" && d.next_attempt_at && <p className="mt-1 text-[11px] text-slate-500">Reintento {fmtDateTz(d.next_attempt_at)}</p>}
                  </Td>
                  <Td className="text-right tabular-nums">{d.attempts}</Td>
                  <Td className="font-mono text-xs text-red-700">{d.last_error ?? ""}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={editing ? "Editar canal" : "Nuevo canal"}
        size="lg"
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="channel-form" loading={busy === "save"}>
              Guardar
            </Button>
          </>
        }
      >
        <form id="channel-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Nombre" required className="sm:col-span-3" htmlFor="c-name">
            <Input id="c-name" maxLength={100} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Tipo" className="sm:col-span-3" htmlFor="c-kind">
            <Select id="c-kind" value={form.kind} disabled={Boolean(editing)} onChange={(e) => set("kind", e.target.value as FormState["kind"])}>
              <option value="webhook">Webhook (HTTP POST firmado)</option>
              <option value="log">Log del servidor (desarrollo)</option>
            </Select>
          </Field>
          {form.kind === "webhook" && (
            <>
              <Field
                label="URL"
                required={!editing}
                className="sm:col-span-6"
                htmlFor="c-url"
                hint={
                  editing
                    ? `Actual: ${editing.url_display || "—"} (no se muestra completa). Deja vacío para conservarla.`
                    : channels.data?.allow_http
                      ? "https:// (http:// permitido en este servidor)."
                      : "Debe ser https://. Se guarda cifrada y no se vuelve a mostrar."
                }
              >
                <Input id="c-url" type="url" autoComplete="off" value={form.url} onChange={(e) => set("url", e.target.value)} placeholder="https://" />
              </Field>
              <Field
                label="Secreto de firma (HMAC)"
                className="sm:col-span-4"
                htmlFor="c-secret"
                hint={editing ? (editing.has_secret ? "Hay un secreto guardado. Deja vacío para conservarlo." : "Sin secreto: los envíos no se firman.") : "Opcional pero recomendado. Solo escritura."}
              >
                <Input id="c-secret" type="password" autoComplete="new-password" value={form.signing_secret} disabled={form.clear_secret} onChange={(e) => set("signing_secret", e.target.value)} />
              </Field>
              <Field label="Timeout (s)" className="sm:col-span-2" htmlFor="c-timeout">
                <Input id="c-timeout" inputMode="numeric" value={form.timeout_seconds} onChange={(e) => set("timeout_seconds", e.target.value)} />
              </Field>
              {editing?.has_secret && (
                <div className="sm:col-span-6">
                  <Switch checked={form.clear_secret} onChange={(v) => set("clear_secret", v)} label="Quitar el secreto de firma" />
                </div>
              )}
            </>
          )}
          <Field label="Severidad mínima" className="sm:col-span-3" htmlFor="c-sev">
            <Select id="c-sev" value={form.min_severity} onChange={(e) => set("min_severity", e.target.value as Severity)}>
              {(Object.keys(SEVERITY) as Severity[]).map((k) => (
                <option key={k} value={k}>
                  {SEVERITY[k].label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Grupo" className="sm:col-span-3" htmlFor="c-group">
            <Select id="c-group" value={form.group_id} onChange={(e) => set("group_id", e.target.value)}>
              <option value="" disabled={!canGlobalChannel}>
                Todos los grupos{canGlobalChannel ? "" : " (requiere alcance global)"}
              </option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </Select>
          </Field>
          <div className="sm:col-span-6">
            <p className="mb-1 text-sm font-medium text-slate-700">Categorías (ninguna marcada = todas)</p>
            <div className="flex flex-wrap gap-1.5">
              {(Object.keys(CATEGORY_LABEL) as IncidentCategory[]).map((k) => (
                <Button key={k} type="button" size="sm" variant={form.categories.includes(k) ? "primary" : "secondary"} onClick={() => toggleCategory(k)}>
                  {CATEGORY_LABEL[k]}
                </Button>
              ))}
            </div>
          </div>
          <Field label="Recordatorio (min)" className="sm:col-span-2" htmlFor="c-rem" hint="0 = sin recordatorio. Solo abiertas y sin reconocer.">
            <Input id="c-rem" inputMode="numeric" value={form.reminder_interval_minutes} onChange={(e) => set("reminder_interval_minutes", e.target.value)} />
          </Field>
          <div className="grid gap-3 sm:col-span-4 sm:grid-cols-2">
            <Switch checked={form.notify_on_open} onChange={(v) => set("notify_on_open", v)} label="Al abrirse" />
            <Switch checked={form.notify_on_resolve} onChange={(v) => set("notify_on_resolve", v)} label="Al resolverse" />
            <Switch checked={form.is_enabled} onChange={(v) => set("is_enabled", v)} label="Canal activo" />
            {form.kind === "webhook" && <Switch checked={form.verify_tls} onChange={(v) => set("verify_tls", v)} label="Verificar TLS" />}
          </div>
        </form>
      </Modal>
    </>
  );
}
