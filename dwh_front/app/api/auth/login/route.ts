import { NextRequest, NextResponse } from "next/server";
import { checkAdminToken } from "@/lib/server/auth";
import { SESSION_COOKIE, SESSION_MAX_AGE, cookieSecure } from "@/lib/server/config";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  let token = "";
  try {
    const body = (await req.json()) as { token?: unknown };
    token = typeof body.token === "string" ? body.token.trim() : "";
  } catch {
    return NextResponse.json({ detail: "Solicitud no válida." }, { status: 400 });
  }
  if (!token || token.length > 512) {
    return NextResponse.json({ detail: "Ingresa el token de administrador." }, { status: 400 });
  }

  const result = await checkAdminToken(token, false);
  if (result === "invalid") {
    return NextResponse.json({ detail: "Token de administrador no válido." }, { status: 401 });
  }
  if (result === "not_configured") {
    return NextResponse.json(
      { detail: "El backend no tiene configurado el token de administrador ([admin] token)." },
      { status: 503 },
    );
  }
  if (result === "unavailable") {
    return NextResponse.json({ detail: "No se pudo contactar al backend DWH." }, { status: 502 });
  }

  const res = NextResponse.json({ status: "ok" });
  res.cookies.set({
    name: SESSION_COOKIE,
    value: token,
    httpOnly: true,
    sameSite: "strict",
    secure: cookieSecure(),
    path: "/",
    maxAge: SESSION_MAX_AGE,
  });
  return res;
}
