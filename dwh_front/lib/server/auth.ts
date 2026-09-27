import "server-only";
import { isIP } from "node:net";
import type { NextRequest } from "next/server";
import { CSRF_HEADER, CSRF_VALUE } from "@/lib/session";
import { apiBaseUrl } from "./config";

/**
 * IP del navegador, SOLO de fuentes de confianza (nunca el primer valor de X-Forwarded-For,
 * que lo controla el cliente):
 * - `DWH_CLIENT_IP_HEADER` (p. ej. `x-real-ip`): cabecera que el proxy inverso de confianza
 *   SOBRESCRIBE con la IP del socket.
 * - `DWH_TRUSTED_PROXY_HOPS` = N ≥ 1: hay N proxies de confianza que AGREGAN a X-Forwarded-For;
 *   la IP del cliente es la N-ésima desde el final.
 * - Sin nada configurado (defecto): desconocida (""). Next (servidor propio) no expone la IP
 *   del socket cuando el cliente ya envía X-Forwarded-For, así que no se puede confiar en ella.
 *   El backend aplica entonces solo el bloqueo por usuario (nunca bloquea a todos).
 */
export function clientIp(req: NextRequest): string {
  let candidate = "";
  const header = (process.env.DWH_CLIENT_IP_HEADER || "").trim().toLowerCase();
  const hops = Number.parseInt(process.env.DWH_TRUSTED_PROXY_HOPS || "0", 10);
  if (header) {
    candidate = (req.headers.get(header) || "").split(",")[0]?.trim() || "";
  } else if (Number.isInteger(hops) && hops > 0) {
    const list = (req.headers.get("x-forwarded-for") || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    candidate = list.length >= hops ? list[list.length - hops] : "";
  }
  if (candidate.startsWith("::ffff:")) candidate = candidate.slice(7);
  return isIP(candidate) ? candidate : "";
}

/**
 * Cabeceras hacia el backend: IP del usuario (si se conoce) en `x-nexus-client-ip`, autenticada
 * con la clave compartida `DWH_PANEL_PROXY_KEY` (= `[auth] panel_proxy_key` del backend).
 * Nunca se reenvía X-Forwarded-For ni las cookies del navegador.
 */
export function forwardHeaders(req: NextRequest, token?: string): Record<string, string> {
  const h: Record<string, string> = { accept: "application/json" };
  const key = (process.env.DWH_PANEL_PROXY_KEY || "").trim();
  if (key) {
    h["x-nexus-proxy-key"] = key;
    const ip = clientIp(req);
    if (ip) h["x-nexus-client-ip"] = ip;
  }
  const ua = req.headers.get("user-agent");
  if (ua) h["user-agent"] = ua.slice(0, 200);
  if (token) h.authorization = `Bearer ${token}`;
  return h;
}

/** Origen público del panel: `DWH_PUBLIC_ORIGIN` (recomendado) o el Host de la petición. */
function expectedOrigin(req: NextRequest): string {
  const configured = (process.env.DWH_PUBLIC_ORIGIN || "").trim().replace(/\/+$/, "");
  if (configured) return configured.toLowerCase();
  const host = req.headers.get("host") || "";
  return host ? `${req.nextUrl.protocol}//${host}`.toLowerCase() : "";
}

/**
 * Protección CSRF para peticiones que modifican: cabecera personalizada obligatoria, Origin (si
 * viene) igual al origen público del panel y Sec-Fetch-Site del mismo sitio. No se usa
 * X-Forwarded-Host (lo controla el cliente).
 */
export function csrfProblem(req: NextRequest): string | null {
  if (req.method === "GET" || req.method === "HEAD") return null;
  if (req.headers.get(CSRF_HEADER) !== CSRF_VALUE) return "Falta la cabecera anti-CSRF.";
  const origin = req.headers.get("origin");
  if (origin) {
    let o = "";
    try {
      o = new URL(origin).origin.toLowerCase();
    } catch {
      return "Origen no válido.";
    }
    const expected = expectedOrigin(req);
    if (!expected || o !== expected) return "Origen no permitido.";
  }
  const site = req.headers.get("sec-fetch-site");
  if (site && site !== "same-origin" && site !== "none") return "Petición entre sitios no permitida.";
  return null;
}

export async function backendFetch(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<Response> {
  const { timeoutMs = 15_000, ...rest } = init;
  return fetch(`${apiBaseUrl()}${path}`, {
    ...rest,
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
}
