import { NextRequest, NextResponse } from "next/server";
import { forgetToken } from "@/lib/server/auth";
import { SESSION_COOKIE, cookieSecure } from "@/lib/server/config";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  forgetToken(req.cookies.get(SESSION_COOKIE)?.value || "");
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
