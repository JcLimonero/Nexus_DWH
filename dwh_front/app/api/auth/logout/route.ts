import { NextRequest, NextResponse } from "next/server";
import { backendFetch, csrfProblem, forwardHeaders } from "@/lib/server/auth";
import { SESSION_COOKIE, cookieSecure } from "@/lib/server/config";

export const dynamic = "force-dynamic";

/** Cierra la sesión en el backend (la revoca) y borra la cookie. */
export async function POST(req: NextRequest) {
  const bad = csrfProblem(req);
  if (bad) return NextResponse.json({ detail: bad }, { status: 403 });
  const token = req.cookies.get(SESSION_COOKIE)?.value || "";
  if (token) {
    await backendFetch("/admin/auth/logout", { method: "POST", headers: forwardHeaders(req, token) }).catch(
      () => undefined,
    );
  }
  const res = NextResponse.json({ status: "ok" });
  res.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    sameSite: "strict",
    secure: cookieSecure(),
    path: "/",
    maxAge: 0,
  });
  return res;
}
