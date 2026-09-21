import "./globals.css";
import type { Metadata } from "next";
import Nav from "@/components/Nav";

export const metadata: Metadata = { title: "Crypto AI Lab — paper trading", robots: { index: false, follow: false } };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        <div className="paper-banner">PAPER TRADING ONLY — fake money, real prices. No exchange or brokerage is connected.</div>
        <main className="wrap">{children}</main>
      </body>
    </html>
  );
}
