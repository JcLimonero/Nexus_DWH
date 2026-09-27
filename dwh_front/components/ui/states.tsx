"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { AlertCircle, Inbox, RefreshCw, SearchX } from "lucide-react";
import { Button, Spinner } from "./primitives";

export function LoadingState({ label = "Cargando…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-slate-500">
      <Spinner /> {label}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-4 py-14 text-center">
      <AlertCircle className="h-8 w-8 text-red-400" />
      <div>
        <p className="text-sm font-medium text-slate-800">No se pudo cargar la información</p>
        <p className="mt-1 max-w-md text-sm text-slate-500">{message}</p>
      </div>
      {onRetry && (
        <Button variant="secondary" size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={onRetry}>
          Reintentar
        </Button>
      )}
    </div>
  );
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-4 py-14 text-center">
      <Inbox className="h-8 w-8 text-slate-300" />
      <div>
        <p className="text-sm font-medium text-slate-800">{title}</p>
        {description && <p className="mt-1 text-sm text-slate-500">{description}</p>}
      </div>
      {action}
    </div>
  );
}

/** Envoltorio: muestra loading/error/empty o el contenido. */
export function DataState({
  loading,
  error,
  empty,
  onRetry,
  emptyTitle = "Sin registros",
  emptyDescription,
  emptyAction,
  children,
  hasData,
}: {
  loading: boolean;
  error: string | null;
  empty: boolean;
  hasData: boolean;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  children: ReactNode;
}) {
  if (loading && !hasData) return <LoadingState />;
  if (error && !hasData) return <ErrorState message={error} onRetry={onRetry} />;
  if (empty) return <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />;
  return <>{children}</>;
}

/** Recurso inexistente o fuera del alcance del usuario (el backend responde 404 en ambos casos). */
export function NotFoundState({ title, backHref, backLabel }: { title: string; backHref: string; backLabel: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 bg-white px-6 py-16 text-center">
      <SearchX className="mb-3 h-8 w-8 text-slate-300" />
      <p className="text-sm font-medium text-slate-700">{title}</p>
      <p className="mt-1 max-w-md text-xs text-slate-500">No existe o no está dentro de los grupos que su usuario puede ver.</p>
      <Link href={backHref} className="mt-4 text-sm font-medium text-brand-600 hover:text-brand-700">
        {backLabel}
      </Link>
    </div>
  );
}
