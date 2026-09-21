"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  ["/", "Dashboard"], ["/btc", "BTC"], ["/eth", "ETH"], ["/xrp", "XRP"], ["/events", "Events / Research"],
  ["/trade", "Trade"], ["/predictions", "Predictions"], ["/trades", "Paper Trades"], ["/performance", "Performance"], ["/health", "Health"], ["/settings", "Settings"],
];

export default function Nav() {
  const path = usePathname();
  return (
    <nav className="top">
      <div className="wrap">
        <span className="brand">◆ Crypto AI Lab</span>
        {TABS.map(([href, label]) => (
          <Link key={href} href={href} className={path === href ? "on" : ""}>{label}</Link>
        ))}
      </div>
    </nav>
  );
}
