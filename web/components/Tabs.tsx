import Link from "next/link";

/** Simple page tabs driven by ?tab=... so every sub-view keeps a shareable URL. */
export default function Tabs({ base, current, tabs }: { base: string; current: string; tabs: { key: string; label: string }[] }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => <Link key={t.key} href={`${base}?tab=${t.key}`} className={current === t.key ? "on" : ""} role="tab" aria-selected={current === t.key}>{t.label}</Link>)}
    </div>
  );
}
