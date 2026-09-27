"use client";

import { useState } from "react";
import { Ban, Info, KeyRound, RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Installation, LegacyClient, ListResponse } from "@/lib/types";
import { fmtAgo, fmtDateTz, fmtNumber, secondsSince, tzLabel } from "@/lib/format";
import { Badge, Button, Card, IconButton, PageHeader, Select } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useRefData } from "@/components/ref-data";
import { useActions } from "@/components/use-actions";

const SCOPE_LABEL: Record<string, string> = { group: "Grupo", company: "Empresa", agency: "Agencia" };

function scopeText(i: { group_name: string | null; company_name: string | null; agency_name: string | null }) {
  return [i.group_name, i.company_name, i.agency_name].filter(Boolean).join(" / ");
}

/** Verde < 3 min, ámbar < 1 h, rojo ≥ 1 h o nunca. */
function LastSeen({ value }: { value: string | null }) {
  const s = secondsSince(value);
  const tone = s === null ? "red" : s < 180 ? "green" : s < 3600 ? "amber" : "red";
  return (
    <div>
      <Badge tone={tone}>{fmtAgo(value)}</Badge>
      <p className="mt-1 whitespace-nowrap text-xs text-slate-500">{fmtDateTz(value)}</p>
    </div>
  );
}

export default function InstalacionesPage() {
  const [groupId, setGroupId] = useState("");
  const [status, setStatus] = useState("");
  const { data, loading, error, reload } = useApi<ListResponse<Installation>>(
    `admin/installations${qs({ group_id: groupId, status })}`,
  );
  const legacy = useApi<ListResponse<LegacyClient>>("admin/legacy-clients");
  const { groups } = useRefData();
  const { run, busy } = useActions(() => {
    void reload();
  });
  const items = data?.items ?? [];
  const legacyItems = (legacy.data?.items ?? []).filter((l) => !groupId || String(l.group_id) === groupId);

  return (
    <>
      <PageHeader
        title="Instalaciones"
        description={`Agentes ETL enrolados (una credencial por máquina). Horas en ${tzLabel()}.`}
        actions={
          <Button
            variant="secondary"
            icon={<RefreshCw className="h-4 w-4" />}
            onClick={() => {
              void reload();
              void legacy.reload();
            }}
            loading={loading && Boolean(data)}
          >
            Actualizar
          </Button>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select aria-label="Filtrar por grupo" className="w-full sm:w-52" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
          <option value="">Todos los grupos</option>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}
            </option>
          ))}
        </Select>
        <Select aria-label="Estado" className="w-full sm:w-40" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Todos los estados</option>
          <option value="active">Activas</option>
          <option value="revoked">Revocadas</option>
        </Select>
        {data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(items.length)} instalación(es)</span>}
      </div>

      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle="Sin instalaciones"
          emptyDescription="Ningún agente se ha enrolado todavía (POST /agent/enroll con un token de grupo, empresa o agencia)."
        >
          <Table>
            <thead>
              <tr>
                <Th>Instalación</Th>
                <Th>Alcance</Th>
                <Th>Estado</Th>
                <Th>Último contacto</Th>
                <Th>Versión</Th>
                <Th className="text-right">Cola</Th>
                <Th className="text-right">Fallos 24 h</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((i) => {
                const hb = i.last_heartbeat;
                return (
                  <Tr key={i.id} className={i.status === "revoked" ? "opacity-60" : undefined}>
                    <Td>
                      <p className="font-medium text-slate-900">{i.name}</p>
                      <p className="text-xs text-slate-500">{i.hostname || "—"}{i.os_info ? ` · ${i.os_info}` : ""}</p>
                      <p className="mt-0.5 font-mono text-[11px] text-slate-400" title={i.id}>
                        {i.id.slice(0, 8)}…
                      </p>
                    </Td>
                    <Td>
                      <Badge tone="blue">{SCOPE_LABEL[i.scope_type]}</Badge>
                      <p className="mt-1 text-xs text-slate-600">{scopeText(i)}</p>
                      <p className="text-[11px] text-slate-400">
                        Enrolada con token de {SCOPE_LABEL[i.enrolled_via]?.toLowerCase()} ({i.enrollment_token_prefix}…)
                      </p>
                    </Td>
                    <Td>
                      <div className="flex flex-col items-start gap-1">
                        {i.status === "active" ? <Badge tone="green">Activa</Badge> : <Badge tone="red">Revocada</Badge>}
                        {i.rotation_required && <Badge tone="amber">Rotación pendiente</Badge>}
                        {i.rotation_in_grace && <Badge tone="slate">Secreto anterior en gracia</Badge>}
                      </div>
                      {i.status === "revoked" && (
                        <p className="mt-1 max-w-[14rem] text-xs text-slate-500">
                          {fmtDateTz(i.revoked_at)}
                          {i.revoked_reason ? ` · ${i.revoked_reason}` : ""}
                        </p>
                      )}
                    </Td>
                    <Td>
                      <LastSeen value={i.last_seen_at} />
                    </Td>
                    <Td className="whitespace-nowrap font-mono text-xs">{i.client_version || "—"}</Td>
                    <Td className="text-right tabular-nums">
                      {hb?.queue_depth ?? "—"}
                      {hb?.queue_overflow_total ? <p className="text-xs text-red-600">{hb.queue_overflow_total} descartes</p> : null}
                      {hb?.running?.length ? <p className="text-xs text-brand-700">{hb.running.length} en curso</p> : null}
                    </Td>
                    <Td className="text-right tabular-nums">
                      {i.failures_24h ? <Badge tone="red">{i.failures_24h}</Badge> : <span className="text-slate-400">0</span>}
                    </Td>
                    <Td className="text-right">
                      {i.status === "active" && (
                        <div className="flex justify-end gap-0.5">
                          <IconButton
                            label="Rotar credencial"
                            disabled={busy === `rot-${i.id}`}
                            onClick={() =>
                              run(`rot-${i.id}`, `admin/installations/${i.id}/rotate`, {
                                success: "Rotación solicitada: el agente cambiará su secreto en el próximo contacto.",
                                confirm: {
                                  title: "Rotar credencial",
                                  message: "El agente obtendrá un secreto nuevo en su próximo contacto con Nexus. El panel nunca ve el secreto.",
                                  confirmLabel: "Solicitar rotación",
                                },
                              })
                            }
                          >
                            <KeyRound className="h-4 w-4" />
                          </IconButton>
                          <IconButton
                            label="Revocar instalación"
                            tone="danger"
                            disabled={busy === `rev-${i.id}`}
                            onClick={() =>
                              run(`rev-${i.id}`, `admin/installations/${i.id}/revoke`, {
                                body: { reason: "Revocada desde el panel" },
                                success: "Instalación revocada.",
                                confirm: {
                                  title: "Revocar instalación",
                                  message: (
                                    <>
                                      <b>{i.name}</b> dejará de poder autenticarse y el agente se detendrá. Para volver a operar
                                      habrá que re-enrolar la máquina (<code>--enroll</code>) con un token de enrolamiento vigente.
                                    </>
                                  ),
                                  confirmLabel: "Revocar",
                                  danger: true,
                                },
                              })
                            }
                          >
                            <Ban className="h-4 w-4" />
                          </IconButton>
                        </div>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <div className="mb-3 mt-8 flex items-center gap-2">
        <h2 className="text-base font-semibold text-slate-900">Clientes legados (tokens)</h2>
        <Badge tone="amber">Obsoleto</Badge>
      </div>
      <p className="mb-3 flex items-start gap-2 text-sm text-slate-500">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        Agentes antiguos que aún operan con token de grupo, empresa o agencia (endpoints /configs, /group-configs,
        /agency-configs, /client-event). Actualícelos al agente v5 para tener identidad por instalación. Datos de los últimos 30 días.
      </p>
      <Card>
        <DataState
          loading={legacy.loading}
          error={legacy.error}
          hasData={Boolean(legacy.data)}
          empty={legacyItems.length === 0}
          onRetry={legacy.reload}
          emptyTitle="Sin clientes legados"
          emptyDescription="Ningún cliente usó los endpoints legados en los últimos 30 días."
        >
          <Table>
            <thead>
              <tr>
                <Th>Tipo de token</Th>
                <Th>Alcance</Th>
                <Th>Prefijo</Th>
                <Th>Último contacto</Th>
                <Th className="text-right">Peticiones 30 d</Th>
                <Th className="text-right">Errores HTTP</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {legacyItems.map((l, idx) => (
                <Tr key={`${l.auth_kind}-${l.token_prefix}-${idx}`}>
                  <Td>
                    <Badge tone="amber">{SCOPE_LABEL[l.auth_kind] ?? l.auth_kind}</Badge>
                  </Td>
                  <Td className="text-xs text-slate-600">{scopeText(l) || "—"}</Td>
                  <Td className="font-mono text-xs">{l.token_prefix}…</Td>
                  <Td>
                    <LastSeen value={l.last_seen_at} />
                  </Td>
                  <Td className="text-right tabular-nums">{fmtNumber(l.requests_30d)}</Td>
                  <Td className="text-right tabular-nums">{fmtNumber(l.http_errors_30d)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>
    </>
  );
}
