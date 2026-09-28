"use client";

import { useState, type FormEvent } from "react";
import { Ban, KeyRound, LockOpen, Pencil, Plus, RefreshCw, ShieldCheck, Trash2, UserCog, Wand2 } from "lucide-react";
import { useApi } from "@/lib/api";
import type { Group, ListResponse, PanelRole, PanelSession, PanelUser } from "@/lib/types";
import { fmtAgo, fmtDateTz, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useActions } from "@/components/use-actions";
import { NoPermission, PERMISSION_LABEL, useSession, type Permission } from "@/components/session";

interface RoleRow {
  role: string;
  group_id: string; // "" = todos los grupos
}

interface FormState {
  username: string;
  display_name: string;
  email: string;
  password: string;
  must_change_password: boolean;
  is_superadmin: boolean;
  is_active: boolean;
  roles: RoleRow[];
}

const EMPTY: FormState = {
  username: "",
  display_name: "",
  email: "",
  password: "",
  must_change_password: true,
  is_superadmin: false,
  is_active: true,
  roles: [{ role: "lectura", group_id: "" }],
};

/** Contraseña temporal aleatoria (se muestra una vez para entregarla al usuario). */
function randomPassword(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.!";
  const bytes = new Uint32Array(18);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

export default function UsuariosPage() {
  const { canAny, me, loading: sessionLoading } = useSession();
  if (sessionLoading) return null;
  if (!canAny("users.manage")) {
    return (
      <>
        <PageHeader title="Usuarios" />
        <NoPermission what="Administrar usuarios requiere el permiso «Administrar usuarios» con alcance global." />
      </>
    );
  }
  return <UsersAdmin isSuper={Boolean(me?.user.is_superadmin)} myId={me?.user.id ?? null} />;
}

function UsersAdmin({ isSuper, myId }: { isSuper: boolean; myId: number | null }) {
  const users = useApi<ListResponse<PanelUser>>("admin/users");
  const roles = useApi<{ roles: PanelRole[]; permissions: { code: string; description: string; global_only: boolean }[] }>("admin/roles");
  const groups = useApi<ListResponse<Group>>("admin/groups");
  const sessions = useApi<ListResponse<PanelSession>>("admin/sessions");
  const { me } = useSession();
  const minPw = me?.password_min_length ?? 8;
  const { run, busy } = useActions(() => {
    void users.reload();
    void sessions.reload();
  });
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<PanelUser | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [resetFor, setResetFor] = useState<PanelUser | null>(null);
  const [resetPw, setResetPw] = useState("");
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));
  const roleList = roles.data?.roles ?? [];
  const selfRoles = Boolean(editing && editing.id === myId && !isSuper);
  const groupList = groups.data?.items ?? [];
  const items = users.data?.items ?? [];

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY, password: randomPassword() });
    setOpen(true);
  }
  function openEdit(u: PanelUser) {
    setEditing(u);
    setForm({
      username: u.username,
      display_name: u.display_name,
      email: u.email ?? "",
      password: "",
      must_change_password: u.must_change_password,
      is_superadmin: u.is_superadmin,
      is_active: u.is_active,
      roles: u.roles.map((r) => ({ role: r.role, group_id: r.group_id ? String(r.group_id) : "" })),
    });
    setOpen(true);
  }

  const rolesBody = () =>
    form.roles.filter((r) => r.role).map((r) => ({ role: r.role, group_id: r.group_id ? Number(r.group_id) : null }));

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (editing) {
      const body: Record<string, unknown> = {
        display_name: form.display_name.trim(),
        email: form.email.trim(),
        is_active: form.is_active,
      };
      if (isSuper) body.is_superadmin = form.is_superadmin;
      const ok = await run("save", `admin/users/${editing.id}`, { method: "PUT", body });
      if (!ok) return;
      if (editing.id === myId && !isSuper) {
        setOpen(false);
        return;
      }
      const ok2 = await run("save", `admin/users/${editing.id}/roles`, {
        method: "PUT",
        body: { roles: rolesBody() },
        success: "Usuario actualizado.",
      });
      if (ok2) setOpen(false);
      return;
    }
    const res = await run("save", "admin/users", {
      method: "POST",
      body: {
        username: form.username.trim().toLowerCase(),
        display_name: form.display_name.trim(),
        email: form.email.trim() || null,
        password: form.password,
        must_change_password: form.must_change_password,
        is_superadmin: isSuper ? form.is_superadmin : false,
        roles: rolesBody(),
      },
      success: "Usuario creado. Entréguele la contraseña temporal por un canal seguro.",
    });
    if (res) setOpen(false);
  }

  function roleLabel(code: string) {
    return roleList.find((r) => r.code === code)?.name ?? code;
  }
  const globalOnly = (code: string) => Boolean(roleList.find((r) => r.code === code)?.global_only);

  return (
    <>
      <PageHeader
        title="Usuarios"
        description="Usuarios del panel, roles por grupo y sesiones. Los cambios quedan en Auditoría."
        actions={
          <>
            <Button
              variant="secondary"
              icon={<RefreshCw className="h-4 w-4" />}
              onClick={() => {
                void users.reload();
                void sessions.reload();
              }}
            >
              Actualizar
            </Button>
            <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate}>
              Nuevo usuario
            </Button>
          </>
        }
      />

      <Card className="mb-6">
        <DataState loading={users.loading} error={users.error} hasData={Boolean(users.data)} empty={items.length === 0} onRetry={users.reload} emptyTitle="Sin usuarios">
          <Table>
            <thead>
              <tr>
                <Th>Usuario</Th>
                <Th>Roles (alcance)</Th>
                <Th>Estado</Th>
                <Th>Último acceso</Th>
                <Th className="text-right">Sesiones</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((u) => (
                <Tr key={u.id} className={u.is_active ? undefined : "opacity-60"}>
                  <Td>
                    <p className="font-medium text-slate-900">{u.display_name}</p>
                    <p className="font-mono text-xs text-slate-500">{u.username}</p>
                    {u.email && <p className="text-xs text-slate-500">{u.email}</p>}
                  </Td>
                  <Td>
                    <div className="flex max-w-md flex-wrap gap-1">
                      {u.is_superadmin && <Badge tone="red">Superadministrador</Badge>}
                      {u.roles.map((r) => (
                        <Badge key={`${r.role}-${r.group_id ?? 0}`} tone="blue">
                          {r.role_name} · {r.group_name ?? "Todos los grupos"}
                        </Badge>
                      ))}
                      {!u.is_superadmin && u.roles.length === 0 && <span className="text-xs italic text-slate-400">Sin roles</span>}
                    </div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {u.is_active ? <Badge tone="green">Activo</Badge> : <Badge>Inactivo</Badge>}
                      {u.locked && <Badge tone="red">Bloqueado</Badge>}
                      {u.must_change_password && <Badge tone="amber">Debe cambiar contraseña</Badge>}
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    {u.last_login_at ? (
                      <>
                        <p>{fmtAgo(u.last_login_at)}</p>
                        <p className="text-slate-500">{fmtDateTz(u.last_login_at)}</p>
                      </>
                    ) : (
                      <span className="text-slate-400">nunca</span>
                    )}
                  </Td>
                  <Td className="text-right tabular-nums">{u.active_sessions}</Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      <IconButton label="Editar datos y roles" onClick={() => openEdit(u)} disabled={u.is_superadmin && !isSuper}>
                        <Pencil className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Reiniciar contraseña"
                        disabled={u.is_superadmin && !isSuper}
                        onClick={() => {
                          setResetFor(u);
                          setResetPw(randomPassword());
                        }}
                      >
                        <KeyRound className="h-4 w-4" />
                      </IconButton>
                      {u.locked && !(u.is_superadmin && !isSuper) && (
                        <IconButton label="Desbloquear" onClick={() => run(`unlock-${u.id}`, `admin/users/${u.id}/unlock`, { success: "Usuario desbloqueado." })}>
                          <LockOpen className="h-4 w-4" />
                        </IconButton>
                      )}
                      {u.id !== myId && u.is_active && !(u.is_superadmin && !isSuper) && (
                        <IconButton
                          label="Desactivar (cierra sus sesiones)"
                          tone="danger"
                          onClick={() =>
                            run(`off-${u.id}`, `admin/users/${u.id}`, {
                              method: "PUT",
                              body: { is_active: false },
                              success: "Usuario desactivado y sesiones cerradas.",
                              confirm: {
                                title: "Desactivar usuario",
                                message: `«${u.username}» no podrá iniciar sesión y se cerrarán sus sesiones abiertas.`,
                                confirmLabel: "Desactivar",
                                danger: true,
                              },
                            })
                          }
                        >
                          <Ban className="h-4 w-4" />
                        </IconButton>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-base font-semibold text-slate-900">Sesiones activas</h2>
        <span className="text-xs text-slate-500">Horas en {tzLabel()}. Vencen por inactividad y por tiempo absoluto.</span>
      </div>
      <Card className="mb-6">
        <DataState
          loading={sessions.loading}
          error={sessions.error}
          hasData={Boolean(sessions.data)}
          empty={(sessions.data?.items ?? []).length === 0}
          onRetry={sessions.reload}
          emptyTitle="Sin sesiones activas"
        >
          <Table>
            <thead>
              <tr>
                <Th>Usuario</Th>
                <Th>Inicio</Th>
                <Th>Última actividad</Th>
                <Th>Vence</Th>
                <Th>IP / navegador</Th>
                <Th className="text-right">Acción</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {(sessions.data?.items ?? []).map((s) => (
                <Tr key={s.id}>
                  <Td className="font-mono text-xs">
                    {s.username} {s.current && <Badge tone="green">esta sesión</Badge>}
                  </Td>
                  <Td className="whitespace-nowrap text-xs">{fmtDateTz(s.created_at)}</Td>
                  <Td className="whitespace-nowrap text-xs">{fmtAgo(s.last_seen_at)}</Td>
                  <Td className="whitespace-nowrap text-xs">{fmtDateTz(s.expires_at)}</Td>
                  <Td className="max-w-xs text-xs">
                    <p className="font-mono">{s.ip || "—"}</p>
                    <p className="truncate text-slate-500" title={s.user_agent ?? undefined}>
                      {s.user_agent || "—"}
                    </p>
                  </Td>
                  <Td className="text-right">
                    {s.is_superadmin && !isSuper && !s.current ? (
                      <span className="text-xs text-slate-400">Solo un superadministrador</span>
                    ) : (
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={busy === `rev-${s.id}`}
                      onClick={() =>
                        run(`rev-${s.id}`, `admin/sessions/${s.id}/revoke`, {
                          success: "Sesión cerrada.",
                          confirm: s.current
                            ? { title: "Cerrar su propia sesión", message: "Tendrá que volver a iniciar sesión.", confirmLabel: "Cerrar", danger: true }
                            : undefined,
                        })
                      }
                    >
                      Cerrar sesión
                    </Button>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <h2 className="mb-3 text-base font-semibold text-slate-900">Roles y permisos</h2>
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Rol</Th>
              <Th>Permisos</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {roleList.map((r) => (
              <Tr key={r.code}>
                <Td>
                  <p className="font-medium text-slate-900">{r.name}</p>
                  <p className="font-mono text-xs text-slate-500">{r.code}</p>
                  {r.global_only && <p className="text-xs text-amber-700">Solo alcance global</p>}
                </Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    {r.permissions.map((p) => (
                      <Badge key={p}>{PERMISSION_LABEL[p as Permission] ?? p}</Badge>
                    ))}
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </Card>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        size="lg"
        title={editing ? `Editar usuario: ${editing.username}` : "Nuevo usuario"}
        description="Los permisos se asignan por rol y alcance: todos los grupos o un grupo concreto."
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="user-form" loading={busy === "save"}>
              {editing ? "Guardar" : "Crear usuario"}
            </Button>
          </>
        }
      >
        <form id="user-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Usuario" required className="sm:col-span-3" htmlFor="u-name" hint="3-64: minúsculas, números, punto, guion o guion bajo.">
            <Input
              id="u-name"
              required
              disabled={Boolean(editing)}
              autoComplete="off"
              pattern="[a-z0-9][a-z0-9._\-]{2,63}"
              value={form.username}
              onChange={(e) => set("username", e.target.value.toLowerCase())}
            />
          </Field>
          <Field label="Nombre visible" className="sm:col-span-3" htmlFor="u-display">
            <Input id="u-display" maxLength={120} value={form.display_name} onChange={(e) => set("display_name", e.target.value)} />
          </Field>
          <Field label="Correo (opcional)" className="sm:col-span-6" htmlFor="u-email">
            <Input id="u-email" type="email" maxLength={255} value={form.email} onChange={(e) => set("email", e.target.value)} />
          </Field>
          {!editing && (
            <Field
              label="Contraseña temporal"
              required
              className="sm:col-span-6"
              htmlFor="u-pass"
              hint={`Mínimo ${minPw} caracteres. Entréguela por un canal seguro; el usuario deberá cambiarla al entrar.`}
            >
              <div className="flex gap-2">
                <Input id="u-pass" required className="font-mono" autoComplete="new-password" value={form.password} onChange={(e) => set("password", e.target.value)} />
                <Button type="button" variant="secondary" icon={<Wand2 className="h-4 w-4" />} onClick={() => set("password", randomPassword())}>
                  Generar
                </Button>
              </div>
            </Field>
          )}
          <div className="grid gap-3 sm:col-span-6 sm:grid-cols-2">
            {!editing && (
              <Switch checked={form.must_change_password} onChange={(v) => set("must_change_password", v)} label="Debe cambiarla al entrar" />
            )}
            {editing && (
              <Switch
                checked={form.is_active}
                disabled={editing.id === myId}
                onChange={(v) => set("is_active", v)}
                label="Activo"
                description={editing.id === myId ? "No puede desactivarse a sí mismo." : "Desactivar cierra sus sesiones."}
              />
            )}
            {isSuper && (
              <Switch
                checked={form.is_superadmin}
                disabled={editing?.id === myId}
                onChange={(v) => set("is_superadmin", v)}
                label="Superadministrador"
                description="Todos los permisos en todos los grupos."
              />
            )}
          </div>

          <div className="sm:col-span-6">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                <UserCog className="h-3.5 w-3.5" /> Roles por alcance
              </h3>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                disabled={selfRoles}
                icon={<Plus className="h-3.5 w-3.5" />}
                onClick={() => set("roles", [...form.roles, { role: "lectura", group_id: "" }])}
              >
                Agregar rol
              </Button>
            </div>
            {selfRoles && (
              <p className="mb-2 text-xs text-amber-700">No puede modificar sus propios roles: pídalo a otro administrador.</p>
            )}
            {form.roles.length === 0 && <p className="text-xs italic text-slate-400">Sin roles: no verá nada (salvo que sea superadministrador).</p>}
            <fieldset disabled={selfRoles} className="space-y-2">
              {form.roles.map((r, idx) => (
                <div key={idx} className="flex flex-wrap items-center gap-2">
                  <Select
                    aria-label="Rol"
                    className="w-full sm:w-64"
                    value={r.role}
                    onChange={(e) => {
                      const roles = [...form.roles];
                      roles[idx] = { role: e.target.value, group_id: globalOnly(e.target.value) ? "" : r.group_id };
                      set("roles", roles);
                    }}
                  >
                    {roleList.map((x) => (
                      <option key={x.code} value={x.code}>
                        {x.name}
                      </option>
                    ))}
                  </Select>
                  <Select
                    aria-label="Alcance"
                    className="w-full sm:w-56"
                    value={r.group_id}
                    disabled={globalOnly(r.role)}
                    onChange={(e) => {
                      const roles = [...form.roles];
                      roles[idx] = { ...r, group_id: e.target.value };
                      set("roles", roles);
                    }}
                  >
                    <option value="">Todos los grupos</option>
                    {groupList.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.name}
                      </option>
                    ))}
                  </Select>
                  <IconButton label="Quitar rol" tone="danger" onClick={() => set("roles", form.roles.filter((_, i) => i !== idx))}>
                    <Trash2 className="h-4 w-4" />
                  </IconButton>
                  <span className="text-[11px] text-slate-500">
                    {(roleList.find((x) => x.code === r.role)?.permissions ?? []).map((p) => PERMISSION_LABEL[p as Permission] ?? p).join(" · ")}
                  </span>
                </div>
              ))}
            </fieldset>
          </div>
        </form>
      </Modal>

      <Modal
        open={Boolean(resetFor)}
        onClose={() => setResetFor(null)}
        title={resetFor ? `Reiniciar contraseña: ${resetFor.username}` : ""}
        description="Se cierran sus sesiones y deberá cambiarla en su próximo inicio de sesión."
        footer={
          <>
            <Button variant="secondary" onClick={() => setResetFor(null)}>
              Cancelar
            </Button>
            <Button
              icon={<ShieldCheck className="h-4 w-4" />}
              loading={busy === "reset"}
              onClick={async () => {
                if (!resetFor) return;
                const ok = await run("reset", `admin/users/${resetFor.id}/reset-password`, {
                  body: { password: resetPw },
                  success: "Contraseña reiniciada. Entréguela por un canal seguro.",
                });
                if (ok) setResetFor(null);
              }}
            >
              Reiniciar
            </Button>
          </>
        }
      >
        <Field label="Contraseña temporal" htmlFor="reset-pw" hint={`Mínimo ${minPw} caracteres.`}>
          <div className="flex gap-2">
            <Input id="reset-pw" className="font-mono" autoComplete="new-password" value={resetPw} onChange={(e) => setResetPw(e.target.value)} />
            <Button type="button" variant="secondary" icon={<Wand2 className="h-4 w-4" />} onClick={() => setResetPw(randomPassword())}>
              Generar
            </Button>
          </div>
        </Field>
      </Modal>
    </>
  );
}
