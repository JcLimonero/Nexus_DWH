"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useApi } from "@/lib/api";
import type { Agency, CatalogObject, ListResponse, Task } from "@/lib/types";
import { fmtDate, fmtSeconds } from "@/lib/format";
import { Badge, Button, Field, Input, Select, Switch, Textarea } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { useActions } from "@/components/use-actions";
import { useConfirm, useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";
import {
  QueryPreviewBuilder,
  runCreateTableCommand,
  runUpsertCheckCommand,
  type AppliedDefinition,
  type CommandOut,
} from "@/components/query-preview";

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

/** Definición generada por "Crear un extractor desde el query", con la resolución de qué hacer
 * con el objeto del catálogo al guardar (crear uno nuevo, reemplazar el existente o reutilizarlo
 * tal cual). Se decide una vez, en handleApplyFromQuery, para no repreguntar en cada guardado. */
interface GeneratedDef extends AppliedDefinition {
  mode: "create" | "update" | "reuse";
  existingId: number | null;
  name: string;
}

interface PostSaveReport {
  createTable?: CommandOut;
  upsertCheck?: CommandOut;
}

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
  tarea: { create: "Nueva tarea", edit: "Editar tarea", view: "Ver tarea", submit: "Crear tarea", created: "Tarea creada.", updated: "Tarea actualizada.", active: "Tarea activa" },
  extractor: { create: "Nuevo extractor", edit: "Editar extractor", view: "Ver extractor", submit: "Crear extractor", created: "Extractor creado.", updated: "Extractor actualizado.", active: "Extractor activo" },
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

function commandBadge(label: string, c?: CommandOut) {
  if (!c) return null;
  if (c.status === "ok") return <Badge tone="green">{label}: OK</Badge>;
  if (c.status === "no_agent") return <Badge tone="amber">{label}: sin agente en línea (actualice a 5.4)</Badge>;
  return <Badge tone="red">{label}: {c.message || c.error_code || "error"}</Badge>;
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
  const confirm = useConfirm();
  const { run } = useActions(onSaved);
  // Catálogos solo mientras el formulario está abierto (no cuestan nada a la página que lo contiene).
  const agencyList = useApi<ListResponse<Agency>>(open ? "admin/agencies" : null);
  const objects = useApi<ListResponse<CatalogObject>>(open ? "admin/objects" : null);
  const allAgencies = useMemo(() => agencyList.data?.items ?? [], [agencyList.data]);
  const agencies = allAgencies.filter((a) => can("config.manage", a.group_id) && (groupId === undefined || a.group_id === groupId));
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [generatedDef, setGeneratedDef] = useState<GeneratedDef | null>(null);
  const [postSaveReport, setPostSaveReport] = useState<PostSaveReport | null>(null);
  const [postSaveRunning, setPostSaveRunning] = useState<"create_table" | "upsert_check" | null>(null);
  const [saved, setSaved] = useState(false);
  const readOnly = Boolean(task && !can("config.manage", task.group_id));

  useEffect(() => {
    if (!open) return;
    setGeneratedDef(null);
    setPostSaveReport(null);
    setPostSaveRunning(null);
    setSaved(false);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, task, defaultAgencyId]);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  const selectedAgency = allAgencies.find((a) => String(a.id) === form.agency_id);
  const objectOptions = useMemo(
    () => (objects.data?.items ?? []).filter((o) => selectedAgency && o.company_id === selectedAgency.company_id),
    [objects.data, selectedAgency],
  );
  const currentObject = task ? (objects.data?.items ?? []).find((o) => o.id === task.object_catalog_id) : null;

  /** El usuario pulsó "Usar esta definición" en el generador. Decide qué pasará con el objeto del
   * catálogo al guardar: crear uno nuevo, reemplazar uno existente (con confirmación si su
   * definición actual difiere) o reutilizarlo tal cual. */
  async function handleApplyFromQuery(def: AppliedDefinition) {
    if (!selectedAgency) return;
    const companyId = selectedAgency.company_id;
    const name = (def.destination_table.split(".").pop() || def.destination_table).trim();
    const existing = (objects.data?.items ?? []).find(
      (o) => o.company_id === companyId && o.destination_table.toLowerCase() === def.destination_table.toLowerCase(),
    );
    let mode: GeneratedDef["mode"] = "create";
    if (existing) {
      const differs =
        (existing.create_table_sql || "").trim() !== def.create_table_sql.trim() ||
        (existing.upsert_keys || "").trim() !== def.upsert_keys.trim() ||
        (existing.constraint_name || "").trim() !== def.constraint_name.trim();
      if (differs) {
        const overwrite = await confirm({
          title: "El objeto ya existe con otra definición",
          message: (
            <>
              Ya hay un objeto del catálogo <b>{existing.name}</b> (tabla <code className="font-mono">{existing.destination_table}</code>) con un{" "}
              <code className="font-mono">create_table_sql</code> o claves de upsert distintas a lo generado desde el query.
              {(existing.task_count ?? 0) > 0 && (
                <>
                  {" "}<b className="text-red-700">Lo usan {existing.task_count} extractor(es)</b>; reemplazar la definición les afecta a todos.
                </>
              )}{" "}
              ¿Reemplazar su definición?
            </>
          ),
          confirmLabel: "Reemplazar definición",
        });
        if (overwrite) {
          mode = "update";
        } else {
          const reuse = await confirm({
            title: "Usar el objeto existente sin cambios",
            message: "¿Usar el objeto existente tal como está? El extractor de todos modos guardará el query que acabas de probar.",
            confirmLabel: "Usar el existente",
          });
          if (!reuse) {
            toast.error("Se canceló: la definición generada no se aplicó.");
            return;
          }
          mode = "reuse";
        }
      } else {
        mode = "reuse";
      }
    }
    setGeneratedDef({ ...def, mode, existingId: existing?.id ?? null, name });
    set("extract_sql", def.extract_sql);
    set("object_catalog_id", existing ? String(existing.id) : "");
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const sched = Number(form.schedule_seconds);
    if (!form.agency_id) return toast.error("Selecciona la agencia.");
    if (!generatedDef && !form.object_catalog_id) return toast.error("Selecciona un objeto o crea el extractor desde el query.");
    if (!Number.isInteger(sched) || sched < 10) return toast.error("La programación debe ser un entero ≥ 10 segundos.");
    if (!form.extract_sql.trim()) return toast.error("El SQL de extracción es obligatorio.");
    const expected = optInt(form.expected_duration_seconds, 1);
    const tolerance = optInt(form.delay_tolerance_seconds, 0);
    if (Number.isNaN(expected)) return toast.error("La duración esperada debe ser un entero ≥ 1 (o vacía = automática).");
    if (Number.isNaN(tolerance)) return toast.error("La tolerancia debe ser un entero ≥ 0 (o vacía = automática).");

    setSaving(true);
    let objectId = form.object_catalog_id ? Number(form.object_catalog_id) : null;
    if (generatedDef && generatedDef.mode !== "reuse") {
      const objectBody = {
        company_id: selectedAgency!.company_id,
        destination_table: generatedDef.destination_table,
        create_table_sql: generatedDef.create_table_sql,
        upsert_keys: generatedDef.upsert_keys,
        constraint_name: generatedDef.constraint_name,
        create_constraint_sql: generatedDef.create_constraint_sql,
      };
      const savedObject =
        generatedDef.mode === "create"
          ? await run<CatalogObject>("save_object", "admin/objects", { method: "POST", body: { ...objectBody, name: generatedDef.name } })
          : await run<CatalogObject>("save_object", `admin/objects/${generatedDef.existingId}`, { method: "PUT", body: objectBody });
      if (!savedObject) {
        setSaving(false);
        return;
      }
      objectId = savedObject.id;
    } else if (generatedDef && generatedDef.mode === "reuse") {
      objectId = generatedDef.existingId ?? objectId;
    }
    if (!objectId) {
      setSaving(false);
      return toast.error("No se pudo determinar el objeto del catálogo.");
    }

    const body = {
      agency_id: Number(form.agency_id),
      object_catalog_id: objectId,
      extract_sql: form.extract_sql,
      schedule_seconds: sched,
      is_active: form.is_active,
      run_on_company_token: form.run_on_company_token,
      expected_duration_seconds: expected,
      delay_tolerance_seconds: tolerance,
    };
    const res = await run<Task>("save", task ? `admin/tasks/${task.id}` : "admin/tasks", {
      method: task ? "PUT" : "POST",
      body,
      success: task ? t.updated : t.created,
    });
    if (!res) {
      setSaving(false);
      return;
    }
    setSaved(true);
    // El requerimiento pide que guardar cree la tabla y valide el upsert: si el extractor se generó
    // desde el query, se piden esos dos comandos automáticamente y su resultado se muestra aquí
    // mismo (el diálogo se queda abierto para que el usuario lo vea antes de cerrar).
    if (generatedDef && selectedAgency) {
      setPostSaveRunning("create_table");
      try {
        const createRes = await runCreateTableCommand(selectedAgency.company_id, generatedDef);
        setPostSaveReport((r) => ({ ...r, createTable: createRes }));
      } catch (err) {
        toast.error((err as Error).message);
      }
      if (generatedDef.key_columns.length > 0) {
        setPostSaveRunning("upsert_check");
        try {
          const upsertRes = await runUpsertCheckCommand(selectedAgency.company_id, generatedDef);
          setPostSaveReport((r) => ({ ...r, upsertCheck: upsertRes }));
        } catch (err) {
          toast.error((err as Error).message);
        }
      }
      setPostSaveRunning(null);
      setSaving(false);
      return; // el usuario cierra manualmente tras revisar el resultado
    }
    setSaving(false);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="xl"
      title={task ? `${readOnly ? t.view : t.edit} #${task.id}` : t.create}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {saved ? "Cerrar" : "Cancelar"}
          </Button>
          {!readOnly && !saved && (
            <Button type="submit" form="task-form" loading={saving}>
              {task ? "Guardar cambios" : t.submit}
            </Button>
          )}
        </>
      }
    >
      <form id="task-form" onSubmit={onSubmit}>
        <fieldset disabled={readOnly || saved} className="grid gap-4 sm:grid-cols-6">
          <Field label="Agencia" required className="sm:col-span-3" htmlFor="t-agency">
            <Select
              id="t-agency"
              required
              value={form.agency_id}
              onChange={(e) => {
                const a = agencies.find((x) => String(x.id) === e.target.value);
                setGeneratedDef(null);
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
            required={!generatedDef}
            className="sm:col-span-3"
            htmlFor="t-object"
            hint={
              generatedDef
                ? generatedDef.mode === "create"
                  ? `Se creará al guardar: ${generatedDef.name} → ${generatedDef.destination_table}`
                  : generatedDef.mode === "update"
                    ? `Se reemplazará la definición de: ${generatedDef.name}`
                    : `Se reutilizará tal cual: ${generatedDef.name}`
                : selectedAgency && objects.data && objectOptions.length === 0
                  ? "La empresa de esta agencia no tiene objetos en el catálogo."
                  : "Solo objetos de la empresa de la agencia."
            }
          >
            <Select
              id="t-object"
              required={!generatedDef}
              value={form.object_catalog_id}
              onChange={(e) => {
                setGeneratedDef(null);
                set("object_catalog_id", e.target.value);
              }}
              disabled={!selectedAgency || Boolean(generatedDef)}
            >
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

          {selectedAgency && !generatedDef && (
            <QueryPreviewBuilder
              key={task?.id ?? "new"}
              companyId={selectedAgency.company_id}
              initialSql={task?.extract_sql ?? ""}
              initialTable={currentObject?.destination_table ?? ""}
              onApply={handleApplyFromQuery}
            />
          )}
          {generatedDef && (
            <div className="col-span-full flex flex-wrap items-center gap-3 rounded-lg border border-indigo-200 bg-indigo-50/60 p-3 text-sm">
              <span>
                Extractor generado desde el query: <code className="font-mono">{generatedDef.destination_table}</code> ({generatedDef.columns.length} columna(s),{" "}
                {generatedDef.key_columns.length} llave(s)).
              </span>
              <Button type="button" size="sm" variant="secondary" onClick={() => setGeneratedDef(null)}>
                Quitar definición generada
              </Button>
            </div>
          )}

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
            <Textarea
              id="t-sql"
              mono
              rows={8}
              required
              value={form.extract_sql}
              onChange={(e) => {
                setGeneratedDef(null);
                set("extract_sql", e.target.value);
              }}
              placeholder="SELECT * FROM vista WHERE fecha >= '{last_run}'"
            />
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
          {saved && (
            <div className="col-span-full rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm">
              <p className="mb-2 font-medium text-slate-800">Extractor guardado.</p>
              {generatedDef ? (
                <>
                  <p className="mb-2 text-xs text-slate-600">
                    Pedido automáticamente al agente: crear la tabla en el DWH y validar el upsert (con ROLLBACK; no se confirma nada).
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {postSaveRunning === "create_table" && <Badge tone="blue">Creando tabla…</Badge>}
                    {commandBadge("Crear tabla", postSaveReport?.createTable)}
                    {postSaveRunning === "upsert_check" && <Badge tone="blue">Validando upsert…</Badge>}
                    {commandBadge("Validar upsert", postSaveReport?.upsertCheck)}
                    {!postSaveRunning && generatedDef.key_columns.length === 0 && (
                      <Badge tone="slate">Sin llaves elegidas: no se validó el upsert.</Badge>
                    )}
                  </div>
                  {postSaveReport?.upsertCheck?.result?.upsert && Object.keys(postSaveReport.upsertCheck.result.upsert.column_errors).length > 0 && (
                    <ul className="mt-2 list-disc pl-5 text-xs text-red-700">
                      {Object.entries(postSaveReport.upsertCheck.result.upsert.column_errors).map(([k, v]) => (
                        <li key={k}>
                          {k}: {v}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              ) : (
                <p className="text-xs text-slate-600">Puedes cerrar esta ventana.</p>
              )}
            </div>
          )}
        </fieldset>
      </form>
    </Modal>
  );
}
