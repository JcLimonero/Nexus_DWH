import "server-only";
import { createHash } from "node:crypto";
import { apiBaseUrl } from "./config";

export type TokenCheck = "ok" | "invalid" | "unavailable" | "not_configured";

// Caché corta de tokens válidos (hash -> expiración) para no validar en cada request.
const cache = new Map<string, number>();
const TTL_MS = 15_000;

function hash(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/** Valida el token de administrador contra el backend (GET /admin/whoami). */
export async function checkAdminToken(token: string, useCache = true): Promise<TokenCheck> {
  if (!token) return "invalid";
  const key = hash(token);
  const now = Date.now();
  if (useCache) {
    const exp = cache.get(key);
    if (exp && exp > now) return "ok";
  }
  try {
    const res = await fetch(`${apiBaseUrl()}/admin/whoami`, {
      headers: { "x-admin-token": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    if (res.ok) {
      cache.set(key, now + TTL_MS);
      if (cache.size > 100) {
        cache.forEach((exp, k) => {
          if (exp <= now) cache.delete(k);
        });
      }
      return "ok";
    }
    cache.delete(key);
    if (res.status === 401) return "invalid";
    if (res.status === 503) return "not_configured";
    return "unavailable";
  } catch {
    return "unavailable";
  }
}

export function forgetToken(token: string): void {
  if (token) cache.delete(hash(token));
}
