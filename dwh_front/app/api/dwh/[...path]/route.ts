/**
 * Proxy servidor → backend DWH.
 *
 * - Solo permite rutas /admin/* y /monitor/*.
 * - Agrega x-admin-token desde la cookie httpOnly de sesión.
 * - Para /monitor/* valida primero la sesión admin y agrega x-monitor-token
 *   desde la variable de servidor DWH_MONITOR_TOKEN (nunca llega al navegador).
 */
import { NextRequest, NextResponse } from "next/server";
import { checkAdminToken } from "@/lib/server/auth";
import { SESSION_COOKIE, apiBaseUrl, monitorToken } from "@/lib/server/config";

export const dynamic = "force-dynamic";

type Ctx = { params: { path: string[] } };

function json(detail: string, status: number) {
  return NextResponse.json({ detail }, { status });
}

async function proxy(req: NextRequest, { params }: Ctx) {
  const token = req.cookies.get(SESSION_COOKIE)?.value || "";
  if (!token) return json("Sesión no iniciada.", 401);

  const segments = params.path || [];
  if (
    segments.length === 0 ||
    !["admin", "monitor"].includes(segments[0]) ||
    segments.some((s) => !s || s === "." || s === ".." || s.includes("/") || s.includes("\\"))
  ) {
    return json("Ruta no permitida.", 404);
  }

  const headers: Record<string, string> = { "x-admin-token": token, accept: "application/json" };

  if (segments[0] === "monitor") {
    // El token de monitor lo pone el servidor: exigir sesión admin válida.
    const check = await checkAdminToken(token);
    if (check === "invalid") return json("Sesión expirada o token no válido.", 401);
    if (check !== "ok") return json("No se pudo validar la sesión con el backend.", 502);
    const mt = monitorToken();
    if (!mt) return json("Falta DWH_MONITOR_TOKEN en la configuración del panel.", 503);
    headers["x-monitor-token"] = mt;
  }

  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
    if (body) headers["content-type"] = "application/json";
  }

  let base: string;
  try {
    base = apiBaseUrl();
  } catch (e) {
    return json((e as Error).message, 500);
  }
  const url = `${base}/${segments.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;

  try {
    const res = await fetch(url, {
      method: req.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") || "application/json" },
    });
  } catch {
    return json("No se pudo contactar al backend DWH.", 502);
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
