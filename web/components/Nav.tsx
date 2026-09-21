"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const Icon = ({ d }: { d: string }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d={d} /></svg>
);
const TABS = [
  ["/", "Dashboard", "M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"],
  ["/signals", "Signals", "M5 20V10M12 20V4M19 20v-7"],
  ["/news", "News", "M6 3h12a1 1 0 011 1v16l-3-2-3 2-3-2-3 2V4a1 1 0 011-1zM9 8h6M9 12h6"],
  ["/trades", "Trades", "M3 17l6-6 4 4 8-8M15 7h6v6"],
  ["/settings", "Settings", "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"],
];

export default function Nav() {
  const path = usePathname();
  const active = (href: string) => (href === "/" ? path === "/" : path === href || path.startsWith(href + "/"));
  return (
    <header className="top">
      <div className="wrap topbar">
        <Link href="/" className="brand" aria-label="Crypto AI Lab home">
          <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden><defs><linearGradient id="lg" x1="0" x2="1" y1="1" y2="0"><stop offset="0" stopColor="#3b82f6" /><stop offset="1" stopColor="#22d3ee" /></linearGradient></defs><path d="M20 4L36 34H4z" fill="url(#lg)" /><path d="M20 15l7 14H13z" fill="#0b1220" opacity=".55" /></svg>
          <span><b>Crypto AI Lab</b><small>Paper Trading</small></span>
        </Link>
        <nav className="navtabs" aria-label="Main">
          {TABS.map(([href, label, d]) => (
            <Link key={href} href={href} className={active(href) ? "on" : ""}><Icon d={d} /><span>{label}</span></Link>
          ))}
        </nav>
        <div className="papertag" title="Nothing here can place a real order"><span className="dot" /><b>Paper Trading</b><small>No real money. Trade safely.</small></div>
      </div>
    </header>
  );
}
