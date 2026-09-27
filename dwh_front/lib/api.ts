"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CSRF_HEADER, CSRF_VALUE } from "@/lib/session";

export class ApiError extends Error {
  status: number;
  code?: string;
  permission?: string;
  constructor(status: number, message: string, code?: string, permission?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.permission = permission;
  }
}

function detailField(data: unknown, key: "code" | "permission"): string | undefined {
  if (data && typeof data === "object" && "detail" in data) {
    const d = (data as { detail: unknown }).detail;
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const v = (d as Record<string, unknown>)[key];
      return typeof v === "string" ? v : undefined;
    }
  }
  return undefined;
}

function extractDetail(data: unknown, fallback: string): string {
  if (data && typeof data === "object" && "detail" in data) {
    const d = (data as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    // Errores con código: {"detail": {"code": "...", "message": "..."}}
    if (d && typeof d === "object" && !Array.isArray(d) && typeof (d as { message?: unknown }).message === "string") {
      return (d as { message: string }).message;
    }
    // Errores de validación de FastAPI: [{loc, msg}, ...]
    if (Array.isArray(d)) {
      return d
        .map((e: { loc?: unknown[]; msg?: string }) => {
          const field = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(".") : "";
          return field ? `${field}: ${e.msg}` : e.msg;
        })
        .join(" · ");
    }
  }
  return fallback;
}

let redirecting = false;

async function handleUnauthorized() {
  if (redirecting || typeof window === "undefined") return;
  redirecting = true;
  try {
    await fetch("/api/auth/logout", { method: "POST", headers: { [CSRF_HEADER]: CSRF_VALUE } });
  } finally {
    window.location.href = "/login";
  }
}

/** Llama al backend DWH a través del proxy /api/dwh/... */
export async function api<T = unknown>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = { [CSRF_HEADER]: CSRF_VALUE };
  if (options.body !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(`/api/dwh/${path.replace(/^\/+/, "")}`, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
  });
  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (res.status === 401) {
    void handleUnauthorized();
    throw new ApiError(401, extractDetail(data, "Sesión expirada."));
  }
  const code = detailField(data, "code");
  if (res.status === 403 && code === "password_change_required" && typeof window !== "undefined") {
    window.location.href = "/cambiar-contrasena";
  }
  if (res.status === 403 && code === "permission_required") {
    throw new ApiError(403, "Sin permiso para esta acción. " + extractDetail(data, ""), code, detailField(data, "permission"));
  }
  if (!res.ok) {
    throw new ApiError(res.status, extractDetail(data, `Error ${res.status}`), code, detailField(data, "permission"));
  }
  return data as T;
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  });
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Hook de lectura simple con estados loading/error y recarga. */
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Código HTTP del último error (p. ej. 404 = no existe o fuera del alcance). */
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(path));
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!path) return;
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const d = await api<T>(path);
      if (id === seq.current) setData(d);
    } catch (e) {
      if (id === seq.current) {
        setError((e as Error).message);
        setStatus(e instanceof ApiError ? e.status : null);
      }
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void load();
  }, [load]);

  return { data, error, status, loading, reload: load, setData };
}
