"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { FilterX } from "lucide-react";
import { useApi } from "@/lib/api";
import type { Agency, Company, Group, ListResponse, Task } from "@/lib/types";
import { Button, Input, Select } from "@/components/ui/primitives";

/**
 * Filtros comunes (grupo, empresa, agencia, base, tarea, estado, fechas) guardados en
 * la URL (?group_id=…): se pueden compartir/recargar. Las opciones salen de la API, que
 * ya limita los grupos al alcance del usuario.
 */
export type FilterKey = "group_id" | "company_id" | "agency_id" | "database_id" | "task_id" | "status" | "since" | "until";

export function useUrlFilters<K extends string>(keys: readonly K[], defaults: Partial<Record<K, string>> = {}) {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const values = useMemo(() => {
    const out = {} as Record<K, string>;
    keys.forEach((k) => (out[k] = sp.get(k) ?? defaults[k] ?? ""));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sp, keys.join("|")]);

  const set = useCallback(
    (patch: Partial<Record<K, string>>) => {
      const next = new URLSearchParams(sp.toString());
      Object.entries(patch).forEach(([k, v]) => {
        if (v === undefined || v === null || v === "" || v === (defaults as Record<string, string>)[k]) next.delete(k);
        else next.set(k, String(v));
      });
      const q = next.toString();
      router.replace(q ? `${pathname}?${q}` : pathname, { scroll: false });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sp, pathname, router],
  );

  const clear = useCallback(() => {
    const next = new URLSearchParams(sp.toString());
    keys.forEach((k) => next.delete(k));
    const q = next.toString();
    router.replace(q ? `${pathname}?${q}` : pathname, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sp, pathname, router, keys.join("|")]);

  return [values, set, clear] as const;
}

/** Fechas del filtro (AAAA-MM-DD, día local) → ISO para la API. */
export function dateRange(since?: string, until?: string): { since?: string; until?: string } {
  const out: { since?: string; until?: string } = {};
  if (since) out.since = new Date(`${since}T00:00:00`).toISOString();
  if (until) out.until = new Date(`${until}T23:59:59.999`).toISOString();
  return out;
}

export interface Option {
  value: string;
  label: string;
}

interface MonitoredDbLite {
  id: number;
  display_name: string;
  group_id: number | null;
  company_id: number | null;
  kind: string;
}

export function FilterBar({
  values,
  onChange,
  onClear,
  fields,
  statusOptions,
  statusLabel = "Estado",
  children,
}: {
  values: Partial<Record<FilterKey, string>>;
  onChange: (patch: Partial<Record<FilterKey, string>>) => void;
  onClear?: () => void;
  fields: FilterKey[];
  statusOptions?: Option[];
  statusLabel?: string;
  children?: ReactNode;
}) {
  const has = (k: FilterKey) => fields.includes(k);
  const groups = useApi<ListResponse<Group>>(has("group_id") ? "admin/groups" : null);
  const companies = useApi<ListResponse<Company>>(has("company_id") ? "admin/companies" : null);
  const agencies = useApi<ListResponse<Agency>>(has("agency_id") ? "admin/agencies" : null);
  const tasks = useApi<ListResponse<Task>>(has("task_id") ? "admin/tasks" : null);
  const dbs = useApi<ListResponse<MonitoredDbLite>>(has("database_id") ? "admin/monitored-databases" : null);
  const g = values.group_id ?? "";
  const c = values.company_id ?? "";
  const a = values.agency_id ?? "";

  const companyOpts = (companies.data?.items ?? []).filter((x) => !g || String(x.group_id) === g);
  const agencyOpts = (agencies.data?.items ?? []).filter(
    (x) => (!g || String(x.group_id) === g) && (!c || String(x.company_id) === c),
  );
  const taskOpts = (tasks.data?.items ?? []).filter(
    (x) => (!g || String(x.group_id) === g) && (!c || String(x.company_id) === c) && (!a || String(x.agency_id) === a),
  );
  const dbOpts = (dbs.data?.items ?? []).filter((x) => !c || x.kind === "dwh" || String(x.company_id) === c);
  const active = fields.some((k) => values[k]);

  // ?group_id= de un grupo fuera del alcance (enlace compartido, permisos cambiados): se avisa y se limpia
  // en lugar de mostrar una lista vacía sin explicación.
  const [outOfScope, setOutOfScope] = useState(false);
  useEffect(() => {
    if (!has("group_id") || !g || !groups.data) return;
    if (!groups.data.items.some((x) => String(x.id) === g)) {
      setOutOfScope(true);
      onChange({ group_id: "", company_id: "", agency_id: "", task_id: "", database_id: "" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [g, groups.data]);

  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filtros">
      {outOfScope && (
        <div className="flex w-full items-center justify-between rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800" role="status">
          <span>Grupo fuera de su alcance: se quitó el filtro y se muestran solo los grupos que puede ver.</span>
          <button type="button" className="ml-3 font-medium hover:underline" onClick={() => setOutOfScope(false)}>
            Entendido
          </button>
        </div>
      )}
      {has("group_id") && (
        <Select
          aria-label="Filtrar por grupo"
          className="w-full sm:w-44"
          value={g}
          onChange={(e) => onChange({ group_id: e.target.value, company_id: "", agency_id: "", task_id: "", database_id: "" })}
        >
          <option value="">Todos los grupos</option>
          {(groups.data?.items ?? []).map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </Select>
      )}
      {has("company_id") && (
        <Select
          aria-label="Filtrar por empresa"
          className="w-full sm:w-48"
          value={c}
          onChange={(e) => onChange({ company_id: e.target.value, agency_id: "", task_id: "" })}
        >
          <option value="">Todas las empresas</option>
          {companyOpts.map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </Select>
      )}
      {has("agency_id") && (
        <Select aria-label="Filtrar por agencia" className="w-full sm:w-48" value={a} onChange={(e) => onChange({ agency_id: e.target.value, task_id: "" })}>
          <option value="">Todas las agencias</option>
          {agencyOpts.map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </Select>
      )}
      {has("database_id") && (
        <Select aria-label="Filtrar por base" className="w-full sm:w-52" value={values.database_id ?? ""} onChange={(e) => onChange({ database_id: e.target.value })}>
          <option value="">Todas las bases</option>
          {dbOpts.map((x) => (
            <option key={x.id} value={x.id}>
              {x.display_name}
            </option>
          ))}
        </Select>
      )}
      {has("task_id") && (
        <Select aria-label="Filtrar por tarea" className="w-full sm:w-52" value={values.task_id ?? ""} onChange={(e) => onChange({ task_id: e.target.value })}>
          <option value="">Todas las tareas</option>
          {taskOpts.map((x) => (
            <option key={x.id} value={x.id}>
              #{x.id} {x.object_name} · {x.agency_name}
            </option>
          ))}
        </Select>
      )}
      {has("status") && statusOptions && (
        <Select aria-label={`Filtrar por ${statusLabel.toLowerCase()}`} className="w-full sm:w-44" value={values.status ?? ""} onChange={(e) => onChange({ status: e.target.value })}>
          <option value="">{statusLabel}: todos</option>
          {statusOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
      )}
      {has("since") && (
        <label className="flex items-center gap-1 text-xs text-slate-500">
          Desde
          <Input type="date" aria-label="Desde" className="w-36" value={values.since ?? ""} onChange={(e) => onChange({ since: e.target.value })} />
        </label>
      )}
      {has("until") && (
        <label className="flex items-center gap-1 text-xs text-slate-500">
          Hasta
          <Input type="date" aria-label="Hasta" className="w-36" value={values.until ?? ""} onChange={(e) => onChange({ until: e.target.value })} />
        </label>
      )}
      {children}
      {onClear && active && (
        <Button variant="ghost" size="sm" icon={<FilterX className="h-4 w-4" />} onClick={onClear}>
          Limpiar
        </Button>
      )}
    </div>
  );
}
