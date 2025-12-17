"use client";

import { useEffect } from "react";

export default function EmbedLayout({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    // Make the body transparent for embed routes
    document.body.style.background = "transparent";
    document.documentElement.style.background = "transparent";

    // Hide Next.js development indicator
    const style = document.createElement("style");
    style.textContent = `
      nextjs-portal { display: none !important; }
      [data-nextjs-dialog] { display: none !important; }
      #__next-build-indicator { display: none !important; }
    `;
    document.head.appendChild(style);

    return () => {
      // Cleanup
      document.body.style.background = "";
      document.documentElement.style.background = "";
      style.remove();
    };
  }, []);

  return <>{children}</>;
}
