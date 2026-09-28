"use client";

import { useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { Database, Eye, EyeOff, LogIn } from "lucide-react";
import { Button, Input } from "@/components/ui/primitives";
import { CSRF_HEADER, CSRF_VALUE } from "@/lib/session";

/** Solo permite volver a rutas del mismo origen (evita open redirect). */
export function safeNext(next: string | null): string {
  if (!next) return "/";
  try {
    const url = new URL(next, window.location.origin);
    if (url.origin !== window.location.origin) return "/";
    // El pathname normalizado puede quedar como "//host" (p. ej. "/.//host"): colapsar a una sola barra.
    const path = "/" + url.pathname.replace(/^[/\\]+/, "");
    return `${path}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
}

export default function LoginForm() {
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json", [CSRF_HEADER]: CSRF_VALUE },
        body: JSON.stringify({ email, password }),
      });
      const data = (await res.json().catch(() => ({}))) as { detail?: string; must_change_password?: boolean };
      if (!res.ok) {
        setError(data.detail || "No se pudo iniciar sesión.");
        return;
      }
      setPassword("");
      window.location.href = data.must_change_password ? "/cambiar-contrasena" : safeNext(params.get("next"));
    } catch {
      setError("No se pudo contactar al servidor.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="w-full max-w-sm">
      <div className="mb-6 flex flex-col items-center text-center">
        <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-brand-600 text-white shadow-md">
          <Database className="h-6 w-6" />
        </div>
        <h1 className="text-xl font-semibold text-slate-900">Nexus DWH</h1>
        <p className="text-sm text-slate-500">Panel de administración</p>
      </div>
      <form onSubmit={onSubmit} className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <label htmlFor="email" className="mb-1 block text-sm font-medium text-slate-700">
          Correo
        </label>
        <Input
          id="email"
          type="email"
          autoComplete="username email"
          autoCapitalize="none"
          spellCheck={false}
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          maxLength={255}
        />
        <label htmlFor="password" className="mb-1 mt-4 block text-sm font-medium text-slate-700">
          Contraseña
        </label>
        <div className="relative">
          <Input
            id="password"
            type={show ? "text" : "password"}
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="pr-10"
            maxLength={1024}
          />
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-slate-400 hover:text-slate-600"
            aria-label={show ? "Ocultar contraseña" : "Mostrar contraseña"}
          >
            {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
        {error && (
          <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error}
          </div>
        )}
        <Button
          type="submit"
          className="mt-5 w-full"
          loading={loading}
          disabled={!email.trim() || !password}
          icon={<LogIn className="h-4 w-4" />}
        >
          Entrar
        </Button>
        <p className="mt-3 text-center text-[11px] text-slate-500">
          Tras varios intentos fallidos la cuenta se bloquea temporalmente.
        </p>
      </form>
    </div>
  );
}
