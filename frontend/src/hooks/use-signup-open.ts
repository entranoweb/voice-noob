"use client";

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * Whether the backend accepts sign-ups right now.
 *
 * `null` while the answer is on its way, then `true` or `false`. A failed
 * request counts as closed: the link stays hidden rather than pointing at a form
 * the server would refuse. The server refuses a closed sign-up whatever this
 * says (POST /api/v1/auth/register answers 403); this only keeps the page honest.
 */
export function useSignupOpen(): boolean | null {
  const [open, setOpen] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${API_BASE}/api/v1/auth/config`);
        const data = response.ok ? ((await response.json()) as { signup_open?: unknown }) : null;
        if (!cancelled) setOpen(data?.signup_open === true);
      } catch {
        if (!cancelled) setOpen(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return open;
}
