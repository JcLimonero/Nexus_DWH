import "server-only";

/** Nombre de la cookie httpOnly con el token de sesión. */
export { SESSION_COOKIE } from "@/lib/session";

export function apiBaseUrl(): string {
  const url = (process.env.DWH_API_URL || "").trim().replace(/\/+$/, "");
  if (!url) throw new Error("Falta la variable de entorno DWH_API_URL.");
  return url;
}

export function cookieSecure(): boolean {
  const v = (process.env.DWH_COOKIE_SECURE || "").trim().toLowerCase();
  if (v === "true" || v === "1") return true;
  if (v === "false" || v === "0") return false;
  return process.env.NODE_ENV === "production";
}

/** Tope de la cookie (s); el vencimiento real lo decide el backend (absoluto + inactividad). */
export const SESSION_MAX_AGE = 60 * 60 * 12;
