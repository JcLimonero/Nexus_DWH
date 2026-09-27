"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Search } from "lucide-react";
import { api, useApi } from "@/lib/api";
import type { Agency, CloneResponse, CloneResult, ListResponse, Task } from "@/lib/types";
import { cx } from "@/lib/format";
import { Badge, Button, Input, Select, Switch } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";

/**
 * Clonar extractores a otras agencias (POST /admin/tasks/{id}/clone y
 * POST /admin/agencies/{id}/clone-tasks). El servidor de origen sale de la empresa de la
 * agencia destino: el mismo extractor se ejecuta contra el origen de cada agencia.
 * Flujo: elegir destinos y opciones → vista previa (dry_run) → confirmar → resultados.
 */

export type CloneSource =
  | { kind: "task"; taskId: number; objectName: string; agencyId: number; agencyName: string }
  | { kind: "agency"; agencyId: number; agencyName: string; tasks: { id: number; object_name: string; destination_table: string }[] };

type Tone = "green" | "red" | "amber" | "slate" | "blue";

const STATUS: Record<CloneResult["status"], { preview: string; done: string; tone: Tone }> = {
  created: { preview: "Se creará", done: "Creado", tone: "green" },
  updated: { preview: "Se actualizará", done: "Actualizado", tone: "blue" },
  skipped_exists: { preview: "Se omitirá (ya existe)", done: "Omitido (ya existía)", tone: "slate" },
  object_conflict: { preview: "Conflicto de objeto", done: "Conflicto de objeto", tone: "amber" },
  error: { preview: "Error", done: "Error", tone: "red" },
};

const OBJECT_ACTION: Record<string, string> = {
  reused: "objeto reutilizado",
  created: "objeto copiado a la empresa",
  updated: "definición del objeto sobrescrita",
  conflict: "objeto con otra definición",
  missing: "objeto faltante",
};

const WARNING: Record<string, string> = {
  static_columns_review: "Revisa las columnas estáticas (p. ej. dn): se copiaron de la empresa de origen.",
};

export function CloneTasksModal({
  open,
  onClose,
  source,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  source: CloneSource | null;
  /** Se llama tras clonar (para recargar la página). */
  onDone?: () => void;
}) {
  const { can } = useSession();
  const toast = useToast();
  const agenciesApi = useApi<ListResponse<Agency>>(open ? "admin/agencies" : null);
  // Para marcar qué agencias ya tienen el extractor (mismo objeto por nombre).
  const tasksApi = useApi<ListResponse<Task>>(open ? "admin/tasks" : null);
  const [targets, setTargets] = useState<Set<number>>(new Set());
  const [taskIds, setTaskIds] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [copyObject, setCopyObject] = useState(true);
  const [disabled, setDisabled] = useState(true);
  const [onConflict, setOnConflict] = useState<"skip" | "update">("skip");
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<CloneResponse | null>(null);
  const [done, setDone] = useState<CloneResponse | null>(null);

  // Reinicio al abrir con otro origen.
  useEffect(() => {
    if (!open || !source) return;
    setTargets(new Set());
    setTaskIds(new Set(source.kind === "agency" ? source.tasks.map((t) => t.id) : []));
    setSearch("");
    setCopyObject(true);
    setDisabled(true);
    setOnConflict("skip");
    setOverwrite(false);
    setPreview(null);
    setDone(null);
  }, [open, source]);

  // Cualquier cambio de selección/opciones invalida la vista previa.
  useEffect(() => setPreview(null), [targets, taskIds, copyObject, disabled, onConflict, overwrite]);

  const sourceNames = useMemo(() => {
    if (!source) return new Set<string>();
    if (source.kind === "task") return new Set([source.objectName]);
    return new Set(source.tasks.filter((t) => taskIds.has(t.id)).map((t) => t.object_name));
  }, [source, taskIds]);

  // agencia → nombres de objetos que ya tiene como extractor
  const existing = useMemo(() => {
    const m = new Map<number, Set<string>>();
    (tasksApi.data?.items ?? []).forEach((t) => m.set(t.agency_id, (m.get(t.agency_id) ?? new Set()).add(t.object_name)));
    return m;
  }, [tasksApi.data]);

  const candidates = useMemo(
    () =>
      (agenciesApi.data?.items ?? []).filter((a) => source && a.id !== source.agencyId && can("config.manage", a.group_id)),
    [agenciesApi.data, source, can],
  );
  const needle = search.trim().toLowerCase();
  const visible = candidates.filter(
    (a) => !needle || [a.name, a.company_name, a.group_name].some((x) => x.toLowerCase().includes(needle)),
  );
  const grouped = useMemo(() => {
    const m = new Map<string, Agency[]>();
    visible.forEach((a) => {
      const k = `${a.group_name} / ${a.company_name}`;
      m.set(k, [...(m.get(k) ?? []), a]);
    });
    return Array.from(m.entries());
  }, [visible]);

  if (!source) return null;

  const toggleTarget = (id: number) =>
    setTargets((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const toggleTask = (id: number) =>
    setTaskIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const noTasks = source.kind === "agency" && taskIds.size === 0;
  const canRun = targets.size > 0 && !noTasks && !busy;

  async function submit(dryRun: boolean) {
    if (!source) return;
    const body: Record<string, unknown> = {
      target_agency_ids: Array.from(targets),
      copy_object_if_missing: copyObject,
      enabled: !disabled,
      on_conflict: onConflict,
      overwrite_objects: onConflict === "update" && overwrite,
      dry_run: dryRun,
    };
    let path = `admin/tasks/${source.kind === "task" ? source.taskId : 0}/clone`;
    if (source.kind === "agency") {
      path = `admin/agencies/${source.agencyId}/clone-tasks`;
      body.task_ids = Array.from(taskIds);
    }
    setBusy(true);
    try {
      const res = await api<CloneResponse>(path, { method: "POST", body });
      if (dryRun) setPreview(res);
      else {
        setDone(res);
        const s = res.summary;
        toast.success(`Clonado: ${s.created} creado(s), ${s.updated} actualizado(s), ${s.skipped_exists} omitido(s).`);
        onDone?.();
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const title =
    source.kind === "task" ? `Clonar extractor «${source.objectName}»` : `Clonar extractores de ${source.agencyName}`;
  const result = done ?? preview;

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="xl"
      title={title}
      description="El extractor usa el servidor de origen de la empresa de cada agencia destino; el SQL, la programación y los umbrales se copian."
      footer={
        done ? (
          <Button onClick={onClose}>Cerrar</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              Cancelar
            </Button>
            <Button variant="secondary" onClick={() => submit(true)} disabled={!canRun} loading={busy && !preview}>
              Vista previa
            </Button>
            <Button onClick={() => submit(false)} disabled={!canRun || !preview} loading={busy && Boolean(preview)} title={preview ? undefined : "Primero revisa la vista previa"}>
              Confirmar y clonar
            </Button>
          </>
        )
      }
    >
      {done ? (
        <CloneResults res={done} />
      ) : (
        <div className="grid gap-5 lg:grid-cols-5">
          <div className="space-y-4 lg:col-span-3">
            {source.kind === "agency" && (
              <fieldset>
                <legend className="mb-1.5 text-sm font-medium text-slate-700">Extractores a clonar</legend>
                <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
                  {source.tasks.map((t) => (
                    <label key={t.id} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-slate-50">
                      <input type="checkbox" aria-label={`Clonar ${t.object_name}`} className="h-4 w-4 rounded border-slate-300" checked={taskIds.has(t.id)} onChange={() => toggleTask(t.id)} />
                      <span className="min-w-0 truncate">
                        {t.object_name} <span className="font-mono text-[11px] text-slate-400">→ {t.destination_table}</span>
                      </span>
                    </label>
                  ))}
                </div>
                {noTasks && <p className="mt-1 text-xs text-red-600">Selecciona al menos un extractor.</p>}
              </fieldset>
            )}
            <fieldset>
              <legend className="mb-1.5 flex w-full flex-wrap items-center justify-between gap-2 text-sm font-medium text-slate-700">
                <span>
                  Agencias destino <span className="font-normal text-slate-500">({targets.size} seleccionada(s))</span>
                </span>
                <span className="flex gap-2 text-xs font-normal">
                  <button type="button" className="text-brand-600 hover:underline" onClick={() => setTargets(new Set([...targets, ...visible.map((a) => a.id)]))}>
                    Seleccionar visibles
                  </button>
                  <button type="button" className="text-slate-500 hover:underline" onClick={() => setTargets(new Set())}>
                    Ninguna
                  </button>
                </span>
              </legend>
              <div className="relative mb-2">
                <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-400" aria-hidden />
                <Input type="search" aria-label="Buscar agencia destino" placeholder="Buscar agencia, empresa o grupo…" className="pl-8" value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
              <div className="max-h-72 overflow-y-auto rounded-md border border-slate-200">
                {agenciesApi.loading && !agenciesApi.data ? (
                  <p className="p-3 text-sm text-slate-500">Cargando agencias…</p>
                ) : grouped.length === 0 ? (
                  <p className="p-3 text-sm text-slate-500">
                    {candidates.length === 0 ? "No hay otras agencias donde pueda administrar la configuración." : "Ninguna agencia coincide con la búsqueda."}
                  </p>
                ) : (
                  grouped.map(([k, list]) => (
                    <div key={k}>
                      <p className="sticky top-0 bg-slate-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{k}</p>
                      {list.map((a) => {
                        const has = [...sourceNames].filter((n) => existing.get(a.id)?.has(n));
                        return (
                          <label key={a.id} className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-slate-50">
                            <input type="checkbox" aria-label={`Destino ${a.name}`} className="h-4 w-4 rounded border-slate-300" checked={targets.has(a.id)} onChange={() => toggleTarget(a.id)} />
                            <span className="min-w-0 flex-1 truncate">{a.name}</span>
                            {!a.is_enabled && <Badge>Deshabilitada</Badge>}
                            {has.length > 0 && (
                              <Badge tone="amber">{source.kind === "task" ? "Ya lo tiene" : `Ya tiene ${has.length}`}</Badge>
                            )}
                          </label>
                        );
                      })}
                    </div>
                  ))
                )}
              </div>
            </fieldset>
          </div>

          <div className="space-y-4 lg:col-span-2">
            <fieldset className="space-y-3">
              <legend className="mb-1 text-sm font-medium text-slate-700">Opciones</legend>
              <Switch checked={disabled} onChange={setDisabled} label="Crear deshabilitados" description="Recomendado: revisa y activa cada extractor clonado." />
              <Switch
                checked={copyObject}
                onChange={setCopyObject}
                label="Copiar el objeto si la empresa no lo tiene"
                description="El catálogo es por empresa; si otra empresa no tiene el objeto (mismo nombre) se crea una copia."
              />
              <div>
                <label htmlFor="clone-conflict" className="mb-1 block text-sm font-medium text-slate-700">
                  Si la agencia ya tiene el extractor
                </label>
                <Select id="clone-conflict" value={onConflict} onChange={(e) => setOnConflict(e.target.value as "skip" | "update")}>
                  <option value="skip">Omitirlo</option>
                  <option value="update">Actualizar SQL, programación y umbrales</option>
                </Select>
              </div>
              {onConflict === "update" && (
                <Switch
                  checked={overwrite}
                  onChange={setOverwrite}
                  label="Sobrescribir objetos con otra definición"
                  description="Si la empresa destino tiene el objeto con otra tabla destino, llaves o columnas estáticas, se reemplaza su definición (afecta a todas sus agencias)."
                />
              )}
            </fieldset>
            {preview && (
              <div>
                <p className="mb-1.5 text-sm font-medium text-slate-700">Vista previa</p>
                <CloneResults res={preview} compact />
              </div>
            )}
            {!preview && targets.size > 0 && <p className="text-xs text-slate-500">Revisa la vista previa antes de confirmar.</p>}
          </div>
        </div>
      )}
    </Modal>
  );
}

function CloneSummary({ res }: { res: CloneResponse }) {
  const s = res.summary;
  const pre = res.dry_run;
  return (
    <div className="flex flex-wrap gap-1.5">
      <Badge tone="green">
        {s.created} {pre ? "por crear" : "creado(s)"}
      </Badge>
      {s.updated > 0 && <Badge tone="blue">{s.updated} {pre ? "por actualizar" : "actualizado(s)"}</Badge>}
      {s.skipped_exists > 0 && <Badge>{s.skipped_exists} {pre ? "se omitirán" : "omitido(s)"}</Badge>}
      {s.object_conflict > 0 && <Badge tone="amber">{s.object_conflict} conflicto(s)</Badge>}
      {s.error > 0 && <Badge tone="red">{s.error} error(es)</Badge>}
      {s.objects_created > 0 && <Badge tone="slate">{s.objects_created} objeto(s) {pre ? "por copiar" : "copiado(s)"}</Badge>}
    </div>
  );
}

function CloneResults({ res, compact = false }: { res: CloneResponse; compact?: boolean }) {
  const pre = res.dry_run;
  return (
    <div className="space-y-2">
      <CloneSummary res={res} />
      {!pre && (res.summary.created > 0 || res.summary.objects_created > 0) && (
        <p className="flex items-start gap-1.5 text-xs text-slate-600">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          Revisa los extractores creados (y las columnas estáticas de los objetos copiados) antes de activarlos.
        </p>
      )}
      <div className={cx("rounded-md border border-slate-200", compact && "max-h-80 overflow-y-auto")}>
        <Table>
          <thead>
            <tr>
              <Th>Destino</Th>
              {!compact && <Th>Extractor</Th>}
              <Th>Resultado</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {res.results.map((r, i) => {
              const st = STATUS[r.status];
              return (
                <Tr key={`${r.source_task_id}-${r.agency_id}-${i}`}>
                  <Td className="py-2">
                    <p className="font-medium text-slate-900">{r.agency_name ?? `Agencia #${r.agency_id}`}</p>
                    {r.company_name && <p className="text-[11px] text-slate-500">{[r.group_name, r.company_name].filter(Boolean).join(" / ")}</p>}
                    {compact && <p className="text-[11px] text-slate-500">{r.object_name}</p>}
                  </Td>
                  {!compact && <Td className="py-2 text-xs">{r.object_name}{r.task_id ? <span className="text-slate-400"> · #{r.task_id}</span> : null}</Td>}
                  <Td className="py-2">
                    <Badge tone={st.tone}>{pre ? st.preview : st.done}</Badge>
                    {r.object_action && r.status !== "error" && <p className="mt-0.5 text-[11px] text-slate-500">{OBJECT_ACTION[r.object_action]}</p>}
                    {r.message && r.status !== "created" && <p className="mt-0.5 max-w-xs text-[11px] text-slate-500">{r.message}</p>}
                    {r.warnings
                      .filter((w) => WARNING[w])
                      .map((w) => (
                        <p key={w} className="mt-0.5 max-w-xs text-[11px] text-amber-700">
                          {WARNING[w]}
                        </p>
                      ))}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      </div>
    </div>
  );
}
