import type { Metadata } from "next";
import "../../../globals.css";

export const metadata: Metadata = {
  title: "Widget Preview - Voice Noob",
  description: "Preview your Voice Noob voice agent widget",
};

export default function PreviewLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-black antialiased">{children}</body>
    </html>
  );
}
