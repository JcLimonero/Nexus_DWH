"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Lock } from "lucide-react";
import { api } from "@/lib/api";
import { CSRF_HEADER, CSRF_VALUE } from "@/lib/session";

/** Permisos del panel (sección 20 de DWH_README.md). */
export type Permission =
  | "view"
  | "incident.acknowledge"
  | "incident.close_queue"
  | "structure.acknowledge"
  | "structure.reclassify"
  | "inventory.approve_baseline"
  | "inventory.configure"
  | "inventory.view_definitions"
  | "credentials.manage"
  | "config.manage"
  | "audit.view"
  | "users.manage";

export interface Me {
  user: { id: number | null; username: string; display_name: string; is_superadmin: boolean; auth_kind: string };
  must_change_password: boolean;
  /** permiso → "all" (todos los grupos) o lista de group_id */
  permissions: Partial<Record<Permission, "all" | number[]>>;
  session_expires_at: string | null;
  idle_timeout_seconds: number;
  groups: { id: number; name: string }[];
}

interface SessionValue {
  me: Me | null;
  loading: boolean;
  error: string | null;
  /** ¿Tiene el permiso sobre ese grupo? Sin grupo (o null): solo con alcance global. */
  can: (perm: Permission, groupId?: number | null) => boolean;
  /** ¿Tiene el permiso en al menos un grupo? (para mostrar la acción) */
  canAny: (perm: Permission) => boolean;
  /** ¿Tiene el permiso con alcance global? */
  canGlobal: (perm: Permission) => boolean;
  /** ¿Tiene el permiso en TODOS esos grupos? (recursos compartidos, p. ej. un DWH de varios grupos) */
  canAll: (perm: Permission, groupIds: (number | null | undefined)[]) => boolean;
  reload: () => void;
}

const Ctx = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const d = await api<Me>("admin/auth/me");
      setMe(d);
      setError(null);
      if (d.must_change_password && typeof window !== "undefined" && window.location.pathname !== "/cambiar-contrasena") {
        window.location.href = "/cambiar-contrasena";
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const value = useMemo<SessionValue>(() => {
    const perms = me?.permissions ?? {};
    const can = (perm: Permission, groupId?: number | null) => {
      if (!me) return false;
      if (me.user.is_superadmin) return true;
      const g = perms[perm];
      if (g === "all") return true;
      if (!g || groupId === undefined || groupId === null) return false;
      return g.includes(Number(groupId));
    };
    const canAny = (perm: Permission) => {
      if (!me) return false;
      if (me.user.is_superadmin) return true;
      const g = perms[perm];
      return g === "all" || (Array.isArray(g) && g.length > 0);
    };
    const canGlobal = (perm: Permission) => Boolean(me && (me.user.is_superadmin || perms[perm] === "all"));
    const canAll = (perm: Permission, groupIds: (number | null | undefined)[]) => {
      const ids = groupIds.length ? groupIds : [null];
      return ids.every((g) => can(perm, g ?? null));
    };
    return { me, loading, error, can, canAny, canGlobal, canAll, reload: () => void load() };
  }, [me, loading, error, load]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession fuera de SessionProvider");
  return v;
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST", headers: { [CSRF_HEADER]: CSRF_VALUE } }).catch(() => undefined);
  window.location.href = "/login";
}

/** Aviso de "Sin permiso" para páginas/secciones completas. */
export function NoPermission({ what }: { what?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 bg-white px-6 py-16 text-center">
      <Lock className="mb-3 h-8 w-8 text-slate-300" />
      <p className="text-sm font-medium text-slate-700">Sin permiso</p>
      <p className="mt-1 max-w-md text-xs text-slate-500">
        {what ?? "Su usuario no tiene permiso para ver esta sección."} Si lo necesita, pídalo a un administrador de usuarios.
      </p>
    </div>
  );
}

export const PERMISSION_LABEL: Record<Permission, string> = {
  view: "Consultar",
  "incident.acknowledge": "Reconocer incidencias/eventos",
  "incident.close_queue": "Cerrar incidencias de cola",
  "structure.acknowledge": "Dar por entendido (atribuir)",
  "structure.reclassify": "Reclasificar cambios",
  "inventory.approve_baseline": "Aprobar línea base",
  "inventory.configure": "Configurar inventario",
  "inventory.view_definitions": "Ver SQL de vistas",
  "credentials.manage": "Administrar credenciales",
  "config.manage": "Administrar configuración",
  "audit.view": "Ver auditoría",
  "users.manage": "Administrar usuarios",
};
