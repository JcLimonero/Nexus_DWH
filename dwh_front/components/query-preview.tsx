"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Badge, Button, Field, Input, Textarea } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/feedback";

/**
 * "Crear un extractor desde el query" (DWH_README.md, sección 24): ejecuta el
 * query contra la BD de origen (lo hace el agente, nunca Nexus), muestra
 * columnas/tipos detectados + una muestra de hasta 20 filas para elegir las
 * llaves, y genera la definición (destination_table, create_table_sql,
 * upsert_keys, constraint_name) que "Usar esta definición" copia al resto del
 * formulario del objeto del catálogo. También permite crear la tabla en el
 * DWH y validar el upsert antes de guardar.
 */

type Status = "pending" | "running" | "ok" | "failed" | "expired" | "no_agent";

interface ColumnMeta {
  name: string;
  source_type: string;
  nullable: boolean;
  suggested_name: string;
  renamed_from: string | null;
  suggested_pg_type: string;
  type_warning: string | null;
}

interface CommandOut {
  id: string;
  status: Status;
  result: { columns?: ColumnMeta[]; row_count?: number; upsert?: UpsertReport } | null;
  error_code: string | null;
  message: string | null;
  has_sample_rows: boolean;
  rows?: unknown[][];
}

interface UpsertReport {
  inserted: number;
  updated: number;
  duplicate_keys_in_sample: number;
  null_keys_in_sample: number;
  column_errors: Record<string, string>;
  rolled_back: boolean;
}

interface EditableColumn extends ColumnMeta {
  name_edit: string;
  type_edit: string;
  is_key: boolean;
}

function quoteSnake(s: string): string {
  return (s || "col").toLowerCase();
}

async function poll(id: string, includeRows: boolean, onTick?: (c: CommandOut) => void): Promise<CommandOut> {
  const started = Date.now();
  while (Date.now() - started < 180_000) {
    const c = await api<CommandOut>(`admin/query-commands/${id}${includeRows ? "?include_rows=true" : ""}`);
    onTick?.(c);
    if (c.status === "ok" || c.status === "failed" || c.status === "expired" || c.status === "no_agent") return c;
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error("El comando tardó demasiado en responder.");
}

export function QueryPreviewBuilder({
  companyId,
  onApply,
}: {
  companyId: number | null;
  /** Copia la definición generada al resto del formulario del objeto. */
  onApply: (def: {
    destination_table: string;
    create_table_sql: string;
    upsert_keys: string;
    constraint_name: string;
    create_constraint_sql: string;
  }) => void;
}) {
  const toast = useToast();
  const [sql, setSql] = useState("");
  const [table, setTable] = useState("");
  const [status, setStatus] = useState<"idle" | "running" | "done">("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [columns, setColumns] = useState<EditableColumn[]>([]);
  const [rows, setRows] = useState<unknown[][] | null>(null);
  const [rowCount, setRowCount] = useState<number | null>(null);
  const [upsertReport, setUpsertReport] = useState<UpsertReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const lastCommandId = useRef<string | null>(null);

  useEffect(() => {
    setColumns([]);
    setRows(null);
    setStatus("idle");
    setMessage(null);
  }, [companyId]);

  const keyColumns = columns.filter((c) => c.is_key).map((c) => c.name_edit);

  async function runPreview() {
    if (!companyId) return toast.error("Selecciona primero la empresa.");
    if (!sql.trim()) return toast.error("Escribe el query de extracción.");
    setBusy("preview");
    setStatus("running");
    setMessage("Pidiendo a un agente en línea que ejecute el query…");
    setColumns([]);
    setRows(null);
    setUpsertReport(null);
    try {
      const created = await api<CommandOut>("admin/query-commands", {
        method: "POST",
        body: { kind: "query_preview", company_id: companyId, extract_sql: sql, sample_limit: 20 },
      });
      lastCommandId.current = created.id;
      const final = await poll(created.id, false, (c) => {
        if (c.status === "pending") setMessage("En espera de un agente en línea…");
        if (c.status === "running") setMessage("El agente está ejecutando el query…");
      });
      if (final.status === "no_agent") {
        setStatus("idle");
        setMessage("No hay ningún agente en línea con la capacidad query-preview (actualice el agente a 5.4).");
        return;
      }
      if (final.status !== "ok") {
        setStatus("idle");
        setMessage(final.message || `El agente no pudo ejecutar el query (${final.error_code ?? "error"}).`);
        return;
      }
      const cols = (final.result?.columns ?? []) as ColumnMeta[];
      setColumns(
        cols.map((c) => ({ ...c, name_edit: c.suggested_name || quoteSnake(c.name), type_edit: c.suggested_pg_type, is_key: false })),
      );
      setRowCount(final.result?.row_count ?? null);
      if (final.has_sample_rows) {
        const withRows = await api<CommandOut>(`admin/query-commands/${final.id}?include_rows=true`);
        setRows(withRows.rows ?? null);
      }
      setStatus("done");
      setMessage(null);
    } catch (e) {
      setStatus("idle");
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function applyDefinition() {
    if (!table.trim()) return toast.error("Indica la tabla destino.");
    if (columns.length === 0) return toast.error("Ejecuta primero la prueba.");
    const keys = keyColumns;
    const colsSql = columns.map((c) => `  "${c.name_edit}" ${c.type_edit}${c.nullable ? "" : " NOT NULL"}`).join(",\n");
    const pk = keys.length ? `,\n  PRIMARY KEY (${keys.map((k) => `"${k}"`).join(", ")})` : "";
    const ddl = `CREATE TABLE IF NOT EXISTS "${table.trim().split(".").pop()}" (\n${colsSql}${pk}\n)`;
    onApply({
      destination_table: table.trim(),
      create_table_sql: ddl,
      upsert_keys: keys.join(", "),
      constraint_name: "",
      create_constraint_sql: "",
    });
    toast.success("Definición copiada al formulario. Revisa y guarda el objeto.");
  }

  async function createTable() {
    if (!companyId) return;
    if (!table.trim()) return toast.error("Indica la tabla destino.");
    setBusy("create_table");
    try {
      const created = await api<CommandOut>("admin/query-commands", {
        method: "POST",
        body: {
          kind: "create_table",
          company_id: companyId,
          destination_table: table.trim(),
          columns: columns.map((c) => ({ name: c.name_edit, pg_type: c.type_edit, nullable: c.nullable })),
          key_columns: keyColumns,
        },
      });
      const final = await poll(created.id, false);
      if (final.status === "ok") toast.success("Tabla creada (o ya existía) en el DWH.");
      else toast.error(final.message || "No se pudo crear la tabla.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function validateUpsert() {
    if (!companyId) return;
    if (!table.trim()) return toast.error("Indica la tabla destino.");
    if (keyColumns.length === 0) return toast.error("Selecciona al menos una columna llave.");
    setBusy("upsert_check");
    setUpsertReport(null);
    try {
      const created = await api<CommandOut>("admin/query-commands", {
        method: "POST",
        body: { kind: "upsert_check", company_id: companyId, extract_sql: sql, destination_table: table.trim(), key_columns: keyColumns, sample_limit: 20 },
      });
      const final = await poll(created.id, false);
      if (final.status === "ok" && final.result?.upsert) {
        setUpsertReport(final.result.upsert);
        toast.success("Validación de upsert completada (sin confirmar cambios: se hizo ROLLBACK).");
      } else {
        toast.error(final.message || "No se pudo validar el upsert.");
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="col-span-full rounded-lg border border-dashed border-indigo-300 bg-indigo-50/40 p-4">
      <p className="mb-2 text-sm font-medium text-indigo-900">Crear un extractor desde el query</p>
      <p className="mb-3 text-xs text-slate-600">
        Escribe el SELECT contra la BD de origen; un agente en línea lo ejecuta (Nexus nunca se conecta a la base del cliente), trae hasta 20 filas de muestra y
        sugiere el tipo de columna destino. Elige las llaves, aplica la definición y, si quieres, crea la tabla y valida el upsert antes de guardar.
      </p>
      <Field label="Query de origen" className="mb-3" htmlFor="qp-sql">
        <Textarea id="qp-sql" mono rows={5} value={sql} onChange={(e) => setSql(e.target.value)} placeholder="SELECT id, nombre, fecha_modificacion FROM clientes WHERE fecha_modificacion >= '{last_run}'" />
      </Field>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <Field label="Tabla destino" htmlFor="qp-table" className="w-56">
          <Input id="qp-table" className="font-mono" value={table} onChange={(e) => setTable(e.target.value)} placeholder="esquema.tabla (opcional)" />
        </Field>
        <Button type="button" loading={busy === "preview"} disabled={!companyId || busy !== null} onClick={runPreview}>
          Ejecutar prueba
        </Button>
        {columns.length > 0 && (
          <Button type="button" variant="secondary" loading={busy === "create_table"} disabled={busy !== null} onClick={createTable}>
            Crear tabla en el DWH
          </Button>
        )}
        {columns.length > 0 && (
          <Button type="button" variant="secondary" loading={busy === "upsert_check"} disabled={busy !== null} onClick={validateUpsert}>
            Validar upsert
          </Button>
        )}
      </div>
      {message && <p className="mb-3 text-sm text-slate-600">{message}</p>}

      {columns.length > 0 && (
        <>
          <div className="mb-3 overflow-x-auto rounded border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-2 py-1.5">Columna origen</th>
                  <th className="px-2 py-1.5">Tipo origen</th>
                  <th className="px-2 py-1.5">Nombre destino</th>
                  <th className="px-2 py-1.5">Tipo destino</th>
                  <th className="px-2 py-1.5">Nulos</th>
                  <th className="px-2 py-1.5">Llave</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {columns.map((c, i) => (
                  <tr key={c.name}>
                    <td className="px-2 py-1 font-mono text-xs">
                      {c.name}
                      {c.renamed_from && <span className="ml-1 text-amber-600" title={`Renombrada de "${c.renamed_from}"`}>*</span>}
                    </td>
                    <td className="px-2 py-1 font-mono text-xs text-slate-500">{c.source_type}</td>
                    <td className="px-2 py-1">
                      <Input
                        className="h-7 font-mono text-xs"
                        value={c.name_edit}
                        onChange={(e) => setColumns((cs) => cs.map((x, j) => (j === i ? { ...x, name_edit: e.target.value } : x)))}
                      />
                    </td>
                    <td className="px-2 py-1">
                      <Input
                        className="h-7 font-mono text-xs"
                        value={c.type_edit}
                        onChange={(e) => setColumns((cs) => cs.map((x, j) => (j === i ? { ...x, type_edit: e.target.value } : x)))}
                      />
                      {c.type_warning && <p className="mt-0.5 text-[11px] text-amber-600">{c.type_warning}</p>}
                    </td>
                    <td className="px-2 py-1 text-center">{c.nullable ? "Sí" : "No"}</td>
                    <td className="px-2 py-1 text-center">
                      <input
                        type="checkbox"
                        checked={c.is_key}
                        onChange={(e) => setColumns((cs) => cs.map((x, j) => (j === i ? { ...x, is_key: e.target.checked } : x)))}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {rows && (
            <div className="mb-3">
              <p className="mb-1 text-xs font-medium text-slate-500">
                Datos de muestra ({rowCount ?? rows.length} fila(s) — <span className="italic">no se guardan</span>)
              </p>
              <div className="overflow-x-auto rounded border border-slate-200 bg-white">
                <table className="w-full text-xs">
                  <thead className="bg-slate-50">
                    <tr>
                      {columns.map((c) => (
                        <th key={c.name} className="whitespace-nowrap px-2 py-1 text-left font-mono">
                          {c.name_edit}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {rows.map((row, i) => (
                      <tr key={i}>
                        {row.map((v, j) => (
                          <td key={j} className="whitespace-nowrap px-2 py-1 font-mono">
                            {v === null ? <span className="text-slate-400">NULL</span> : String(v)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {!rows && status === "done" && <p className="mb-3 text-xs text-slate-500">La empresa no permite mostrar muestra de datos en el panel (solo columnas/tipos).</p>}

          {keyColumns.length > 0 && rows && (
            <KeyValidationHint rows={rows} columns={columns} keyColumns={keyColumns} />
          )}

          {upsertReport && (
            <div className="mb-3 rounded border border-slate-200 bg-white p-3 text-sm">
              <p className="mb-1 font-medium">Resultado de la validación de upsert (se hizo ROLLBACK: no se confirmó nada)</p>
              <div className="flex flex-wrap gap-2">
                <Badge tone="green">Insertadas: {upsertReport.inserted}</Badge>
                <Badge tone="blue">Actualizadas: {upsertReport.updated}</Badge>
                {upsertReport.duplicate_keys_in_sample > 0 && <Badge tone="amber">Llaves duplicadas en la muestra: {upsertReport.duplicate_keys_in_sample}</Badge>}
                {upsertReport.null_keys_in_sample > 0 && <Badge tone="red">Llaves nulas en la muestra: {upsertReport.null_keys_in_sample}</Badge>}
              </div>
              {Object.keys(upsertReport.column_errors).length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-xs text-red-700">
                  {Object.entries(upsertReport.column_errors).map(([k, v]) => (
                    <li key={k}>
                      {k}: {v}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <Button type="button" onClick={applyDefinition}>
            Usar esta definición
          </Button>
        </>
      )}
    </div>
  );
}

function KeyValidationHint({ rows, columns, keyColumns }: { rows: unknown[][]; columns: EditableColumn[]; keyColumns: string[] }) {
  const idxs = keyColumns.map((k) => columns.findIndex((c) => c.name_edit === k));
  const seen = new Set<string>();
  let dup = 0;
  let nulls = 0;
  for (const row of rows) {
    const key = idxs.map((i) => (i >= 0 ? row[i] : null));
    if (key.some((v) => v === null || v === undefined)) nulls += 1;
    else {
      const k = JSON.stringify(key);
      if (seen.has(k)) dup += 1;
      else seen.add(k);
    }
  }
  if (dup === 0 && nulls === 0) {
    return <p className="mb-3 text-xs text-emerald-700">Las llaves elegidas son únicas y no nulas en la muestra.</p>;
  }
  return (
    <p className="mb-3 text-xs text-amber-700">
      Aviso: en la muestra hay {dup > 0 ? `${dup} llave(s) repetida(s)` : ""}
      {dup > 0 && nulls > 0 ? " y " : ""}
      {nulls > 0 ? `${nulls} llave(s) nula(s)` : ""}. Puede indicar que estas columnas no son una llave válida.
    </p>
  );
}
