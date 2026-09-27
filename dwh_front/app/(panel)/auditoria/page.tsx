"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { AuditEntry, ListResponse } from "@/lib/types";
import { fmtDateTz, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Input, PageHeader, Select } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { FilterBar, dateRange, useUrlFilters } from "@/components/scope-filters";
import { NoPermission, useSession } from "@/components/session";

const FILTER_KEYS = ["group_id", "status", "since", "until"] as const;

const AUTH_KIND: Record<string, string> = {
  session: "Usuario",
  static_token: "Token estático",
  anonymous: "Anónimo",
  cli: "CLI",
};

const ACTION_LABEL: Record<string, string> = {
  "auth.login": "Inicio de sesión",
  "auth.logout": "Cierre de sesión",
  "auth.login_failed": "Inicio de sesión fallido",
  "auth.login_locked": "Intento con cuenta bloqueada",
  "auth.login_blocked": "Intento bloqueado por IP",
  "auth.change_password": "Cambio de contraseña",
  "auth.change_password_failed": "Cambio de contraseña fallido",
  "users.create": "Alta de usuario",
  "users.update": "Edición de usuario",
  "users.reset_password": "Reinicio de contraseña",
  "users.set_roles": "Cambio de roles",
  "users.unlock": "Desbloqueo de usuario",
  "sessions.revoke": "Sesión cerrada por administrador",
  "cli.create_superadmin": "Superadministrador creado (CLI)",
};

export default function AuditoriaPage() {
  const { canAny, loading: sessionLoading } = useSession();
  if (sessionLoading) return null;
  if (!canAny("audit.view")) {
    return (
      <>
        <PageHeader title="Auditoría" />
        <NoPermission what="Consultar la auditoría requiere el permiso «Ver auditoría»." />
      </>
    );
  }
  return <AuditList />;
}

function AuditList() {
  const [f, setF, clearF] = useUrlFilters(FILTER_KEYS);
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [limit, setLimit] = useState("300");
  const { data, loading, error, reload } = useApi<ListResponse<AuditEntry>>(
    `admin/audit${qs({ group_id: f.group_id, status: f.status, actor: actor.trim(), action: action.trim(), ...dateRange(f.since, f.until), limit })}`,
  );
  const items = data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Auditoría"
        description={`Acciones del panel: inicios de sesión (también fallidos), cambios de configuración y credenciales, reconocimientos y usos del token estático. Horas en ${tzLabel()}.`}
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reload} loading={loading && Boolean(data)}>
            Actualizar
          </Button>
        }
      />
      <div className="mb-4 space-y-2">
        <FilterBar
          values={f}
          onChange={setF}
          onClear={clearF}
          fields={[...FILTER_KEYS]}
          statusLabel="Resultado"
          statusOptions={[
            { value: "ok", label: "Correctas" },
            { value: "error", label: "Rechazadas / con error" },
          ]}
        >
          <Input aria-label="Actor" placeholder="Usuario" className="w-full sm:w-36" value={actor} onChange={(e) => setActor(e.target.value)} />
          <Input aria-label="Acción" placeholder="Acción (contiene)" className="w-full sm:w-48" value={action} onChange={(e) => setAction(e.target.value)} />
          <Select aria-label="Límite" className="w-full sm:w-36" value={limit} onChange={(e) => setLimit(e.target.value)}>
            {["100", "300", "1000", "2000"].map((l) => (
              <option key={l} value={l}>
                Últimos {l}
              </option>
            ))}
          </Select>
        </FilterBar>
        <p className="text-xs text-slate-500">
          Con alcance por grupo solo se ven las acciones sobre recursos de sus grupos; las acciones sin grupo (inicios de sesión, usuarios) requieren
          alcance global.
        </p>
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="Sin registros">
          <Table>
            <thead>
              <tr>
                <Th>Fecha</Th>
                <Th>Actor</Th>
                <Th>Acción</Th>
                <Th>Recurso</Th>
                <Th>Grupo</Th>
                <Th>Resultado</Th>
                <Th>IP</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((a) => (
                <Tr key={a.id}>
                  <Td className="whitespace-nowrap text-xs">{fmtDateTz(a.at)}</Td>
                  <Td>
                    <p className="font-mono text-xs">{a.actor_name || "—"}</p>
                    <p className="text-[11px] text-slate-500">{AUTH_KIND[a.auth_kind] ?? a.auth_kind}</p>
                  </Td>
                  <Td>
                    <p className="text-sm">{ACTION_LABEL[a.action] ?? a.action}</p>
                    {Object.keys(a.details ?? {}).length > 0 && (
                      <p className="max-w-xs truncate font-mono text-[11px] text-slate-500" title={JSON.stringify(a.details)}>
                        {JSON.stringify(a.details)}
                      </p>
                    )}
                  </Td>
                  <Td className="font-mono text-xs">{a.target_type ? `${a.target_type} ${a.target_id ?? ""}` : "—"}</Td>
                  <Td className="text-xs">{a.group_name ?? "—"}</Td>
                  <Td>
                    {a.status_code == null ? (
                      "—"
                    ) : (
                      <Badge tone={a.status_code < 400 ? "green" : a.status_code === 403 || a.status_code === 404 ? "amber" : "red"}>
                        {a.status_code}
                      </Badge>
                    )}
                  </Td>
                  <Td className="font-mono text-xs">{a.ip || "—"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
        {data && <p className="border-t border-slate-100 px-4 py-2 text-xs text-slate-500">{fmtNumber(items.length)} registro(s)</p>}
      </Card>
    </>
  );
}
