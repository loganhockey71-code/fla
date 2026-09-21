import { NextRequest, NextResponse } from "next/server";

/** CSRF guard for endpoints that move (fake) money: the browser sends the Basic-auth password automatically, so require a same-origin JSON request. */
export function blockCrossSite(req: NextRequest): NextResponse | null {
  const origin = req.headers.get("origin");
  if (!origin || new URL(origin).host !== req.headers.get("host") || !(req.headers.get("content-type") ?? "").startsWith("application/json"))
    return NextResponse.json({ ok: false, message: "Blocked: request must come from this site." }, { status: 403 });
  return null;
}
