import Tabs from "@/components/Tabs";
import SettingsView from "@/components/views/SettingsView";
import HealthView from "@/components/views/HealthView";

export const dynamic = "force-dynamic";
const TABS = [{ key: "settings", label: "Settings" }, { key: "health", label: "System health" }];

export default async function SettingsPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const tab = TABS.some((t) => t.key === searchParams.tab) ? searchParams.tab! : "settings";
  return (
    <>
      <h1>Settings</h1>
      <Tabs base="/settings" current={tab} tabs={TABS} />
      {tab === "settings" ? <SettingsView /> : <HealthView />}
    </>
  );
}
