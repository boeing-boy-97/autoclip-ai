import type { Metadata } from "next";

import "./globals.css";
import { SessionProvider } from "@/lib/session";
import { Shell } from "@/components/Shell";

export const metadata: Metadata = {
  title: "AutoClipper AI",
  description: "Turn long recordings into vertical clips, with captions that match the audio.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>
          <Shell>{children}</Shell>
        </SessionProvider>
      </body>
    </html>
  );
}
