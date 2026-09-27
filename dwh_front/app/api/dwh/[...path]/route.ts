/**
 * Proxy servidor → backend DWH.
 *
 * - Solo rutas /admin/* (el panel ya no usa /monitor/* ni el token de monitor:
 *   usa los equivalentes /admin/events|clients|activity con permisos y alcance).
 * - Agrega `Authorization: Bearer <sesión>` desde la cookie httpOnly.
 * - login/logout NO pasan por aquí (route handlers propios que manejan la cookie).
 * - CSRF: toda petición que modifica exige la cabecera x-nexus-csrf y mismo Origin
 *   (además de la cookie SameSite=Strict).
 */
import { NextRequest, NextResponse } from "next/server";
import { backendFetch, csrfProblem, forwardHeaders } from "@/lib/server/auth";
import { SESSION_COOKIE } from "@/lib/server/config";

export const dynamic = "force-dynamic";

type Ctx = { params: { path: string[] } };

function json(detail: string, status: number) {
  return NextResponse.json({ detail }, { status });
}

const BLOCKED = new Set(["admin/auth/login", "admin/auth/logout"]);

async function proxy(req: NextRequest, { params }: Ctx) {
  const token = req.cookies.get(SESSION_COOKIE)?.value || "";
  if (!token) return json("Sesión no iniciada.", 401);

  const segments = params.path || [];
  if (
    segments.length < 2 ||
    segments[0] !== "admin" ||
    segments.some((s) => !s || s === "." || s === ".." || s.includes("/") || s.includes("\\")) ||
    BLOCKED.has(segments.join("/"))
  ) {
    return json("Ruta no permitida.", 404);
  }
  const bad = csrfProblem(req);
  if (bad) return json(bad, 403);

  const headers = forwardHeaders(req, token);
  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
    if (body) headers["content-type"] = "application/json";
  }
  const path = `/${segments.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  try {
    const res = await backendFetch(path, { method: req.method, headers, body, timeoutMs: 30_000 });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: {
        "content-type": res.headers.get("content-type") || "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    return json("No se pudo contactar al backend DWH.", 502);
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
