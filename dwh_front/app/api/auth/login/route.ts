import { NextRequest, NextResponse } from "next/server";
import { backendFetch, csrfProblem, forwardHeaders } from "@/lib/server/auth";
import { SESSION_COOKIE, SESSION_MAX_AGE, cookieSecure } from "@/lib/server/config";

export const dynamic = "force-dynamic";

/**
 * Inicio de sesión: valida usuario/contraseña contra el backend
 * (POST /admin/auth/login) y guarda SOLO el token opaco de sesión en una cookie
 * httpOnly + SameSite=Strict (+ Secure en producción). El navegador nunca ve el token.
 */
export async function POST(req: NextRequest) {
  const bad = csrfProblem(req);
  if (bad) return NextResponse.json({ detail: bad }, { status: 403 });
  let username = "";
  let password = "";
  try {
    const body = (await req.json()) as { username?: unknown; password?: unknown };
    username = typeof body.username === "string" ? body.username.trim() : "";
    password = typeof body.password === "string" ? body.password : "";
  } catch {
    return NextResponse.json({ detail: "Solicitud no válida." }, { status: 400 });
  }
  if (!username || !password || username.length > 64 || password.length > 1024) {
    return NextResponse.json({ detail: "Ingresa usuario y contraseña." }, { status: 400 });
  }

  let res: Response;
  try {
    res = await backendFetch("/admin/auth/login", {
      method: "POST",
      headers: { ...forwardHeaders(req), "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch {
    return NextResponse.json({ detail: "No se pudo contactar al backend DWH." }, { status: 502 });
  }
  const data = (await res.json().catch(() => ({}))) as {
    token?: string;
    expires_at?: string;
    must_change_password?: boolean;
    user?: unknown;
    detail?: unknown;
  };
  if (!res.ok || !data.token) {
    const d = data.detail as { message?: string } | string | undefined;
    const message = typeof d === "string" ? d : d?.message || "No se pudo iniciar sesión.";
    return NextResponse.json({ detail: message }, { status: res.status === 200 ? 502 : res.status });
  }
  const expires = data.expires_at ? Math.floor((Date.parse(data.expires_at) - Date.now()) / 1000) : SESSION_MAX_AGE;
  const out = NextResponse.json({ status: "ok", must_change_password: Boolean(data.must_change_password), user: data.user });
  out.cookies.set({
    name: SESSION_COOKIE,
    value: data.token,
    httpOnly: true,
    sameSite: "strict",
    secure: cookieSecure(),
    path: "/",
    maxAge: Math.max(60, Math.min(SESSION_MAX_AGE, expires)),
  });
  out.headers.set("cache-control", "no-store");
  return out;
}
