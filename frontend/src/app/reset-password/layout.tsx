import type { Metadata } from "next";
import type { ReactNode } from "react";

// The link carries a secret in its query string. Send no Referer from this page,
// so the token cannot leak to any third party the page loads or links to.
export const metadata: Metadata = {
  referrer: "no-referrer",
};

export default function ResetPasswordLayout({ children }: { children: ReactNode }) {
  return children;
}
