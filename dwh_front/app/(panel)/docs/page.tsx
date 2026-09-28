"use client";

import { useMemo, useState } from "react";
import { Printer, Search, X } from "lucide-react";
import { PageHeader, Card, Input, IconButton } from "@/components/ui/primitives";
import { SECTIONS } from "./content";

function normalize(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "");
}

export default function DocsPage() {
  const [query, setQuery] = useState("");

  const q = normalize(query.trim());
  const filtered = useMemo(() => {
    if (!q) return SECTIONS;
    return SECTIONS.filter((s) => normalize(`${s.title} ${s.keywords ?? ""}`).includes(q));
  }, [q]);

  function printDoc() {
    // Imprime todo el contenido, sin importar el filtro de búsqueda vigente.
    setQuery("");
    window.setTimeout(() => window.print(), 50);
  }

  return (
    <div>
      <PageHeader
        title="Documentación"
        description="Guía de uso del panel Nexus DWH: cómo hacer cada tarea, qué significa cada estado y qué permiso necesita."
        actions={
          <button
            type="button"
            onClick={printDoc}
            className="print:hidden inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
          >
            <Printer className="h-4 w-4 text-slate-400" />
            Imprimir / PDF
          </button>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[240px_1fr]">
        {/* Tabla de contenido (escritorio): barra lateral fija con búsqueda */}
        <aside className="print:hidden hidden lg:block">
          <div className="sticky top-6 space-y-3">
            <SearchBox query={query} onChange={setQuery} />
            <nav aria-label="Tabla de contenido" className="space-y-0.5 text-sm">
              {filtered.length === 0 && <p className="px-2 py-3 text-xs text-slate-500">Sin resultados para «{query}».</p>}
              {filtered.map((s) => {
                const Icon = s.icon;
                return (
                  <a
                    key={s.id}
                    href={`#${s.id}`}
                    className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  >
                    <Icon className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                    <span className="truncate">{s.title}</span>
                  </a>
                );
              })}
            </nav>
          </div>
        </aside>

        {/* Buscador + índice (móvil) */}
        <div className="print:hidden lg:hidden">
          <SearchBox query={query} onChange={setQuery} />
          {query && (
            <p className="mt-2 text-xs text-slate-500">
              {filtered.length} resultado{filtered.length === 1 ? "" : "s"} para «{query}».
            </p>
          )}
        </div>

        {/* Contenido */}
        <div className="min-w-0 space-y-8">
          {filtered.length === 0 && (
            <Card className="p-8 text-center text-sm text-slate-500">No hay secciones que coincidan con «{query}».</Card>
          )}
          {filtered.map((s) => {
            const Icon = s.icon;
            return (
              <div key={s.id} id={s.id} className="scroll-mt-6">
                <Card className="p-5 sm:p-6">
                  <div className="flex items-center gap-2.5 border-b border-slate-100 pb-3">
                    <div className="rounded-md bg-brand-50 p-1.5 text-brand-600">
                      <Icon className="h-4 w-4" />
                    </div>
                    <h2 className="text-lg font-semibold text-slate-900">{s.title}</h2>
                  </div>
                  <div className="pt-4">{s.body}</div>
                </Card>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function SearchBox({ query, onChange }: { query: string; onChange: (v: string) => void }) {
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
      <Input
        value={query}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Buscar en la documentación…"
        className="pl-8 pr-8"
        aria-label="Buscar en la documentación"
      />
      {query && (
        <IconButton
          label="Limpiar búsqueda"
          onClick={() => onChange("")}
          className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2"
        >
          <X className="h-3.5 w-3.5" />
        </IconButton>
      )}
    </div>
  );
}
