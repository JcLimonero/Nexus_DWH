"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  Bell,
  BellRing,
  HeartPulse,
  Siren,
  Boxes,
  History,
  MonitorSmartphone,
  Building2,
  Database,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Store,
  Users,
  X,
} from "lucide-react";
import { cx } from "@/lib/format";
import { useIncidentBadge } from "@/components/health";

const NAV = [
  { section: "General", items: [{ href: "/", label: "Dashboard", icon: LayoutDashboard }] },
  {
    section: "Configuración",
    items: [
      { href: "/grupos", label: "Grupos", icon: Users },
      { href: "/empresas", label: "Empresas", icon: Building2 },
      { href: "/agencias", label: "Agencias", icon: Store },
      { href: "/catalogo", label: "Catálogo de objetos", icon: Boxes },
      { href: "/tareas", label: "Tareas", icon: ListChecks },
    ],
  },
  {
    section: "Monitoreo",
    items: [
      { href: "/salud", label: "Salud", icon: HeartPulse },
      { href: "/incidencias", label: "Incidencias", icon: Siren },
      { href: "/notificaciones", label: "Notificaciones", icon: BellRing },
      { href: "/instalaciones", label: "Instalaciones", icon: MonitorSmartphone },
      { href: "/ejecuciones", label: "Ejecuciones", icon: History },
      { href: "/eventos", label: "Eventos", icon: Bell },
      { href: "/actividad", label: "Actividad", icon: Activity },
    ],
  },
];

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const badge = useIncidentBadge();
  return (
    <nav className="flex-1 space-y-6 overflow-y-auto px-3 py-4">
      {NAV.map((group) => (
        <div key={group.section}>
          <p className="px-3 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">{group.section}</p>
          <ul className="space-y-0.5">
            {group.items.map(({ href, label, icon: Icon }) => {
              const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    className={cx(
                      "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                      active ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
                    )}
                  >
                    <Icon className={cx("h-4 w-4", active ? "text-brand-600" : "text-slate-400")} />
                    {label}
                    {href === "/incidencias" && badge && badge.open_unacknowledged > 0 && (
                      <span
                        className={cx(
                          "ml-auto inline-flex min-w-[1.25rem] items-center justify-center rounded-full px-1.5 text-[11px] font-semibold",
                          badge.serious_unacknowledged > 0 ? "bg-red-600 text-white" : "bg-amber-100 text-amber-800",
                        )}
                        title={`${badge.open_unacknowledged} incidencia(s) abierta(s) sin reconocer`}
                        aria-label={`${badge.open_unacknowledged} incidencias abiertas sin reconocer`}
                      >
                        {badge.open_unacknowledged}
                      </span>
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => undefined);
  window.location.href = "/login";
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-white">
        <Database className="h-4 w-4" />
      </div>
      <div className="leading-tight">
        <p className="text-sm font-semibold text-slate-900">Nexus DWH</p>
        <p className="text-[11px] text-slate-500">Administración</p>
      </div>
    </div>
  );
}

function LogoutButton() {
  return (
    <div className="border-t border-slate-200 p-3">
      <button
        onClick={logout}
        className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900"
      >
        <LogOut className="h-4 w-4 text-slate-400" />
        Cerrar sesión
      </button>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  useEffect(() => setOpen(false), [pathname]);

  return (
    <div className="min-h-screen">
      {/* Sidebar escritorio */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-slate-200 bg-white lg:flex">
        <div className="flex h-16 items-center border-b border-slate-200 px-5">
          <Brand />
        </div>
        <NavLinks />
        <LogoutButton />
      </aside>

      {/* Barra superior móvil */}
      <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4 lg:hidden">
        <Brand />
        <button onClick={() => setOpen(true)} className="rounded-md p-2 text-slate-600 hover:bg-slate-100" aria-label="Abrir menú">
          <Menu className="h-5 w-5" />
        </button>
      </header>

      {/* Drawer móvil */}
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-slate-900/40" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-white shadow-xl">
            <div className="flex h-14 items-center justify-between border-b border-slate-200 px-4">
              <Brand />
              <button onClick={() => setOpen(false)} className="rounded-md p-2 text-slate-500 hover:bg-slate-100" aria-label="Cerrar menú">
                <X className="h-5 w-5" />
              </button>
            </div>
            <NavLinks onNavigate={() => setOpen(false)} />
            <LogoutButton />
          </aside>
        </div>
      )}

      <main className="lg:pl-64">
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">{children}</div>
      </main>
    </div>
  );
}
