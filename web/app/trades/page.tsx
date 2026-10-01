import Tabs from "@/components/Tabs";
import MyTradingView from "@/components/views/MyTradingView";
import AiTradesView from "@/components/views/AiTradesView";
import ScalperView from "@/components/views/ScalperView";

export const dynamic = "force-dynamic";
const TABS = [{ key: "scalper", label: "Scalper" }, { key: "mine", label: "My trading" }, { key: "ai", label: "Old AI account" }];

export default async function TradesPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const tab = TABS.some((t) => t.key === searchParams.tab) ? searchParams.tab! : "scalper";
  return (
    <>
      <h1>Trades</h1>
      <Tabs base="/trades" current={tab} tabs={TABS} />
      {tab === "scalper" ? <ScalperView /> : tab === "mine" ? <MyTradingView /> : <AiTradesView />}
    </>
  );
}
