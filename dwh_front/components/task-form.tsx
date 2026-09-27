"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useApi } from "@/lib/api";
import type { Agency, CatalogObject, ListResponse, Task } from "@/lib/types";
import { fmtDate, fmtSeconds } from "@/lib/format";
import { Button, Field, Input, Select, Switch, Textarea } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";

/**
 * Alta/edición de una tarea (agency_task). La usan Tareas, el detalle de grupo y el de agencia;
 * en las vistas nuevas se llama "extractor" (así la conoce el usuario).
 */

interface FormState {
  agency_id: string;
  object_catalog_id: string;
  extract_sql: string;
  schedule_seconds: string;
  is_active: boolean;
  run_on_company_token: boolean;
  expected_duration_seconds: string;
  delay_tolerance_seconds: string;
}

const EMPTY: FormState = {
  agency_id: "",
  object_catalog_id: "",
  extract_sql: "",
  schedule_seconds: "3600",
  is_active: true,
  run_on_company_token: true,
  expected_duration_seconds: "",
  delay_tolerance_seconds: "",
};

/** "" → null (valor automático); si no, entero ≥ min o NaN. */
function optInt(v: string, min: number): number | null {
  if (!v.trim()) return null;
  const n = Number(v);
  return Number.isInteger(n) && n >= min ? n : NaN;
}

const PRESETS = [
  { label: "5 min", value: 300 },
  { label: "15 min", value: 900 },
  { label: "1 h", value: 3600 },
  { label: "6 h", value: 21600 },
  { label: "24 h", value: 86400 },
];

/** Textos según cómo se llame la entidad en la página que abre el formulario. */
const NOUNS = {
  tarea: { create: "Nueva tarea", edit: "Editar tarea", submit: "Crear tarea", created: "Tarea creada.", updated: "Tarea actualizada.", active: "Tarea activa" },
  extractor: { create: "Nuevo extractor", edit: "Editar extractor", submit: "Crear extractor", created: "Extractor creado.", updated: "Extractor actualizado.", active: "Extractor activo" },
} as const;

export type TaskNoun = keyof typeof NOUNS;

function AgencyOptions({ agencies }: { agencies: Agency[] }) {
  const groups = new Map<string, Agency[]>();
  agencies.forEach((a) => {
    const k = `${a.group_name} / ${a.company_name}`;
    groups.set(k, [...(groups.get(k) ?? []), a]);
  });
  return (
    <>
      {Array.from(groups.entries()).map(([k, list]) => (
        <optgroup key={k} label={k}>
          {list.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
}

export function TaskFormModal({
  open,
  onClose,
  task,
  defaultAgencyId = "",
  onSaved,
  noun = "tarea",
  groupId,
}: {
  open: boolean;
  onClose: () => void;
  /** Tarea a editar (o ver en solo lectura sin config.manage); null = alta. */
  task: Task | null;
  /** Agencia preseleccionada en el alta. */
  defaultAgencyId?: string;
  onSaved?: () => void;
  noun?: TaskNoun;
  /** Limita las agencias del alta a un grupo (alta desde el detalle de grupo). */
  groupId?: number;
}) {
  const t = NOUNS[noun];
  const { can } = useSession();
  const toast = useToast();
  const { run } = useActions(onSaved);
  // Catálogos solo mientras el formulario está abierto (no cuestan nada a la página que lo contiene).
  const agencyList = useApi<ListResponse<Agency>>(open ? "admin/agencies" : null);
  const objects = useApi<ListResponse<CatalogObject>>(open ? "admin/objects" : null);
  const allAgencies = useMemo(() => agencyList.data?.items ?? [], [agencyList.data]);
  const agencies = allAgencies.filter((a) => can("config.manage", a.group_id) && (groupId === undefined || a.group_id === groupId));
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const readOnly = Boolean(task && !can("config.manage", task.group_id));

  useEffect(() => {
    if (!open) return;
    if (task) {
      setForm({
        agency_id: String(task.agency_id),
        object_catalog_id: String(task.object_catalog_id),
        extract_sql: task.extract_sql,
        schedule_seconds: String(task.schedule_seconds),
        is_active: task.is_active,
        run_on_company_token: task.run_on_company_token,
        expected_duration_seconds: task.expected_duration_seconds != null ? String(task.expected_duration_seconds) : "",
        delay_tolerance_seconds: task.delay_tolerance_seconds != null ? String(task.delay_tolerance_seconds) : "",
      });
    } else {
      setForm({ ...EMPTY, agency_id: defaultAgencyId });
    }
  }, [open, task, defaultAgencyId]);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  const selectedAgency = allAgencies.find((a) => String(a.id) === form.agency_id);
  const objectOptions = useMemo(
    () => (objects.data?.items ?? []).filter((o) => selectedAgency && o.company_id === selectedAgency.company_id),
    [objects.data, selectedAgency],
  );

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const sched = Number(form.schedule_seconds);
    if (!form.agency_id || !form.object_catalog_id) return toast.error("Selecciona agencia y objeto.");
    if (!Number.isInteger(sched) || sched < 10) return toast.error("La programación debe ser un entero ≥ 10 segundos.");
    if (!form.extract_sql.trim()) return toast.error("El SQL de extracción es obligatorio.");
    const expected = optInt(form.expected_duration_seconds, 1);
    const tolerance = optInt(form.delay_tolerance_seconds, 0);
    if (Number.isNaN(expected)) return toast.error("La duración esperada debe ser un entero ≥ 1 (o vacía = automática).");
    if (Number.isNaN(tolerance)) return toast.error("La tolerancia debe ser un entero ≥ 0 (o vacía = automática).");
    const body = {
      agency_id: Number(form.agency_id),
      object_catalog_id: Number(form.object_catalog_id),
      extract_sql: form.extract_sql,
      schedule_seconds: sched,
      is_active: form.is_active,
      run_on_company_token: form.run_on_company_token,
      expected_duration_seconds: expected,
      delay_tolerance_seconds: tolerance,
    };
    setSaving(true);
    const res = await run<Task>("save", task ? `admin/tasks/${task.id}` : "admin/tasks", {
      method: task ? "PUT" : "POST",
      body,
      success: task ? t.updated : t.created,
    });
    setSaving(false);
    if (res) onClose();
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="xl"
      title={task ? `${t.edit} #${task.id}` : t.create}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancelar
          </Button>
          {!readOnly && (
            <Button type="submit" form="task-form" loading={saving}>
              {task ? "Guardar cambios" : t.submit}
            </Button>
          )}
        </>
      }
    >
      <form id="task-form" onSubmit={onSubmit}>
        <fieldset disabled={readOnly} className="grid gap-4 sm:grid-cols-6">
          <Field label="Agencia" required className="sm:col-span-3" htmlFor="t-agency">
            <Select
              id="t-agency"
              required
              value={form.agency_id}
              onChange={(e) => {
                const a = agencies.find((x) => String(x.id) === e.target.value);
                setForm((f) => {
                  const keepObject = (objects.data?.items ?? []).some((o) => String(o.id) === f.object_catalog_id && a && o.company_id === a.company_id);
                  return { ...f, agency_id: e.target.value, object_catalog_id: keepObject ? f.object_catalog_id : "" };
                });
              }}
            >
              <option value="" disabled>
                {agencyList.loading && !agencyList.data ? "Cargando…" : "Selecciona…"}
              </option>
              <AgencyOptions agencies={readOnly && task ? allAgencies.filter((a) => a.id === task.agency_id) : agencies} />
            </Select>
          </Field>
          <Field
            label="Objeto del catálogo"
            required
            className="sm:col-span-3"
            htmlFor="t-object"
            hint={selectedAgency && objects.data && objectOptions.length === 0 ? "La empresa de esta agencia no tiene objetos en el catálogo." : "Solo objetos de la empresa de la agencia."}
          >
            <Select id="t-object" required value={form.object_catalog_id} onChange={(e) => set("object_catalog_id", e.target.value)} disabled={!selectedAgency}>
              <option value="" disabled>
                {selectedAgency ? "Selecciona…" : "Primero elige la agencia"}
              </option>
              {objectOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name} → {o.destination_table}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="SQL de extracción"
            required
            className="sm:col-span-6"
            htmlFor="t-sql"
            hint={
              <>
                Se ejecuta en la BD de origen. Usa <code className="font-mono">{"'{last_run}'"}</code> para cargas incrementales (el cliente lo sustituye por la
                fecha de la última ejecución, o 1900-01-01 si nunca se ha ejecutado).
              </>
            }
          >
            <Textarea id="t-sql" mono rows={12} required value={form.extract_sql} onChange={(e) => set("extract_sql", e.target.value)} placeholder="SELECT * FROM vista WHERE fecha >= '{last_run}'" />
          </Field>
          <Field label="Ejecutar cada (segundos)" className="sm:col-span-2" htmlFor="t-sched" hint={`= ${fmtSeconds(Number(form.schedule_seconds) || 0)}`}>
            <Input id="t-sched" inputMode="numeric" value={form.schedule_seconds} onChange={(e) => set("schedule_seconds", e.target.value)} />
          </Field>
          <div className="flex flex-wrap items-center gap-1.5 sm:col-span-4 sm:pt-6">
            {PRESETS.map((p) => (
              <Button key={p.value} type="button" size="sm" variant={Number(form.schedule_seconds) === p.value ? "primary" : "secondary"} onClick={() => set("schedule_seconds", String(p.value))}>
                {p.label}
              </Button>
            ))}
          </div>
          <Field
            label="Duración esperada (s)"
            className="sm:col-span-3"
            htmlFor="t-expected"
            hint="Vacío = automática (p90 de las últimas ejecuciones exitosas o el valor por defecto del servidor)."
          >
            <Input id="t-expected" inputMode="numeric" placeholder="automática" value={form.expected_duration_seconds} onChange={(e) => set("expected_duration_seconds", e.target.value)} />
          </Field>
          <Field
            label="Tolerancia de retraso (s)"
            className="sm:col-span-3"
            htmlFor="t-tolerance"
            hint="Margen extra antes de marcar la tarea como retrasada. Vacío = automática."
          >
            <Input id="t-tolerance" inputMode="numeric" placeholder="automática" value={form.delay_tolerance_seconds} onChange={(e) => set("delay_tolerance_seconds", e.target.value)} />
          </Field>
          <div className="grid gap-3 sm:col-span-6 sm:grid-cols-2">
            <Switch checked={form.is_active} disabled={readOnly} onChange={(v) => set("is_active", v)} label={t.active} />
            <Switch
              checked={form.run_on_company_token}
              disabled={readOnly}
              onChange={(v) => set("run_on_company_token", v)}
              label="Incluir en modo empresa (/configs)"
              description="Si se desactiva, solo se entrega a clientes en modo agencia o grupo."
            />
          </div>
          {task && (
            <p className="text-xs text-slate-500 sm:col-span-6">
              Última ejecución: {fmtDate(task.last_run_at)} · Actualizada: {fmtDate(task.updated_at)}
            </p>
          )}
        </fieldset>
      </form>
    </Modal>
  );
}
